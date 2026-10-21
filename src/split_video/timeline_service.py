"""Headless operations on named timeline projects.

The glue between `timeline_ops` (pure editing rules) and a `ProjectStore`: it
loads a project, applies one change, saves it, and probes sources where the
rules need their duration. The editor's `/api/compose/*` endpoints, the
`compose` CLI subcommands and the MCP tools all call these functions, so the
three front doors can't drift apart. Failures are `TimelineError` subclasses
(see `timeline.py`); each caller maps them to its own error style.

Clips on the video track are addressed by 1-based position in the CLI and MCP
(`clip_id_at`), like segment numbers in the split editor, and by id in the API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from split_video import assets, captions, timeline, timeline_ops
from split_video import store as store_module
from split_video.ffmpeg import ProbeError
from split_video.jobs import CancelToken
from split_video.media_store import MediaError, MediaOutsideRootError, MediaStore
from split_video.presets import resolve_export
from split_video.render import render
from split_video.store import ProjectStore
from split_video.timeline import (
    OutputExistsError,
    OutsideRootError,
    Project,
    SourceNotFoundError,
    TimelineError,
)


def source_duration(media: MediaStore, source: str) -> float | None:
    """Duration of a source file by path, or None if it's missing or unreadable."""
    if not source:  # text clips have no source
        return None
    try:
        return media.probe(timeline.source_media_id(media, source)).duration
    except (TimelineError, MediaError, OSError, ProbeError):
        return None


def clip_source_duration(book: assets.AssetBook, clip: timeline.Clip) -> float | None:
    """Duration of a media clip's source file, found by asset id; None for a text clip or a missing file."""
    if clip.kind != "media":
        return None
    found = assets.lookup(book, clip.asset, clip.source)
    return None if found is None else found.asset.duration


def _asset_for_new_clip(media: MediaStore, path: str | None, asset: str | None) -> assets.Asset:
    """The asset a new clip uses, named by exactly one of a `path` (registered if new) or an asset id."""
    if (path is None) == (asset is None):
        raise TimelineError("give exactly one of a file path or an asset id")
    try:
        if asset is not None:
            return assets.get_asset(media, asset)
        assert path is not None
        return assets.register_path(media, path)
    except MediaOutsideRootError:
        raise OutsideRootError(f"{path!r} is outside the project directory") from None
    except MediaError:
        raise SourceNotFoundError(f"file not found: {path}") from None
    except ProbeError as exc:
        raise TimelineError(str(exc)) from None


# --- loading / saving -------------------------------------------------------


def load_existing(
    store: ProjectStore, name: str, media: MediaStore | None = None, *, base_revision: int | None = None
) -> Project:
    """The project `name` (`Project.revision` is its stored revision); `ProjectNotFoundError` if it
    doesn't exist. With `base_revision`, `RevisionConflictError` unless that is still the revision, which
    is how a front door that turns clip numbers into ids does so against the version the caller saw.

    With `media`, its clips are linked to assets (see `assets.link_project`): a version 1 project
    gains asset ids and a moved file is found again. The result is the version 2 model, but the
    project file is not rewritten by reading; the next edit saves it, or `migrate_project` does."""
    project = store.load_existing_timeline(name)
    store_module.check_base_revision("timeline project", name, project.revision, base_revision)
    return project if media is None else replace(assets.link_project(media, project), revision=project.revision)


def migrate_project(store: ProjectStore, media: MediaStore, name: str, *, base_revision: int | None = None) -> Project:
    """Link `name`'s clips to assets and save the project now, rewriting a version 1 file as
    version 2. A clip whose file is missing keeps its `source` and stays unlinked."""
    return store.update_timeline(name, lambda p: assets.link_project(media, p), base_revision)


def create_project(store: ProjectStore, name: str) -> Project:
    """Create an empty project (revision 0)."""
    return store.create_timeline(name)


def edit(
    store: ProjectStore,
    name: str,
    change: Callable[[Project], Project],
    media: MediaStore | None = None,
    *,
    base_revision: int | None = None,
) -> Project:
    """Load `name`, apply `change`, save and return the result (stamped with its new revision).

    The load-modify-save runs under the store's write lock (across processes for the local store), so
    concurrent edits never lose each other. With `base_revision` the edit only applies if the project
    is still at that revision, else `RevisionConflictError` and nothing is written; without it, the
    edit applies to the latest version. Given `media`, the clips are linked to assets first, so the
    first edit of a version 1 project saves it as version 2."""

    def apply(project: Project) -> Project:
        return change(project if media is None else assets.link_project(media, project))

    return store.update_timeline(name, apply, base_revision)


