"""One export operation with three modes (SPL-117): each segment, the whole timeline, the accepted highlights as shorts.

Exporting is a batch on the render queue (`render_queue.py`): one `render` job per output file, tagged with a
shared batch id, so concurrency is bounded, each file has a state and progress, cancel is real and one file that
fails (a missing source, an ffmpeg error, an output that exists) never stops the others. `submit_export` returns the
render queue's batch status (`{batch_id, concurrency, total, counts, finished, jobs}`) plus `mode`; poll and cancel
with `render_queue.batch_status` / `cancel_renders`, which every surface already has.

Modes
-----
`segments` (a recording): each included segment of the video's main track as its own file in `<video>_split/`, named
as the split export names them (`naming.resolve_export_filename`: export name, label, or the numbered default).
Without render options a file is a stream copy (`precise` re-encodes for frame accuracy), exactly as the split
export writes it. With a preset, size, fit or captions each segment is rendered as a one-clip timeline (its clip's crop,
gain and colour kept) to `.mp4`.

`timeline` (a timeline project, or a recording's own timeline): the whole timeline as one `<name>.mp4`, named after
the project (a recording's name without its extension). Rendered; every clip plays, whatever `included` says.

`highlights` (a list of timeline names): each named timeline as a vertical short, `<name>.mp4`, default preset
`vertical-1080` with `fit` crop, and the `shorts` captions when a clip's asset has a transcript.

What "accepted" means
---------------------
The project data has no accepted flag. `suggest_highlights` returns candidates and an edit plan turns one into a
timeline; a highlight is accepted when the caller names that timeline in `timelines`. The caller (the highlights panel,
an agent) owns the list, so unreviewed suggestions are never exported, and nothing new is stored in a project file.
"""

from __future__ import annotations

import posixpath
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from split_video import captions, project, timeline_service, transcribe
from split_video.errors import ProjectError
from split_video.ffmpeg import extract_segment
from split_video.identity import LOCAL_PRINCIPAL, Principal
from split_video.job_work import RENDER_STEPS, render_work
from split_video.jobs import RENDER, JobCancelledError, JobContext, JobQueue
from split_video.media_store import MediaStore
from split_video.presets import resolve_export
from split_video.render_queue import MAX_BATCH, _info_of, batch_status
from split_video.split_model import main_track_index
from split_video.store import ProjectStore
from split_video.timeline import Clip, OutputExistsError, Project, TimelineError, Track

MODES = ("segments", "timeline", "highlights")
SHORT_PRESET = "vertical-1080"
SHORT_FIT = "crop"
SHORT_CAPTIONS = "shorts"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts")


class ExportError(TimelineError):
    """The export request itself is unusable (unknown mode, missing argument, nothing to export)."""

    error_code = "export.invalid"


@dataclass(frozen=True)
class RenderOptions:
    """The render options of an export, as a render request has them."""

    width: int | None = None
    height: int | None = None
    preset: str | None = None
    fit: str | None = None
    captions: str | bool | None = None
    caption_range: Any = None
    caption_font_size: float | None = None
    caption_highlight_color: str | None = None
    caption_position: str | None = None
    caption_tracks: list[int] | None = None

    @property
    def any_set(self) -> bool:
        return any(v is not None for v in (self.width, self.height, self.preset, self.fit, self.captions))

    def caption_request(self, captions_value: str | bool | None) -> captions.CaptionRequest | None:
        return captions.request(
            captions_value,
            self.caption_range,
            self.caption_font_size,
            self.caption_highlight_color,
            self.caption_position,
            self.caption_tracks,
        )


@dataclass
class _Item:
    """One output file: its label, and either the work that writes it or the reason it cannot be written."""

    label: str
    work: Callable[[JobContext], Any] | None = None
    error: Exception | None = None


