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

Co-driving a project that is open in a live editor tab is deliberately not
handled yet: the tab keeps its own in-memory copy and would overwrite
changes made here on its next save. Close the tab (or reload it) first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from split_video import project, timeline, timeline_service
from split_video.editor.browse import PathEscapesRootError, list_directory, resolve_within_root
from split_video.editor.cache import ClassificationCache
from split_video.editor.classify import ModelNotFoundError
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
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

Separately, timeline projects combine clips from several videos into one:
list_timelines / create_timeline -> add_clip_to_timeline (video paths as
for list_videos) -> trim_timeline_clip / move_timeline_clip /
remove_timeline_clip / set_timeline_transition -> show_timeline to review ->
render_timeline. Clips are numbered from 1 in track order.
"""


def create_server(root: Path) -> MCPServer:
    root = root.resolve()
    server = MCPServer("split-video", instructions=INSTRUCTIONS)

    def resolve(video: str) -> Path:
        p = Path(video)
        if p.is_absolute():
            try:
                video = p.resolve().relative_to(root).as_posix()
            except ValueError:
                raise ToolError(f"{video} is outside the allowed root directory") from None
        try:
            path = resolve_within_root(root, video)
        except PathEscapesRootError:
            raise ToolError(f"{video} is outside the allowed root directory") from None
        if not path.is_file():
            raise ToolError(f"video not found: {video}")
        return path

    async def run(fn, *args, **kwargs):
        """Run blocking work (ffmpeg, YAMNet) off the event loop, turning the
        anticipated failures into a tool error the model can read."""
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from None

    def result(source: Path, segments: list, **extra: Any) -> dict[str, Any]:
        return {
            "video": source.relative_to(root).as_posix(),
            **extra,
            "segments": project.describe_segments(segments),
        }

    @server.tool(annotations=_READ_ONLY)
    async def list_videos(directory: str = "") -> dict[str, Any]:
        """List the videos and folders under `directory` (relative to the root; empty = the root),
        noting which videos already have saved splits."""
        try:
            listing = await anyio.to_thread.run_sync(list_directory, root, directory)
        except PathEscapesRootError:
            raise ToolError(f"{directory} is outside the allowed root directory") from None
        except (FileNotFoundError, NotADirectoryError):
            raise ToolError(f"directory not found: {directory}") from None
        return {
            "directory": listing.cwd,
            "entries": [
                {
                    "path": e.path,
                    "type": "directory" if e.is_dir else "video",
                    **({} if e.is_dir else {"has_project": project.has_project(root / e.path)}),
                }
                for e in listing.entries
            ],
        }

    @server.tool(annotations=_READ_ONLY)
    async def list_segments(video: str) -> dict[str, Any]:
        """Show a video's saved segments (start/end/label/color/included/export_name), or the whole
        recording as one segment with saved=false if nothing has been saved yet."""
        source = resolve(video)
        saved = project.has_project(source)
        segments = await run(project.current_segments, source)
        return result(source, segments, saved=saved)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def propose_splits(
        video: str,
        silence_threshold: float = -35.0,
        min_silence_duration: float = 2.0,
        min_song_length: float = 30.0,
        padding: float = 0.15,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Propose splits by detecting silent gaps between songs. With apply=false (default) this only
        returns the proposal; apply=true saves it as the project, REPLACING any saved splits, labels and
        exclusions. silence_threshold is in dB; the other numbers are seconds. If it finds too few or too
        many songs, adjust silence_threshold (try -30 to -40) or min_song_length and call again."""
        source = resolve(video)
        segments = await run(
            project.propose_segments,
            source,
            silence_threshold=silence_threshold,
            min_silence_duration=min_silence_duration,
            min_song_length=min_song_length,
            padding=padding,
        )
        if apply:
            await run(project.save_segments, source, segments)
        return result(source, segments, applied=apply)

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_split(video: str, at: float) -> dict[str, Any]:
        """Cut the segment containing time `at` (seconds) in two. Both halves keep the label, color and
        include flag. Returns the new split's number."""
        source = resolve(video)

        def work() -> tuple[list, int]:
            segments, split = project.add_split(project.current_segments(source), at)
            project.save_segments(source, segments)
            return segments, split

        segments, split = await run(work)
        return result(source, segments, split=split)

    @server.tool(annotations=_EDITS_PROJECT)
    async def move_split(video: str, split: int, to: float) -> dict[str, Any]:
        """Move split number `split` to time `to` (seconds). It stops short of its neighbors instead of
        crossing them; `applied_to` is where it actually landed."""
        source = resolve(video)

        def work() -> tuple[list, float]:
            segments, applied = project.move_split(project.current_segments(source), split, to)
            project.save_segments(source, segments)
            return segments, applied

        segments, applied = await run(work)
        return result(source, segments, split=split, applied_to=applied)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def remove_split(video: str, split: int) -> dict[str, Any]:
        """Remove split number `split`, merging segments `split` and `split`+1 (the earlier one's label,
        color and name win)."""
        source = resolve(video)

        def work() -> list:
            segments = project.remove_split(project.current_segments(source), split)
            project.save_segments(source, segments)
            return segments

        segments = await run(work)
        return result(source, segments, split=split)

    @server.tool(annotations=_EDITS_PROJECT)
    async def label_segment(
        video: str,
        segment: int,
        label: str | None = None,
        color: str | None = None,
        included: bool | None = None,
        export_name: str | None = None,
    ) -> dict[str, Any]:
        """Set a segment's metadata. Only the fields you pass change. `label` is display text; `color` is
        '#rrggbb'; `included` says whether export writes it; `export_name` is the output filename without
        extension. Pass an empty string for `color` or `export_name` to clear it."""
        if label is None and color is None and included is None and export_name is None:
            raise ToolError("pass at least one of label, color, included, export_name")
        source = resolve(video)

        def work() -> list:
            segments = project.update_segment(
                project.current_segments(source),
                segment,
                label=label,
                color=project.UNSET if color is None else color,
                included=included,
                export_name=project.UNSET if export_name is None else export_name,
            )
            project.save_segments(source, segments)
            return segments

        segments = await run(work)
        return result(source, segments, segment=segment)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    async def analyze_audio(video: str, force: bool = False) -> dict[str, Any]:
        """Classify the audio (music, singing, speech, applause, laughter, silence/noise) with YAMNet and
        cache the result next to the video. Returns seconds per bucket and the labeled time regions, which
        help judge whether a proposed split falls between songs or inside one. Can take minutes on a long
        recording; a cached result returns instantly unless force=true. Needs the YAMNet model."""
        source = resolve(video)

        def work() -> dict[str, Any]:
            cache = ClassificationCache(source)
            if force or not cache.is_analyzed:
                project.run_analysis(source, cache, cache.get_thresholds())
            return project.summarize_classification(cache)

        summary = await run(work)
        return {"video": source.relative_to(root).as_posix(), **summary}

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

        def work() -> tuple[project.ExportPlan, list[Path], Path | None]:
            chosen = project.select_for_export(project.current_segments(source), only)
            plan = project.plan_export(
                source,
                [(s.start, s.end, s.export_name) for s in chosen],
                output_format=output_format,
                overwrite=overwrite,
            )
            written, manifest_path = project.run_export(
                plan, precise=precise, output_format=output_format, write_manifest_file=manifest
            )
            return plan, written, manifest_path

        plan, written, manifest_path = await run(work)
        return {
            "video": source.relative_to(root).as_posix(),
            "output_dir": plan.output_dir.relative_to(root).as_posix(),
            "files": [p.relative_to(root).as_posix() for p in written],
            "manifest": manifest_path.relative_to(root).as_posix() if manifest_path else None,
        }

    # --- timeline (multi-clip) projects ---------------------------------------

    def timeline_result(name: str, project_: timeline.Project, **extra: Any) -> dict[str, Any]:
        return {**extra, **timeline_service.describe(root, name, project_)}

    def timeline_clip(name: str, clip: int) -> str:
        return timeline_service.clip_id_at(timeline_service.load_existing(root, name)[1], clip)

    @server.tool(annotations=_READ_ONLY)
    async def list_timelines() -> dict[str, Any]:
        """List the timeline projects (multi-clip compositions) in the root."""
        return {"projects": await run(timeline.list_projects, root)}

    @server.tool(annotations=_EDITS_PROJECT)
    async def create_timeline(name: str) -> dict[str, Any]:
        """Create an empty timeline project called `name` (no extension)."""
        return timeline_result(name, await run(timeline_service.create_project, root, name))

    @server.tool(annotations=_READ_ONLY)
    async def show_timeline(name: str) -> dict[str, Any]:
        """The timeline's tracks and clips: sources, in/out points, where each starts, transitions."""
        project_ = await run(lambda: timeline_service.load_existing(root, name)[1])
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def add_clip_to_timeline(
        name: str,
        video: str,
        position: int | None = None,
        in_point: float = 0.0,
        out_point: float | None = None,
    ) -> dict[str, Any]:
        """Add a clip of `video` (path relative to the root) to the timeline's video track, using
        seconds `in_point`..`out_point` of it (default: all). `position` (from 1) inserts it there;
        the default is the end."""
        project_ = await run(
            timeline_service.add_clip,
            root,
            name,
            video,
            index=None if position is None else position - 1,
            in_point=in_point,
            out_point=out_point,
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def trim_timeline_clip(
        name: str, clip: int, in_point: float | None = None, out_point: float | None = None
    ) -> dict[str, Any]:
        """Change which part of its source video clip number `clip` uses (seconds into the source)."""
        if in_point is None and out_point is None:
            raise ToolError("pass in_point and/or out_point")
        project_ = await run(
            lambda: timeline_service.trim_clip(
                root, name, timeline_clip(name, clip), in_point=in_point, out_point=out_point
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def move_timeline_clip(name: str, clip: int, to_position: int) -> dict[str, Any]:
        """Move clip number `clip` to `to_position` (both from 1)."""
        project_ = await run(lambda: timeline_service.move_clip(root, name, timeline_clip(name, clip), to_position - 1))
        return timeline_result(name, project_)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def remove_timeline_clip(name: str, clip: int) -> dict[str, Any]:
        """Remove clip number `clip` from the timeline (the source video is untouched)."""
        project_ = await run(lambda: timeline_service.remove_clip(root, name, timeline_clip(name, clip)))
        return timeline_result(name, project_)

    @server.tool(annotations=_EDITS_PROJECT)
    async def set_timeline_transition(name: str, clip: int, transition: str, duration: float = 1.0) -> dict[str, Any]:
        """Set how clip number `clip` joins the clip before it: transition "cut" or "crossfade"
        (`duration` seconds). The first clip has nothing to fade from."""
        project_ = await run(
            lambda: timeline_service.set_transition(
                root, name, timeline_clip(name, clip), transition, 0.0 if transition == "cut" else duration
            )
        )
        return timeline_result(name, project_)

    @server.tool(annotations=_REPLACES_PROJECT)
    async def render_timeline(name: str, output: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Render the timeline to an mp4 in the root (default '<name>-render.mp4'). Refuses to replace an
        existing file unless overwrite=true. Re-encodes, so it can take a while for long clips."""

        def work() -> dict[str, Any]:
            project_, filename, out_path = timeline_service.plan_render(root, name, output, overwrite)
            timeline_service.run_render(project_, root, out_path)
            return {"name": name, "output": filename, "duration": project_.duration}

        return await run(work)

    return server
