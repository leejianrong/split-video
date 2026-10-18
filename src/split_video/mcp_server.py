"""MCP server: lets an agent drive split-video's project file directly.

A thin adapter over `split_video.project` — the same functions the browser
editor and the CLI subcommands call — so there is no running `edit` server
to start or keep in sync. Every tool reads and writes the video's
`<video>.split-video-project.json` sidecar; whatever an agent does here is
what the editor shows the next time it opens that video, and vice versa.

Run it over stdio with `split-video mcp [--root DIR]`. Tools only touch
files under `--root` (default: the current directory); a path that resolves
outside it is refused. Segment and split numbers are 1-based, as in the
editor's segment table (split 1 is the cut between segments 1 and 2).

Revisions: every project (split or timeline) has a revision number, shown as
`revision` in every result and incremented by each save. Editing tools take an
optional `base_revision`. If you pass it and the project has changed since (a
human in the editor, another agent), the tool fails with a JSON error
`{"error": "revision_conflict", "current_revision": N, "base_revision": M,
"message": ...}` and writes nothing: re-read the project, re-apply your change
and retry with base_revision=N. If you omit it the edit is applied to the
latest version (atomically, so it never loses another writer's change, but it
isn't checked against what you last saw).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from split_video import assets, project, timeline, timeline_service
from split_video.editor.cache import ClassificationCache
from split_video.editor.classify import ModelNotFoundError
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import MediaNotFoundError, MediaOutsideRootError, MediaStore
from split_video.store import ProjectStore
from split_video.timeline import TimelineError

_EXPECTED_ERRORS = (
    project.ProjectError,
    TimelineError,
    FfmpegNotFoundError,
    ProbeError,
    ExtractError,
    ModelNotFoundError,
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_EDITS_PROJECT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_REPLACES_PROJECT = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Split a long recording of back-to-back songs into one file per song.

Typical flow: list_videos -> propose_splits (apply=true saves them) ->
list_segments to review -> add_split / move_split / remove_split /
label_segment to fix them -> export. analyze_audio adds a music/speech/
applause classification (slow on long recordings) to help judge boundaries.

Videos are paths relative to the server's root directory. Segments and
splits are numbered from 1; split N is the cut between segments N and N+1.
Times are seconds. Edits are saved immediately to the video's
.split-video-project.json file, which the visual editor also reads.

Every project has a `revision` (in each result). Pass it back as `base_revision`
on an edit to be sure nobody changed the project since you read it: on a
revision_conflict error, re-read, re-apply your change and retry with the
current_revision from the error. Without base_revision an edit applies to the
latest version.

Separately, timeline projects combine clips from several videos into one:
list_timelines / create_timeline -> add_clip_to_timeline (video paths as
for list_videos) -> trim_timeline_clip / move_timeline_clip /
remove_timeline_clip / set_timeline_transition -> show_timeline to review ->
render_timeline. Clips are numbered from 1 in track order.

Clips reference media by asset id (a_ plus 16 hex digits), not path: list_assets shows
the ids, paths and stream info of every file a timeline has used, and
add_clip_to_timeline takes either a video path or an asset id. Timeline results show
each clip's asset id and its file's current path, so a renamed file is found again.

For overlays, titles and audio, add_timeline_track makes extra tracks (numbered as
in show_timeline; track 1 is the main video track); add_clip_to_timeline with
track/start places clips on them, place_timeline_clip moves them,
set_timeline_clip sets volume, position, opacity and colour, and
add_timeline_text puts titles over the picture. Later video tracks draw on top.
"""


def conflict_text(exc: RevisionConflictError) -> str:
    """The tool error for a stale write: JSON, so an agent can read `current_revision` without parsing prose."""
    return json.dumps(
        {
            "error": "revision_conflict",
            "current_revision": exc.current,
            "base_revision": exc.base,
            "message": (
                f"{exc.kind} {exc.project!r} is at revision {exc.current}, but your change was based on revision "
                f"{exc.base}, so nothing was written. Re-read the project (list_segments or show_timeline), "
                f"re-apply your change on top of the latest version, and retry with base_revision={exc.current}."
            ),
        }
    )