# --- operations -------------------------------------------------------------


def video_track(project: Project) -> timeline.Track:
    track = next((t for t in project.tracks if t.kind == "video"), None)
    if track is None:
        raise TimelineError("the project has no video track")
    return track


def track_at(project: Project, position: int | None) -> timeline.Track:
    """The track at 1-based `position` in the project's track list, or the
    main video track when `position` is None."""
    if position is None:
        return video_track(project)
    if not 1 <= position <= len(project.tracks):
        raise TimelineError(f"no track {position}; the project has {len(project.tracks)}")
    return project.tracks[position - 1]


def clip_id_at(project: Project, position: int, track: int | None = None) -> str:
    """The id of the clip at 1-based `position` on a track (default: the main video track)."""
    clips = track_at(project, track).clips
    if not 1 <= position <= len(clips):
        raise TimelineError(f"no clip {position}; the track has {len(clips)}")
    return clips[position - 1].id


def add_clip(
    store: ProjectStore,
    media: MediaStore,
    name: str,
    path: str | None = None,
    *,
    asset: str | None = None,
    track_id: str | None = None,
    index: int | None = None,
    start: float | None = None,
    in_point: float = 0.0,
    out_point: float | None = None,
    base_revision: int | None = None,
) -> Project:
    """Add a clip of the file at `path` or of the asset with id `asset` (exactly one of them).

    A path is registered in the asset table; the clip stores the asset id, and `source` as a hint of
    the asset's path (which is the first registered one if the same bytes are in the workspace twice)."""
    record = _asset_for_new_clip(media, path, asset)
    info = record.info
    label = path if path is not None else record.path
    with assets.open_book(media) as book:  # the hint is the path as named, if it's a known copy
        source = book.source_for(record, media.resolve(path)) if path is not None else record.path

    def change(project: Project) -> Project:
        target = timeline_ops.find_track(project, track_id) if track_id else video_track(project)
        if target.kind == "video" and info.width is None:
            raise TimelineError(f"{label} has no video stream, so it can't go on a video track")
        if target.kind == "audio" and not info.has_audio:
            raise TimelineError(f"{label} has no audio stream, so it can't go on an audio track")
        new, _ = timeline_ops.add_clip(
            project,
            target.id,
            source,
            info.duration,
            in_point=in_point,
            out_point=out_point,
            index=index,
            start=start,
            asset=record.id,
        )
        return new

    return edit(store, name, change, media, base_revision=base_revision)


def trim_clip(
    store: ProjectStore,
    media: MediaStore,
    name: str,
    clip_id: str,
    *,
    in_point: float | None = None,
    out_point: float | None = None,
    base_revision: int | None = None,
) -> Project:
    def change(project: Project) -> Project:
        track, i = timeline_ops.find_clip(project, clip_id)
        clip = track.clips[i]
        if clip.kind == "text":
            duration: float | None = float("inf")
        else:
            with assets.open_book(media) as book:
                duration = clip_source_duration(book, clip)
        if duration is None:
            raise TimelineError("the clip's source file is missing or unreadable")
        return timeline_ops.trim_clip(project, clip_id, duration, in_point=in_point, out_point=out_point)

    return edit(store, name, change, media, base_revision=base_revision)


def add_text_clip(
    store: ProjectStore,
    name: str,
    track_id: str,
    text: str,
    duration: float,
    *,
    media: MediaStore | None = None,
    base_revision: int | None = None,
    **options: Any,
) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_text_clip(project, track_id, text, duration, **options)
        return new

    return edit(store, name, change, media, base_revision=base_revision)


def set_clip_properties(
    store: ProjectStore,
    name: str,
    clip_id: str,
    *,
    media: MediaStore | None = None,
    base_revision: int | None = None,
    **properties: float | bool | dict[str, float] | None,
) -> Project:
    return edit(
        store, name, lambda p: timeline_ops.set_clip_properties(p, clip_id, **properties), media,
        base_revision=base_revision,
    )  # fmt: skip