def _plain_filename(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ExportError("output must be a plain filename")
    if not name.lower().endswith(".mp4"):
        raise ExportError("output must end in .mp4")
    return name


def _directory(media: MediaStore, output_dir: str | None, default: str) -> str:
    """The media id of the folder the files go in: `output_dir` (a folder in the root) or `default`."""
    if output_dir is None:
        return default
    return media.resolve(output_dir) if output_dir else ""


def _stem(name: str) -> str:
    stem, ext = posixpath.splitext(posixpath.basename(name))
    return stem if ext.lower() in VIDEO_EXTENSIONS else posixpath.basename(name)


def _single_clip(clip: Clip) -> Project:
    """A timeline of just `clip` (crop, gain and colour kept), for rendering one segment on its own."""
    plain = replace(clip, start=0.0, transition="cut", transition_duration=0.0)
    return Project(tracks=(Track(id="main", kind="video", clips=(plain,), magnetic=True),))


def _render_item(
    label: str,
    timeline: Project,
    media: MediaStore,
    out_id: str,
    filename: str,
    options: RenderOptions,
    caption_value: str | bool | None,
    overwrite: bool,
    claimed: set[str],
    sources: set[str],
) -> _Item:
    """A rendered output, checked as `plan_render` checks one: it must not exist, clash with another output of
    the batch or be one of the project's own sources, and the timeline needs clips and valid caption tracks."""
    try:
        if out_id in claimed:
            raise ExportError(f"{label} is already the output of another file in this export")
        if out_id in sources:
            raise OutputExistsError(f"{label} is a source file of this project: it would be overwritten")
        if media.exists(out_id) and not overwrite:
            raise OutputExistsError(f"{label} already exists")
        if not any(t.clips for t in timeline.tracks):
            raise TimelineError("the timeline has no clips to render")
        request = options.caption_request(caption_value)
        if request is not None:
            captions.caption_tracks(timeline, request)
        claimed.add(out_id)
        work = render_work(
            timeline, media, out_id, filename, options.width, options.height, options.preset, options.fit, label,
            request,
        )  # fmt: skip
        return _Item(label, work)
    except (TimelineError, ValueError) as exc:
        return _Item(label, error=exc)


def _has_transcript(media: MediaStore, timeline: Project) -> bool:
    """Whether any media clip's asset has a stored transcript (a clip whose asset is gone has none; its render fails
    with the real reason)."""
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.kind != "media" or not clip.asset:
                continue
            try:
                if transcribe.load(media, clip.asset) is not None:
                    return True
            except (TimelineError, ValueError):
                continue
    return False


def _segments_items(
    store: ProjectStore,
    media: MediaStore,
    video: str,
    only: list[int] | None,
    output_dir: str | None,
    overwrite: bool,
    precise: bool,
    output_format: str | None,
    options: RenderOptions,
) -> list[_Item]:
    segments = project.current_segments(store, media, video)
    project.select_for_export(segments, only)  # validates `only`
    numbers = sorted(set(only)) if only is not None else [n + 1 for n, s in enumerate(segments) if s.included]
    if not numbers:
        raise ProjectError("segments list must not be empty: no segment is included (pass `only` to name some)")
    chosen = [segments[n - 1] for n in numbers]
    rendering = options.any_set
    # The same planning as the split export: names, order and validation. Conflicts are per file below.
    plan = project.plan_export(
        media,
        video,
        [(s.start, s.end, s.export_name) for s in chosen],
        output_format="mp4" if rendering else output_format,
        overwrite=True,
    )
    folder = _directory(media, output_dir, plan.output_dir)
    timeline = project.video_timeline(store, media, video)
    index = main_track_index(timeline)
    clips = [c for c in timeline.tracks[index].clips if c.kind == "media"] if index is not None else []
    items: list[_Item] = []
    claimed: set[str] = set()
    for number, seg, filename in zip(numbers, chosen, plan.filenames):
        out_id = media.join(folder, filename)
        if rendering:
            clip = clips[number - 1]
            items.append(
                _render_item(
                    filename, _single_clip(clip), media, out_id, filename, options, options.captions, overwrite,
                    claimed, {video},
                )
            )  # fmt: skip
            continue
        if out_id in claimed:
            items.append(_Item(filename, error=ExportError(f"{filename} is already the output of another file")))
            continue
        if media.exists(out_id) and not overwrite:
            items.append(_Item(filename, error=OutputExistsError(f"{filename} already exists")))
            continue
        claimed.add(out_id)
        items.append(_Item(filename, _copy_work(media, video, out_id, seg.start, seg.end, precise, output_format)))
    return items


def _copy_work(
    media: MediaStore, video: str, out_id: str, start: float, end: float, precise: bool, output_format: str | None
) -> Callable[[JobContext], dict[str, Any]]:
    def work(ctx: JobContext) -> dict[str, Any]:
        try:
            with media.local_path(video) as source, media.write_local(out_id) as out_path:
                extract_segment(source, start, end, out_path, precise, output_format, cancel=ctx)
        except JobCancelledError:
            media.delete(out_id)
            raise
        except Exception:
            media.delete(out_id)
            raise
        ctx.progress(RENDER_STEPS, RENDER_STEPS)
        return {"name": media.name(out_id), "output": media.name(out_id), "duration": round(end - start, 3)}

    return work


def _timeline_items(
    store: ProjectStore,
    media: MediaStore,
    video: str | None,
    name: str | None,
    output: str | None,
    output_dir: str | None,
    overwrite: bool,
    options: RenderOptions,
) -> list[_Item]:
    if (video is None) == (name is None):
        raise ExportError("mode timeline needs exactly one of video (a recording's own timeline) and project (a name)")
    if video is not None:
        timeline = project.video_timeline(store, media, video)
        project_name = _stem(media.name(video))
        default_dir = posixpath.dirname(video)
    else:
        assert name is not None
        timeline = timeline_service.load_existing(store, name, media)
        project_name = _stem(name)
        default_dir = ""
    filename = _plain_filename(output) if output else f"{project_name}.mp4"
    out_id = media.join(_directory(media, output_dir, default_dir), filename)
    sources = {c.source for t in timeline.tracks for c in t.clips if c.kind == "media"}
    return [
        _render_item(filename, timeline, media, out_id, filename, options, options.captions, overwrite, set(), sources)
    ]


def _highlight_items(
    store: ProjectStore,
    media: MediaStore,
    names: list[str],
    output_dir: str | None,
    overwrite: bool,
    options: RenderOptions,
) -> list[_Item]:
    if not isinstance(names, list | tuple) or not names or not all(isinstance(n, str) and n for n in names):
        raise ExportError(
            "mode highlights needs timelines: the names of the accepted highlights' timelines "
            "(a highlight is accepted when you list its timeline; unreviewed suggestions are not exported)"
        )
    shorts = replace(
        options,
        preset=options.preset if options.preset is not None else SHORT_PRESET,
        fit=options.fit if options.fit is not None else SHORT_FIT,
    )
    folder = _directory(media, output_dir, "")
    items: list[_Item] = []
    claimed: set[str] = set()
    for name in names:
        label = f"{_stem(name)}.mp4"
        try:
            timeline = timeline_service.load_existing(store, name, media)
        except (TimelineError, ValueError) as exc:
            items.append(_Item(label, error=exc))
            continue
        if options.captions is None:
            caption_value: str | bool | None = SHORT_CAPTIONS if _has_transcript(media, timeline) else None
        else:
            caption_value = None if options.captions == "off" else options.captions
        sources = {c.source for t in timeline.tracks for c in t.clips if c.kind == "media"}
        items.append(
            _render_item(
                label, timeline, media, media.join(folder, label), label, shorts, caption_value, overwrite, claimed,
                sources,
            )
        )  # fmt: skip
    return items


def submit_export(
    jobs: JobQueue,
    store: ProjectStore,
    media: MediaStore,
    mode: str,
    *,
    video: str | None = None,
    project_name: str | None = None,
    timelines: list[str] | None = None,
    only: list[int] | None = None,
    output: str | None = None,
    output_dir: str | None = None,
    overwrite: bool = False,
    precise: bool = False,
    output_format: str | None = None,
    options: RenderOptions | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Queue the export and return the batch's status (`render_queue.batch_status` plus `mode`), one job per file in
    output order. `video` is the recording's media id (already resolved); `project_name` a timeline's name;
    `timelines` the accepted highlights' timeline names. A file that cannot be written (it exists, an unknown
    timeline, bad caption tracks) is already `failed` with `{code, message, fix}`; the others carry on. Raises
    `ExportError` (code `export.invalid`) or the usual `TimelineError`/`ProjectError`/`PresetError`/`CaptionError`
    when the request itself is wrong, before anything is queued."""
    options = options or RenderOptions()
    if mode not in MODES:
        raise ExportError(f"unknown export mode {mode!r}; valid modes: {', '.join(MODES)}")
    # Options shared by every file are checked once, before anything is queued.
    resolve_export(
        options.preset if options.preset is not None or mode != "highlights" else SHORT_PRESET,
        options.width,
        options.height,
        options.fit,
    )
    options.caption_request(None if options.captions in ("off", False) else options.captions)
    if mode == "segments":
        if video is None:
            raise ExportError("mode segments needs video (the recording whose segments are written)")
        items = _segments_items(store, media, video, only, output_dir, overwrite, precise, output_format, options)
    elif mode == "timeline":
        items = _timeline_items(store, media, video, project_name, output, output_dir, overwrite, options)
    else:
        items = _highlight_items(store, media, list(timelines or []), output_dir, overwrite, options)
    if len(items) > MAX_BATCH:
        raise ExportError(f"at most {MAX_BATCH} files per export, got {len(items)}")
    batch_id = uuid.uuid4().hex
    owner = None if principal is None else principal.id
    for item in items:
        if item.work is None:
            exc = item.error or RuntimeError("export item failed")
            jobs.record_failed(
                RENDER,
                error=str(exc) or type(exc).__name__,
                error_code=getattr(exc, "error_code", None) or type(exc).__name__,
                error_info=_info_of(exc),
                label=item.label,
                total=RENDER_STEPS,
                owner=owner,
                group=batch_id,
            )
        else:
            jobs.submit(RENDER, item.work, label=item.label, total=RENDER_STEPS, owner=owner, group=batch_id)
    status = batch_status(jobs, batch_id, LOCAL_PRINCIPAL if principal is None else principal)
    return {**status, "mode": mode}