def create_server(root: Path, store: ProjectStore | None = None, media: MediaStore | None = None) -> MCPServer:
    """`store` and `media` default to local-disk stores under `root` (sidecar
    files next to the videos); tests pass in-memory ones."""
    root = root.resolve()
    store = store if store is not None else LocalProjectStore(root)
    media = media if media is not None else LocalMediaStore(root)
    server = MCPServer("split-video", instructions=INSTRUCTIONS)

    def resolve(video: str) -> str:
        """A video's canonical media id (also its id in the project store)."""
        try:
            media_id = media.resolve_user_path(video)
        except MediaOutsideRootError:
            raise ToolError(f"{video} is outside the allowed root directory") from None
        if not media.is_file(media_id):
            raise ToolError(f"video not found: {video}")
        return media_id

    def current_segments(source: str) -> list:
        return project.current_segments(store, media, source)

    def edit(source: str, change, base_revision: int | None):
        """Apply `change` to the saved segments atomically; returns them and the new revision."""
        saved = project.edit_segments(store, media, source, change, base_revision)
        return saved.value, saved.revision

    async def run(fn, *args, **kwargs):
        """Run blocking work (ffmpeg, YAMNet) off the event loop, turning the
        anticipated failures into a tool error the model can read."""
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except RevisionConflictError as exc:
            raise ToolError(conflict_text(exc)) from None
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from None

    def result(source: str, segments: list, revision: int, **extra: Any) -> dict[str, Any]:
        return {
            "video": source,
            "revision": revision,
            **extra,
            "segments": project.describe_segments(segments),
        }

    @server.tool(annotations=_READ_ONLY)
    async def list_videos(directory: str = "") -> dict[str, Any]:
        """List the videos and folders under `directory` (relative to the root; empty = the root),
        noting which videos already have saved splits."""
        try:
            listing = await anyio.to_thread.run_sync(media.list_dir, directory)
        except MediaOutsideRootError:
            raise ToolError(f"{directory} is outside the allowed root directory") from None
        except MediaNotFoundError:
            raise ToolError(f"directory not found: {directory}") from None
        return {
            "directory": listing.cwd,
            "entries": [
                {
                    "path": e.path,
                    "type": "directory" if e.is_dir else "video",
                    **({} if e.is_dir else {"has_project": project.has_project(store, e.path)}),
                }
                for e in listing.entries
            ],
        }

    @server.tool(annotations=_READ_ONLY)
    async def list_segments(video: str) -> dict[str, Any]:
        """Show a video's saved segments (start/end/label/color/included/export_name), or the whole
        recording as one segment with saved=false if nothing has been saved yet."""
        source = resolve(video)
        saved = project.has_project(store, source)
        loaded = await run(project.load_versioned, store, media, source)
        return result(source, loaded.value, loaded.revision, saved=saved)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def propose_splits(
        video: str,
        silence_threshold: float = -35.0,
        min_silence_duration: float = 2.0,
        min_song_length: float = 30.0,
        padding: float = 0.15,
        apply: bool = False,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Propose splits by detecting silent gaps between songs. With apply=false (default) this only
        returns the proposal; apply=true saves it as the project, REPLACING any saved splits, labels and
        exclusions. silence_threshold is in dB; the other numbers are seconds. If it finds too few or too
        many songs, adjust silence_threshold (try -30 to -40) or min_song_length and call again. With apply=true,
        `base_revision` (optional) makes the save fail with revision_conflict if the project changed since."""
        source = resolve(video)
        segments = await run(
            project.propose_segments,
            media,
            source,
            silence_threshold=silence_threshold,
            min_silence_duration=min_silence_duration,
            min_song_length=min_song_length,
            padding=padding,
        )
        if apply:
            revision = await run(project.save_segments, store, source, segments, base_revision)
        else:
            revision = await run(project.current_revision, store, source)
        return result(source, segments, revision, applied=apply)

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_split(video: str, at: float, base_revision: int | None = None) -> dict[str, Any]:
        """Cut the segment containing time `at` (seconds) in two. Both halves keep the label, color and
        include flag. Returns the new split's number. `base_revision` (optional): fail with
        revision_conflict instead of editing if the project has changed since that revision."""
        source = resolve(video)
        found: dict[str, int] = {}

        def change(segments: list) -> list:
            new, found["split"] = project.add_split(segments, at)
            return new

        segments, revision = await run(edit, source, change, base_revision)
        return result(source, segments, revision, split=found["split"])

    @server.tool(annotations=_EDITS_PROJECT)
    async def move_split(video: str, split: int, to: float, base_revision: int | None = None) -> dict[str, Any]:
        """Move split number `split` to time `to` (seconds). It stops short of its neighbors instead of
        crossing them; `applied_to` is where it actually landed. `base_revision` (optional): fail with
        revision_conflict instead of editing if the project has changed since that revision."""
        source = resolve(video)
        found: dict[str, float] = {}

        def change(segments: list) -> list:
            new, found["applied"] = project.move_split(segments, split, to)
            return new

        segments, revision = await run(edit, source, change, base_revision)
        return result(source, segments, revision, split=split, applied_to=found["applied"])

    @server.tool(annotations=_REPLACES_PROJECT)
    async def remove_split(video: str, split: int, base_revision: int | None = None) -> dict[str, Any]:
        """Remove split number `split`, merging segments `split` and `split`+1 (the earlier one's label,
        color and name win). `base_revision` (optional): fail with revision_conflict instead of editing
        if the project has changed since that revision."""
        source = resolve(video)
        segments, revision = await run(
            edit, source, lambda current: project.remove_split(current, split), base_revision
        )
        return result(source, segments, revision, split=split)

    @server.tool(annotations=_EDITS_PROJECT)
    async def label_segment(
        video: str,
        segment: int,
        label: str | None = None,
        color: str | None = None,
        included: bool | None = None,
        export_name: str | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set a segment's metadata. Only the fields you pass change. `label` is display text; `color` is
        '#rrggbb'; `included` says whether export writes it; `export_name` is the output filename without
        extension. Pass an empty string for `color` or `export_name` to clear it. `base_revision` (optional):
        fail with revision_conflict instead of editing if the project has changed since that revision."""
        if label is None and color is None and included is None and export_name is None:
            raise ToolError("pass at least one of label, color, included, export_name")
        source = resolve(video)

        def change(current: list) -> list:
            return project.update_segment(
                current,
                segment,
                label=label,
                color=project.UNSET if color is None else color,
                included=included,
                export_name=project.UNSET if export_name is None else export_name,
            )

        segments, revision = await run(edit, source, change, base_revision)
        return result(source, segments, revision, segment=segment)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    async def analyze_audio(video: str, force: bool = False) -> dict[str, Any]:
        """Classify the audio (music, singing, speech, applause, laughter, silence/noise) with YAMNet and
        cache the result next to the video. Returns seconds per bucket and the labeled time regions, which
        help judge whether a proposed split falls between songs or inside one. Can take minutes on a long
        recording; a cached result returns instantly unless force=true. Needs the YAMNet model."""
        source = resolve(video)

        def work() -> dict[str, Any]:
            cache = ClassificationCache(media, source)
            if force or not cache.is_analyzed:
                project.run_analysis(media, source, cache, cache.get_thresholds())
            return project.summarize_classification(cache)

        summary = await run(work)
        return {"video": source, **summary}

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    async def export(
        video: str,
        only: list[int] | None = None,
        output_format: str | None = None,
        precise: bool = False,
        overwrite: bool = False,
        manifest: bool = True,
    ) -> dict[str, Any]:
        """Write the segments as separate files into '<video name>_split/' next to the video. By default
        writes the segments marked included; `only` (segment numbers) writes exactly those instead.
        precise=true re-encodes for frame-accurate cuts (slow); the default is a fast stream copy that can
        land a second or two off a keyframe. Refuses to replace existing files unless overwrite=true."""
        source = resolve(video)

        def work() -> tuple[project.ExportPlan, list[str], str | None]:
            chosen = project.select_for_export(current_segments(source), only)
            plan = project.plan_export(
                media,
                source,
                [(s.start, s.end, s.export_name) for s in chosen],
                output_format=output_format,
                overwrite=overwrite,
            )
            written, manifest_path = project.run_export(
                media, plan, precise=precise, output_format=output_format, write_manifest_file=manifest
            )
            return plan, written, manifest_path

        plan, written, manifest_path = await run(work)
        return {
            "video": source,
            "output_dir": plan.output_dir,
            "files": written,
            "manifest": manifest_path,
        }

    # --- timeline (multi-clip) projects ---------------------------------------

    def timeline_result(name: str, project_: timeline.Project, **extra: Any) -> dict[str, Any]:
        return {**extra, **timeline_service.describe(media, name, project_)}

    # Clip and track numbers are resolved against the version the caller saw: with a base_revision that has
    # since been superseded the lookup itself fails with a conflict, since the number could mean another clip.
    def timeline_clip(name: str, clip: int, track: int | None = None, base_revision: int | None = None) -> str:
        loaded = timeline_service.load_existing(store, name, media, base_revision=base_revision)
        return timeline_service.clip_id_at(loaded, clip, track)

    def timeline_track(name: str, track: int | None, base_revision: int | None = None) -> str:
        loaded = timeline_service.load_existing(store, name, media, base_revision=base_revision)
        return timeline_service.track_at(loaded, track).id

    @server.tool(annotations=_READ_ONLY)
    async def list_timelines() -> dict[str, Any]:
        """List the timeline projects (multi-clip compositions) in the root."""
        return {"projects": await run(store.list_timelines)}

    @server.tool(annotations=_EDITS_PROJECT)
    async def create_timeline(name: str) -> dict[str, Any]:
        """Create an empty timeline project called `name` (no extension)."""
        return timeline_result(name, await run(timeline_service.create_project, store, name))

    @server.tool(annotations=_READ_ONLY)
    async def show_timeline(name: str) -> dict[str, Any]:
        """The timeline's tracks and clips: sources, in/out points, where each starts, transitions."""
        project_ = await run(lambda: timeline_service.load_existing(store, name, media))
        return timeline_result(name, project_)

    @server.tool(annotations=_READ_ONLY)
    async def list_assets() -> dict[str, Any]:
        """List the assets: every media file a timeline has used, with its asset id (stable, derived
        from the file's content, so it survives renames), current path, duration, size and stream
        info (width, height, fps, has_audio). `present` is false if the file is missing."""

        def work() -> list[dict[str, Any]]:
            with assets.open_book(media) as book:
                return [a.summary(book.present(a)) for a in book.list()]

        return {"assets": await run(work)}

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_clip_to_timeline(
        name: str,
        video: str | None = None,
        position: int | None = None,
        in_point: float = 0.0,
        out_point: float | None = None,
        track: int | None = None,
        start: float | None = None,
        asset: str | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Add a clip of `video` (path relative to the root; an audio file for an audio track) or of the
        asset with id `asset` (from list_assets): give exactly one of the two. It goes on a
        track, using seconds `in_point`..`out_point` of it (default: all). `track` is a track number
        from show_timeline (default: the main video track). On the main track `position` (from 1)
        inserts it there, default the end; on any other track `start` (seconds) places it, default
        after the last clip. `base_revision` (optional): fail with revision_conflict instead of editing if
        the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.add_clip(
                store,
                media,
                name,
                video,
                asset=asset,
                track_id=None if track is None else timeline_track(name, track, base_revision),
                index=None if position is None else position - 1,
                start=start,
                in_point=in_point,
                out_point=out_point,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def trim_timeline_clip(
        name: str,
        clip: int,
        in_point: float | None = None,
        out_point: float | None = None,
        track: int | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Change which part of its source clip number `clip` (on `track`, default the main video
        track) uses, in seconds into the source. Trimming the left edge of a clip on a free track
        moves its start so the content stays where it is. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        if in_point is None and out_point is None:
            raise ToolError("pass in_point and/or out_point")
        project_ = await run(
            lambda: timeline_service.trim_clip(
                store,
                media,
                name,
                timeline_clip(name, clip, track, base_revision),
                in_point=in_point,
                out_point=out_point,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def move_timeline_clip(
        name: str, clip: int, to_position: int, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Move clip number `clip` to `to_position` (both from 1). `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.move_clip(
                store,
                name,
                timeline_clip(name, clip, None, base_revision),
                to_position - 1,
                media,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def remove_timeline_clip(
        name: str, clip: int, track: int | None = None, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Remove clip number `clip` (on `track`, default the main video track) from the timeline;
        the source file is untouched. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.remove_clip(
                store, name, timeline_clip(name, clip, track, base_revision), media, base_revision=base_revision
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def set_timeline_transition(
        name: str, clip: int, transition: str, duration: float = 1.0, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Set how clip number `clip` joins the clip before it: transition "cut" or "crossfade"
        (`duration` seconds). The first clip has nothing to fade from. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.set_transition(
                store,
                name,
                timeline_clip(name, clip, None, base_revision),
                transition,
                0.0 if transition == "cut" else duration,
                media,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_timeline_track(name: str, kind: str = "video", base_revision: int | None = None) -> dict[str, Any]:
        """Add an empty track ("video" or "audio") on top of the stack. Clips on it are placed by start
        time and may overlap clips on other tracks; a later video track draws over earlier ones. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        return timeline_result(
            name, await run(lambda: timeline_service.add_track(store, name, kind, media, base_revision=base_revision))
        )

    @server.tool(annotations=_REPLACES_PROJECT)
    async def remove_timeline_track(name: str, track: int, base_revision: int | None = None) -> dict[str, Any]:
        """Remove track number `track` (from show_timeline) and its clips. The last video track stays. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.remove_track(
                store, name, timeline_track(name, track, base_revision), media, base_revision=base_revision
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def move_timeline_track(
        name: str, track: int, to_position: int, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Change track number `track`'s place in the stack to `to_position` (both from 1). `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        project_ = await run(
            lambda: timeline_service.move_track(
                store,
                name,
                timeline_track(name, track, base_revision),
                to_position - 1,
                media,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def place_timeline_clip(
        name: str,
        clip: int,
        track: int,
        start: float | None = None,
        to_track: int | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Move clip number `clip` on `track` to `start` seconds and/or onto `to_track` (same kind).
        Only for tracks that place clips by time, not the main track (use move_timeline_clip). `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        if start is None and to_track is None:
            raise ToolError("pass start and/or to_track")
        project_ = await run(
            lambda: timeline_service.place_clip(
                store,
                name,
                timeline_clip(name, clip, track, base_revision),
                start=start,
                track_id=None if to_track is None else timeline_track(name, to_track, base_revision),
                media=media,
                base_revision=base_revision,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def set_timeline_clip(
        name: str,
        clip: int,
        track: int | None = None,
        gain: float | None = None,
        muted: bool | None = None,
        x: float | None = None,
        y: float | None = None,
        scale: float | None = None,
        opacity: float | None = None,
        brightness: float | None = None,
        contrast: float | None = None,
        saturation: float | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set properties of clip number `clip` on `track` (default the main video track): volume
        `gain` (0..4, 1 = unchanged) and `muted`; for overlay clips `x`/`y` (centre, 0..1 of the frame),
        `scale` (width as a fraction of the frame, 0.05..4) and `opacity` (0..1); colour `brightness`
        (-1..1), `contrast` and `saturation` (0..3, 1 = unchanged). Only the values you pass change. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        values = {
            "gain": gain, "muted": muted, "x": x, "y": y, "scale": scale, "opacity": opacity,
            "brightness": brightness, "contrast": contrast, "saturation": saturation,
        }  # fmt: skip
        changes = {k: v for k, v in values.items() if v is not None}
        if not changes:
            raise ToolError("pass at least one property to change")
        project_ = await run(
            lambda: timeline_service.set_clip_properties(
                store,
                name,
                timeline_clip(name, clip, track, base_revision),
                media=media,
                base_revision=base_revision,
                **changes,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_timeline_text(
        name: str,
        text: str,
        track: int | None = None,
        start: float = 0.0,
        duration: float = 3.0,
        size: float = 0.08,
        color: str = "#ffffff",
        x: float = 0.5,
        y: float = 0.85,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Add a text title for `duration` seconds from `start`, on an overlay video track (`track` number
        from show_timeline; default the first overlay track, which add_timeline_track creates).
        `size` is the text height as a fraction of the frame, `color` is #rrggbb, `x`/`y` the centre. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""

        def work() -> timeline.Project:
            loaded = timeline_service.load_existing(store, name, media, base_revision=base_revision)
            if track is None:
                chosen = next((t for t in loaded.tracks if t.kind == "video" and not t.magnetic), None)
                if chosen is None:
                    raise TimelineError("no overlay video track; create one with add_timeline_track first")
                track_id = chosen.id
            else:
                track_id = timeline_service.track_at(loaded, track).id
            return timeline_service.add_text_clip(
                store,
                name,
                track_id,
                text,
                duration,
                media=media,
                base_revision=base_revision,
                start=start,
                font_size=size,
                color=color,
                x=x,
                y=y,
            )

        return timeline_result(name, await run(work))

    @server.tool(annotations=_EDITS_PROJECT)
    async def set_timeline_text(
        name: str,
        clip: int,
        track: int,
        text: str | None = None,
        size: float | None = None,
        color: str | None = None,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Change the words, `size` or `color` of text clip number `clip` on `track`. `base_revision` (optional): fail with revision_conflict instead of editing if the timeline has changed since that revision."""
        if text is None and size is None and color is None:
            raise ToolError("pass text, size and/or color")
        project_ = await run(
            lambda: timeline_service.set_text_properties(
                store,
                name,
                timeline_clip(name, clip, track, base_revision),
                media=media,
                base_revision=base_revision,
                text=text,
                font_size=size,
                color=color,
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def render_timeline(name: str, output: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Render the timeline to an mp4 in the root (default '<name>-render.mp4'). Refuses to replace an
        existing file unless overwrite=true. Re-encodes, so it can take a while for long clips."""

        def work() -> dict[str, Any]:
            project_, filename, out_id = timeline_service.plan_render(store, media, name, output, overwrite)
            timeline_service.run_render(project_, media, out_id)
            return {"name": name, "output": filename, "duration": project_.duration}

        return await run(work)

    return server