def set_clip_crop(
    store: ProjectStore,
    name: str,
    clip_id: str,
    *,
    media: MediaStore,
    crop: dict[str, float] | None = None,
    aspect: str | None = None,
    center_x: float = 0.5,
    center_y: float = 0.5,
    zoom: float = 1.0,
    clear: bool = False,
    base_revision: int | None = None,
) -> Project:
    """Crop a clip to a region of its source frame. Give exactly one of an explicit `crop`
    (`{"x","y","w","h"}`, fractions of the frame), an `aspect` like "9:16" (the largest region of that
    shape, centred on `center_x`/`center_y` and zoomed in by `zoom`, computed by `timeline_ops.crop_for_aspect`
    from the clip's source size) or `clear` (back to the full frame)."""
    if sum([crop is not None, aspect is not None, clear]) != 1:
        raise TimelineError("give exactly one of an explicit crop, an aspect, or clear")

    def change(project: Project) -> Project:
        region = crop
        if aspect is not None:
            track, i = timeline_ops.find_clip(project, clip_id)
            clip = track.clips[i]
            if clip.kind != "media" or track.kind != "video":
                raise timeline_ops.RuleViolation(
                    "crop_not_applicable", "only media clips on video tracks can be cropped"
                )
            with assets.open_book(media) as book:
                found = assets.lookup(book, clip.asset, clip.source)
            if found is None or not found.asset.width or not found.asset.height:
                raise TimelineError("the clip's source file is missing or has no picture")
            aspect_w, aspect_h = timeline_ops.parse_aspect(aspect)
            region = timeline_ops.crop_for_aspect(
                found.asset.width, found.asset.height, aspect_w, aspect_h, center_x, center_y, zoom
            )
        return timeline_ops.set_clip_properties(project, clip_id, crop=region)

    return edit(store, name, change, media, base_revision=base_revision)


def set_text_properties(
    store: ProjectStore,
    name: str,
    clip_id: str,
    *,
    media: MediaStore | None = None,
    base_revision: int | None = None,
    **properties: str | float | None,
) -> Project:
    return edit(
        store, name, lambda p: timeline_ops.set_text_properties(p, clip_id, **properties), media,
        base_revision=base_revision,
    )  # fmt: skip


def add_track(
    store: ProjectStore, name: str, kind: str, media: MediaStore | None = None, *, base_revision: int | None = None
) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_track(project, kind)
        return new

    return edit(store, name, change, media, base_revision=base_revision)


def move_track(
    store: ProjectStore,
    name: str,
    track_id: str,
    to_index: int,
    media: MediaStore | None = None,
    *,
    base_revision: int | None = None,
) -> Project:
    return edit(
        store, name, lambda p: timeline_ops.move_track(p, track_id, to_index), media, base_revision=base_revision
    )


def remove_track(
    store: ProjectStore, name: str, track_id: str, media: MediaStore | None = None, *, base_revision: int | None = None
) -> Project:
    return edit(store, name, lambda p: timeline_ops.remove_track(p, track_id), media, base_revision=base_revision)


def place_clip(
    store: ProjectStore,
    name: str,
    clip_id: str,
    *,
    start: float | None = None,
    track_id: str | None = None,
    media: MediaStore | None = None,
    base_revision: int | None = None,
) -> Project:
    return edit(
        store, name, lambda p: timeline_ops.place_clip(p, clip_id, start=start, track_id=track_id), media,
        base_revision=base_revision,
    )  # fmt: skip


def move_clip(
    store: ProjectStore,
    name: str,
    clip_id: str,
    to_index: int,
    media: MediaStore | None = None,
    *,
    base_revision: int | None = None,
) -> Project:
    return edit(store, name, lambda p: timeline_ops.move_clip(p, clip_id, to_index), media, base_revision=base_revision)


def remove_clip(
    store: ProjectStore, name: str, clip_id: str, media: MediaStore | None = None, *, base_revision: int | None = None
) -> Project:
    return edit(store, name, lambda p: timeline_ops.remove_clip(p, clip_id), media, base_revision=base_revision)


def set_transition(
    store: ProjectStore,
    name: str,
    clip_id: str,
    transition: str,
    duration: float = 0.0,
    media: MediaStore | None = None,
    *,
    base_revision: int | None = None,
) -> Project:
    return edit(
        store, name, lambda p: timeline_ops.set_transition(p, clip_id, transition, duration), media,
        base_revision=base_revision,
    )  # fmt: skip


# --- rendering ---------------------------------------------------------------


def plan_render(
    store: ProjectStore,
    media: MediaStore,
    name: str,
    output: str | None,
    overwrite: bool,
    width: int | None = None,
    height: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
    caption_request: captions.CaptionRequest | None = None,
) -> tuple[Project, str, str]:
    """Check a render request and return the project, output filename and the
    output's media id (a plain filename is a file in the root). `width`/`height`
    (both or neither, see `render.check_size`), the export `preset` and the `fit` are checked here so a
    bad one fails before any job starts (`caption_request`, from `captions.request`, was checked when it was built;
    only its track numbers depend on the project)."""
    resolve_export(preset, width, height, fit)
    project = load_existing(store, name, media)
    filename = output or f"{name}-render.mp4"
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise TimelineError("output must be a plain filename")
    if not filename.lower().endswith(".mp4"):
        raise TimelineError("output must end in .mp4")
    if media.exists(filename) and not overwrite:
        raise OutputExistsError(f"{filename} already exists")
    if not any(t.clips for t in project.tracks):
        raise TimelineError("the timeline has no clips to render")
    if caption_request is not None:
        captions.caption_tracks(project, caption_request)  # a track number the timeline doesn't have fails here
    return project, filename, filename


def run_render(
    project: Project,
    media: MediaStore,
    out_id: str,
    progress: Callable[[float], None] | None = None,
    cancel: CancelToken | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
    caption_request: captions.CaptionRequest | None = None,
) -> dict[str, Any]:
    """Render and return what was used (`render.render`'s `{preset, fit, width, height, fps, captions}`).
    `caption_request` comes from `captions.request`, which validates the caption options."""
    return render(
        project, media, out_id, progress, cancel, width=width, height=height, preset=preset, fit=fit,
        captions=caption_request,
    )  # fmt: skip


def render_result(name: str, filename: str, project: Project, used: dict[str, Any]) -> dict[str, Any]:
    """The JSON every surface (job result, CLI --json, MCP tool) reports for a finished render."""
    return {"name": name, "output": filename, "duration": project.duration, **used}


# --- describing ---------------------------------------------------------------


def describe(media: MediaStore, name: str, project: Project, *, read_only: bool = False) -> dict[str, Any]:
    """JSON-friendly view of a project, with clips numbered from 1 per track.

    Each media clip shows its `asset` id and its file's current path as `source` (found by
    asset id, so a renamed file shows where it is now); an unresolvable clip shows its hint.

    With `read_only` the asset table is only read (`assets.peek`): no file is relocated, hashed or registered,
    so the table is never written; the path shown is the recorded one. Dry runs use it."""
    find = assets.peek if read_only else assets.lookup
    with assets.open_book(media) as book:
        rows = {c.id: find(book, c.asset, c.source) for t in project.tracks for c in t.clips if c.kind == "media"}
    return {
        "name": name,
        "revision": project.revision,
        "duration": project.duration,
        "tracks": [
            {
                "index": n + 1,
                "id": t.id,
                "kind": t.kind,
                "magnetic": t.magnetic,
                "clips": [
                    {
                        "index": i + 1,
                        "id": c.id,
                        "asset": rows[c.id].asset.id if rows.get(c.id) else c.asset,
                        "source": rows[c.id].source if rows.get(c.id) else c.source,
                        "in": c.in_point,
                        "out": c.out_point,
                        "start": c.start,
                        "end": c.end,
                        "duration": c.duration,
                        "source_duration": rows[c.id].asset.duration if rows.get(c.id) else None,
                        "gain": c.gain,
                        "muted": c.muted,
                        "transition": c.transition,
                        "transition_duration": c.transition_duration,
                        "kind": c.kind,
                        "text": c.text,
                        "font_size": c.font_size,
                        "color": c.color,
                        "x": c.x,
                        "y": c.y,
                        "scale": c.scale,
                        "opacity": c.opacity,
                        "brightness": c.brightness,
                        "contrast": c.contrast,
                        "saturation": c.saturation,
                        "crop": c.crop,
                    }
                    for i, c in enumerate(t.clips)
                ],
            }
            for n, t in enumerate(project.tracks)
        ],
    }
