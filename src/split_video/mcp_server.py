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
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from split_video import (
    assets,
    contact_sheet,
    edit_ops,
    edit_plan,
    frames,
    highlights,
    library,
    media_report,
    ocr,
    presets,
    project,
    proxy,
    scenes,
    search,
    time_index,
    timeline,
    timeline_service,
    transcribe,
    transcript_search,
)
from split_video import captions as captions_module
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
    frames.FrameError,  # also the base of the contact-sheet errors
    transcribe.TranscriptionError,
    transcribe.TranscriptionUnavailableError,
    transcript_search.TranscriptNotFoundError,
    transcript_search.TranscriptQueryError,
    ocr.OcrError,
    ocr.OcrUnavailableError,
    ocr.OcrNotFoundError,
    ocr.OcrArgumentError,
    search.SearchNotFoundError,
    search.SearchQueryError,
    time_index.TimeIndexError,
    media_report.MediaReportError,
    highlights.HighlightsError,
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
render_timeline (`captions` burns in word-highlighted captions from the clips' transcripts: transcribe_asset
first, styles in list_caption_styles). Clips are numbered from 1 in track order.

Clips reference media by asset id (a_ plus 16 hex digits), not path: list_assets shows
the ids, paths and stream info of every file in the library (scan_library adds a folder's
media; a timeline clip's file is added too), and
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
        """List the library: every media file scanned or used by a timeline, with its asset id (stable, derived
        from the file's content, so it survives renames), current path, duration, size and stream
        info (width, height, fps, has_audio). `present` is false if the file is missing."""

        return {"assets": await run(library.listing, media)}

    @server.tool(annotations=_EDITS_PROJECT)
    async def scan_library(folder: str = "", proxies: bool = False) -> dict[str, Any]:
        """Scan `folder` (default: the whole workspace, recursively) and record every media file as
        an asset, so list_assets shows it with duration, size and stream info. Incremental: files
        already recorded and unchanged are not probed again. Returns the counts, the newly `added`
        assets (id and path), `copies` (paths whose bytes are already in the library) and `skipped`
        files that could not be read as media, each with a reason. With `proxies` true, each newly added
        video also gets its proxy and thumbnail strip made before this returns (slow: minutes per hour of
        video; see prepare_asset), listed under `proxies` (one prepare_asset result per video; the HTTP API
        queues these as jobs instead and returns their ids)."""
        try:
            report = (await run(library.scan, media, folder)).to_dict()
        except MediaOutsideRootError:
            raise ToolError(f"{folder} is outside the allowed root directory") from None
        except MediaNotFoundError:
            raise ToolError(f"folder not found: {folder}") from None
        if proxies:
            ids = [a["id"] for a in report["added"] if (await run(assets.get_asset, media, a["id"])).has_video]
            report["proxies"] = [await run(proxy.prepare, media, asset_id) for asset_id in ids]
        return report

    @server.tool(annotations=_EDITS_PROJECT)
    async def prepare_asset(asset: str, force: bool = False) -> dict[str, Any]:
        """Make the proxy and thumbnail strip of the library asset with id `asset` (from list_assets), if they
        are missing or stale (`force` rebuilds). The proxy is a small H.264 copy (640 px wide, 15 fps) that
        seeks instantly and stands in for the original in visual work; the strip is one JPEG of evenly
        spaced frames. Slow for a long recording (minutes per hour of video) and runs before this returns;
        the original is never changed and audio analysis still reads it. Returns `proxy` and `thumbnails`
        metadata (tile size, columns, rows, count and seconds per tile for the strip) and `made`, what this
        call produced (empty when everything was current). An audio-only asset has neither (`skipped`).
        list_assets shows `proxy` and `thumbnails` per asset."""
        return await run(proxy.prepare, media, asset, force=force)

    @server.tool(annotations=_EDITS_PROJECT)
    async def transcribe_asset(
        asset: str, model: str = transcribe.DEFAULT_MODEL, language: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Transcribe the speech of the library asset with id `asset` (from list_assets) into words with start and end
        times, stored with the asset; read them with transcript_range and
        transcript_search (or get_transcript for the whole of a short one). Runs locally (faster-whisper on the CPU) and
        before this returns: about 3 minutes per hour of audio with the default `model` "base.en" (English; "small.en"
        is more careful and 3 times slower; "base" or "small" handle other languages, with `language` a code like
        "fr", or unset to detect it). The first use downloads the model (140 MB) into the workspace. If a current
        transcript from this model exists nothing is redone unless `force`. Returns `model`, `language`,
        `duration`, `segments` and `words` (counts) and `made` (false when the stored one was current). Without the
        optional faster-whisper package this is a tool error saying how to install it."""
        return await run(transcribe.transcribe, media, asset, model=model, language=language, force=force)

    @server.tool(annotations=_READ_ONLY)
    async def get_transcript(asset: str, start: float | None = None, end: float | None = None) -> dict[str, Any]:
        """The stored transcript of the library asset `asset`: `model`, `language`, `duration` and `segments`, each
        with `start`, `end`, `text` and `words` (`start`, `end`, `word`, `probability`), all in seconds from the
        start of the asset's audio. With `start` and/or `end` only the words overlapping that span (a cut segment
        keeps only those words). This is the whole transcript at once: for a long recording use transcript_range (paged)
        and transcript_search. Run transcribe_asset first: without a transcript this is a tool error. list_assets
        shows `transcript` per asset."""
        found = await run(transcribe.load, media, asset)
        if found is None:
            raise ToolError(f"asset {asset} has no transcript yet: run transcribe_asset first")
        return transcribe.excerpt(found, start, end)

    @server.tool(annotations=_READ_ONLY)
    async def transcript_range(
        asset: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        granularity: str = "segment",
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Read the transcript of the library asset `asset` between `start` and `end` seconds (either may be left
        out), one page at a time, so a long recording never has to be loaded whole. Returns `items`: segments
        (`index`, `start`, `end`, `text`), or with `granularity` "word" words (`index`, `segment`, `start`, `end`,
        `word`, `probability`), those overlapping the range (a segment that starts before `start` but runs past it
        is included, whole). A page holds at most `limit` items (default 50 segments or 200 words; at most 500) and,
        with `max_chars`, stops before the item that would exceed that many characters. Also `total_in_range`,
        `covered` (first start and last end on this page), `range` and `next_cursor`: while it is not null, call
        again with the same arguments plus `cursor` to continue; every item appears exactly once. To find where
        something was said use transcript_search. Without a transcript this is a tool error: run transcribe_asset."""
        return await run(
            transcript_search.transcript_range,
            media,
            asset,
            start,
            end,
            limit=limit,
            cursor=cursor,
            granularity=granularity,
            max_chars=max_chars,
        )

    @server.tool(name="transcript_search", annotations=_READ_ONLY)
    async def search_transcript(
        asset: str,
        query: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Find where a word or phrase is said in the transcript of the library asset `asset`. Case-insensitive
        match on whole words, ignoring punctuation, also across segment boundaries (no regex, no fuzzy or semantic
        matching; at most 500 characters). Returns `hits` in time order, each with `time` (seconds, where the
        first matched word starts), `end`, `text` (as transcribed), `context_before` and `context_after` (`context`
        words each, default 8, at most 50), `segment` and `word` indices, plus `total_matches` and `next_cursor`
        (pass it back as `cursor` for the next page of at most `limit` hits, default 20, at most 200). `start` and
        `end` keep only hits whose first word overlaps that span. Read around a hit with transcript_range. Without
        a transcript this is a tool error: run transcribe_asset."""
        return await run(
            transcript_search.transcript_search,
            media,
            asset,
            query,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            context=context,
            case_sensitive=case_sensitive,
        )

    @server.tool(annotations=_READ_ONLY)
    async def get_frame(
        asset: str,
        time: float,
        width: int | None = None,
        height: int | None = None,
        format: str = "jpeg",
        source: str = "auto",
    ) -> list[Image | str]:
        """Look at the library asset `asset` (from list_assets) at `time` seconds: returns the picture as an image
        plus a short text block with `time` (the frame actually shown), `width`, `height`, `source` and `duration`.
        `width` and/or `height` (pixels, at most 4096) scale it keeping the aspect ratio, never beyond the source's
        size: a small image is cheaper to look at (try width 640). `format` is "jpeg" (default) or "png". `source`
        "auto" reads the asset's proxy when it has one (at most 640 px wide and 15 frames per second, so the frame is
        within 1/15 s of `time`); "original" reads the file itself, for fine detail such as small text. A time past
        the end of the asset, or an audio-only asset, is a tool error that says so."""
        try:
            frame = await run(
                frames.extract_frame, media, asset, time, width=width, height=height, format=format, source=source
            )
        except frames.FrameError as exc:
            raise ToolError(str(exc)) from None
        info = {k: v for k, v in frame.info().items() if k != "bytes"}
        return [Image(data=frame.data, format=frame.format), json.dumps(info)]

    @server.tool(annotations=_READ_ONLY)
    async def get_contact_sheet(
        asset: str,
        start: float = 0.0,
        end: float | None = None,
        count: int = 12,
        columns: int | None = None,
        tile_width: int = 320,
        format: str = "jpeg",
        source: str = "auto",
    ) -> list[Image | str]:
        """Skim a stretch of the library asset `asset` (from list_assets) in one picture: `count` frames (1..64,
        default 12) evenly spread over `start`..`end` seconds (`end` defaults to the asset's end; each tile is the
        centre of one of `count` equal slices), in a grid, each stamped in its top-left corner with its time
        (H:MM:SS, or M:SS.s when the tiles are under 10 s apart). Returns the image plus a compact text block:
        JSON with `columns`, `rows`, `tile_width`, `tile_height`, `width`, `height`, `source`, `duration` and
        `times` (tile times in seconds, row-major: tile i is column i % columns, row i // columns). Spot something
        in a tile, then get_frame at its time (source "original") for detail, or call again with a narrower
        start/end to zoom in. `columns` defaults to near-square (12 -> 4x3); the image is at most 2560 px wide, so
        `columns` x `tile_width` must fit. `source` "auto" reads the proxy when there is one, "original" the file.
        A bad range (the message says how long the asset is) or an audio-only asset is a tool error."""
        try:
            sheet = await run(
                contact_sheet.make_sheet,
                media,
                asset,
                start,
                end,
                count=count,
                columns=columns,
                tile_width=tile_width,
                format=format,
                source=source,
            )
        except frames.FrameError as exc:
            raise ToolError(str(exc)) from None
        lay = sheet.layout
        info = {k: v for k, v in sheet.info().items() if k not in ("tiles", "bytes", "mime", "asset")}
        info["times"] = [t.time for t in lay.tiles]
        return [Image(data=sheet.data, format=sheet.format), json.dumps(info, separators=(",", ":"))]

    @server.tool(annotations=_EDITS_PROJECT)
    async def analyze_scenes(
        asset: str,
        threshold: float = scenes.DEFAULT_THRESHOLD,
        min_gap: float = scenes.DEFAULT_MIN_GAP,
        screen_threshold: float = scenes.DEFAULT_SCREEN_THRESHOLD,
        force: bool = False,
    ) -> dict[str, Any]:
        """Find the scene changes and the screen-activity curve of the library asset with id `asset` (from
        list_assets), stored with the asset; read them with get_scenes. One cheap ffmpeg pass over the asset's proxy
        if it has a current one (else the original), at 5 frames per second and 160 px wide: minutes for a
        multi-hour file, before this returns. A cut is a frame whose change score (0..1) reaches `threshold`
        (default 0.15; lower finds subtler switches, higher only hard cuts); cuts closer than `min_gap` seconds
        (default 1) count as one. A second rule finds text-only changes on a same-background screen (a pane swapped for
        another): a frame where at least `screen_threshold` (default 0.02, 0 turns it off) of the picture changed,
        once, not scrolling or typing; each scene has `kind` ("cut" or "screen"), `score` and `changed`. Activity is one 0..1 value per second: how much the picture changed (typing in a
        still editor reads low, a window switch spikes). Returns `source` ("proxy" or "original"), `duration`,
        counts of `scenes` and `activity_buckets`, and `made` (false when current signals existed unless `force`).
        An audio-only asset returns `skipped`. list_assets shows `scenes` per asset."""
        try:
            return await run(
                scenes.analyze,
                media,
                asset,
                threshold=threshold,
                min_gap=min_gap,
                screen_threshold=screen_threshold,
                force=force,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=_READ_ONLY)
    async def get_scenes(asset: str, start: float | None = None, end: float | None = None) -> dict[str, Any]:
        """The stored scene changes and activity curve of the library asset `asset`: `scenes` (`time` in seconds from
        the start, `score` 0..1, `kind` "cut" or "screen", `changed` the share of the picture that changed), `activity` (one 0..1 value per `activity_interval` seconds; `activity[k]` covers
        `[k * interval, (k + 1) * interval)`), plus `source`, `duration`, `threshold` and `min_gap`. With `start`
        and/or `end` (seconds) only the cuts in that span and the activity values overlapping it (`activity_start` is
        the second the first value covers). Run analyze_scenes first: without signals this is a tool error."""
        found = await run(scenes.load, media, asset)
        if found is None:
            raise ToolError(f"asset {asset} has no scene signals yet: run analyze_scenes first")
        return scenes.excerpt(found, start, end)

    @server.tool(annotations=_EDITS_PROJECT)
    async def ocr_asset(
        asset: str,
        interval: float = ocr.DEFAULT_INTERVAL,
        max_frames: int = ocr.DEFAULT_MAX_FRAMES,
        force: bool = False,
    ) -> dict[str, Any]:
        """Read the text on the screen of the library asset with id `asset` (a screen recording, from list_assets) and
        store it with the asset: file names, error messages, terminal output. Search it with ocr_search and read it
        with get_ocr. Frames are read from the ORIGINAL (the proxy is too small to read code) with Tesseract, at each
        scene change (run analyze_scenes first for these; without scene data only the regular frames are read) and
        every `interval` seconds (default 30, at least 1), at most `max_frames` (default 2000); a frame that looks
        like the last one read is skipped. Slow and before this returns: about 1 to 2 seconds per frame read, 4 at a
        time. If current text made with these settings exists nothing is redone unless `force`. Returns `engine`,
        `duration`, `frames`, `scene_frames`, `ocr_frames`, `skipped_unchanged`, `capped`, `note` (a remark when the
        schedule was capped or there were no scenes) and `made`. An audio-only asset returns `skipped`. Without the
        tesseract program this is a tool error saying how to install it. list_assets shows `ocr` per asset."""
        try:
            ocr.require_available()  # before any frame is decoded
            return await run(ocr.recognize, media, asset, interval=interval, max_frames=max_frames, force=force)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        except ocr.OcrUnavailableError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=_READ_ONLY)
    async def get_ocr(
        asset: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read the stored on-screen text of the library asset `asset` between `start` and `end` seconds (either may
        be left out), one page at a time. Returns `items`, each `{time, until, text, lines, source_scene}`: the text
        read at `time` that stayed up until `until` (when other text replaced it); a state that began before `start`
        but is still up is included. A page holds at most `limit` states (default 20, at most 200). Also
        `total_in_range`, `range` and `next_cursor`: while it is not null, call again with the same arguments plus
        `cursor` to continue; every state appears exactly once. To find where some text was on screen use
        ocr_search. Without stored text this is a tool error: run ocr_asset."""
        return await run(ocr.ocr_range, media, asset, start, end, limit=limit, cursor=cursor)

    @server.tool(name="ocr_search", annotations=_READ_ONLY)
    async def search_ocr(
        asset: str,
        query: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
    ) -> dict[str, Any]:
        """Find where some text was on the screen of the library asset `asset`. Fuzzy and token-based, because OCR is
        imperfect: case-insensitive, split on anything that is not a letter or digit (`test_cut_burst` also finds
        `test cut burst`), and every word of the query must be in the same frame, allowing a glued or slightly
        misread word (at most 500 characters). Returns `hits` best first, each `{source: "ocr", time, end, score
        (0..1), text (the line that matched best), context_before, context_after (`context` lines, default 1, at most
        10), matched_tokens, frame}`: `time` is when the text first showed (seconds), `end` when it was replaced.
        Also `total_matches` and `next_cursor` (pass it back as `cursor` for the next page of at most `limit` hits,
        default 20, at most 200). `start` and `end` keep only text that was up in that span. Read the whole screen
        around a hit with get_ocr. Without stored text this is a tool error: run ocr_asset."""
        return await run(
            ocr.ocr_search, media, asset, query, start=start, end=end, limit=limit, cursor=cursor, context=context
        )

    @server.tool(annotations=_READ_ONLY)
    async def visual_search(
        asset: str,
        query: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
    ) -> dict[str, Any]:
        """Find what the screen of the library asset `asset` showed: the on-screen text search (same as ocr_search:
        fuzzy, token-based, best first; hits `{source: "ocr", time, end, score (0..1), text, context_before,
        context_after, matched_tokens, frame}`, `total_matches`, `next_cursor`) plus `sources`, which visual sources
        were searched (today only "ocr"; picture descriptions are not available). To search what was said as well,
        use search_asset. Without stored text this is a tool error: run ocr_asset."""
        return await run(
            search.visual_search, media, asset, query, start=start, end=end, limit=limit, cursor=cursor, context=context
        )

    @server.tool(annotations=_READ_ONLY)
    async def search_asset(
        asset: str,
        query: str,
        sources: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
    ) -> dict[str, Any]:
        """Find the moments where `query` was said OR shown in the library asset `asset`: one ranked list over the
        transcript and the on-screen text ("the part where the build fails" finds someone saying "the build
        failed" while the terminal shows FAILED). Each hit is a moment `{source, time (seconds: where to seek), end,
        score (0..1, comparable across sources), text, context_before, context_after (`context` words or lines, at
        most 10), match ("phrase" or "words"), found_in, evidence}`; `evidence` lists each source's own hit
        (`{source, time, end, score, text, covers, ...}`). Things said and shown within 15 s are one moment and rank
        higher. Matching is by word, no stemming: `failed` does not find `fails`. `sources` is "transcript", "ocr" or
        "transcript,ocr" (default both); the `sources` in the result says what was searched and how to make
        what is missing. Returns `total_matches` and `next_cursor` (pass it back as `cursor` for the next page of at
        most `limit` hits, default 20, at most 200); `start` and `end` keep hits in that span. If neither a
        transcript nor on-screen text exists this is a tool error naming transcribe_asset and ocr_asset. Look around a
        hit with transcript_range, get_ocr or get_frame."""
        return await run(
            search.search_asset,
            media,
            asset,
            query,
            sources=sources,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            context=context,
        )

    @server.tool(annotations=_READ_ONLY)
    async def what_is_happening(asset: str, time: float, window: float | None = None) -> dict[str, Any]:
        """Everything known about the library asset `asset` around `time` (seconds from the start), in one call:
        `audio` (dominant `class`: music, singing, speech, applause_crowd, laughter, silence_other or other_sound,
        which is loud sound YAMNet filed as silence_other, then `raw_class` says so; plus `share`, `secondary` and
        the `regions` in the window), `loudness` (`mean_db`, `max_db`, `min_db`, a `curve`),
        `silence` (`inside` a silence and no word being spoken, `inside_exact`, `intervals`), `speech` (the `words` spoken, `text`, `word_count`,
        `words_per_minute`), and `visual` (the nearest `scene_change`, `scene_index`, `shot_start`, screen `activity`,
        the on-screen `text` with its `lines`, `age_seconds` and `stale` (true when it was read long before `time` or the
        picture changed since: `note` says why), and `description`, which is always null for now). The window is
        `window` seconds (default 10, at most 600) centred on `time`, clamped to the asset. It only reads what other
        tools stored and never decodes or runs a model: a signal that was never computed is null, and `available` and
        `missing` say which and how to make it (analyze_audio, propose_splits, transcribe_asset, analyze_scenes,
        ocr_asset). A tool error for an unknown asset, a negative time, a time beyond the duration or a bad window.
        For a whole recording use timeline_index."""
        return await run(time_index.what_is_happening, media, asset, time, window)

    @server.tool(annotations=_READ_ONLY)
    async def timeline_index(
        asset: str, start: float | None = None, end: float | None = None, step: float | None = None
    ) -> dict[str, Any]:
        """Compact points over the library asset `asset`, one per `step` seconds (default 60, raised so there are at
        most 500 points) from `start` to `end` (default the whole asset): `audio` class, `loudness_db` (mean) and
        `loudness_max_db`, `word_count`, `words_per_minute`, `scene_changes` in the window, `activity` (mean) and the
        first line of on-screen `text`; null where a signal was never computed (`available` / `missing` say how to make
        it). Pick a point, then ask what_is_happening for the detail. A tool error for a bad span, step or more than
        500 points."""
        return await run(time_index.timeline_index, media, asset, start, end, step)

    @server.tool(
        name="media_report",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def media_report_tool(
        asset: str, bucket_seconds: float | None = None, include: list[str] | None = None, prepare: bool = False
    ) -> dict[str, Any]:
        """Every stored signal of the library asset `asset` in ONE document of at most 40 KB, so you do not have to call
        five tools: `asset` info, `signals` (which exist; `missing` says how to make the others), whole-recording
        structure (`loudness` summary, `regions` of music/speech/applause, `silence_gaps`, the strongest
        `scene_changes`, a `transcript` summary with top keywords) and `buckets`: one array per column (dominant
        audio class, mean and max loudness, silence fraction, words, scene cuts, activity, on-screen text changes
        and short transcript / screen text snippets) over equal time buckets (default max(10, duration / 180)
        seconds; `bucket_seconds` 5 to 600). `include` limits it to some of audio, loudness, silence, speech, scenes,
        ocr. If the document would be too large it is shrunk (shorter snippets, no snippets, coarser buckets) and
        `notices` says how. It only reads what is stored, unless prepare=true, which first makes the missing cheap
        signals (loudness curve, silence scan, audio classification if the YAMNet model is installed; can take
        minutes on a long recording) but never transcribes, finds scenes or reads on-screen text. A tool error for
        an unknown asset or a bad bucket size. See what_is_happening for the detail at one time."""
        return await run(
            media_report.media_report,
            media,
            asset,
            bucket_seconds=bucket_seconds,
            include=include,
            prepare=prepare,
        )

    @server.tool(annotations=_READ_ONLY)
    async def suggest_highlights(
        asset: str,
        preset: str = "default",
        count: int = highlights.DEFAULT_COUNT,
        min_seconds: float = highlights.DEFAULT_MIN_SECONDS,
        max_seconds: float = highlights.DEFAULT_MAX_SECONDS,
        target_seconds: float | None = None,
        start: float | None = None,
        end: float | None = None,
        keywords: list[str] | None = None,
        weights: dict[str, float] | None = None,
        min_gap_seconds: float = highlights.DEFAULT_MIN_GAP,
        snap: bool = True,
    ) -> dict[str, Any]:
        """A deterministic first pass at the highlights of the library asset `asset`: ranked, non-overlapping
        candidate windows (`start` / `end` are seconds on the asset's timeline, `duration` between min_seconds and
        max_seconds) scored from the stored speech density, loudness, applause / music share, loud non-speech sound,
        scene and screen changes, screen activity, on-screen text changes and `keywords` (found in speech or on-screen
        text, e.g. ["FAILED", "error"]), minus silence. `preset` sets the weights: gig (music, applause, loudness),
        stream (speech, screen changes, on-screen text, keywords), lecture (speech, slide changes, on-screen text)
        or default; `weights` overrides single ones, e.g. {"applause": 3, "speech": 0}; the valid signals are speech,
        loudness, applause, music, noise, scene, activity, ocr, keywords and silence (a penalty). Each candidate has
        `score` (0 to 1), `reasons` (what was found, in words), `score_parts`, `snapped` (what its edges were moved to:
        a silence, sentence or scene change), and a `title_hint` / `transcript_excerpt` / `ocr_excerpt`. `start` and
        `end` limit where to look. Only reads what is stored: `signals.missing` says how to make the absent
        signals. This proposes, it does not decide: judge the candidates yourself (get_frame, transcript_range,
        what_is_happening). A tool error for an unknown asset, an unknown preset or weight signal, a negative weight,
        a bad span, or when no signal is stored yet (it says what to run)."""
        return await run(
            highlights.suggest_highlights,
            media,
            asset,
            preset=preset,
            count=count,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=target_seconds,
            weights=weights,
            keywords=keywords,
            start=start,
            end=end,
            min_gap_seconds=min_gap_seconds,
            snap=snap,
        )

    @server.tool(annotations=_REPLACES_PROJECT)
    async def apply_plan(
        plan: dict[str, Any], dry_run: bool = False, base_revision: int | None = None
    ) -> dict[str, Any]:
        """Build a WHOLE timeline from ONE JSON edit plan instead of dozens of single edits (a highlights reel: one
        call). `plan` is `{"schema_version": 1, "project": "<name>", "mode": "create"|"replace", "tracks": [{"kind":
        "video", "clips": [{"asset": "a_...", "in": 12.5, "out": 41, "transition": "crossfade",
        "transition_duration": 1}, {"kind": "text", "text": "Title", "out": 3}]}]}`; call get_edit_plan_schema for
        the full format (it is strict: unknown fields are errors). Clip `in`/`out` are seconds of the asset, so a
        suggest_highlights candidate's `start`/`end` go straight in. The first video track is the magnetic main
        track (clips back to back); tracks with `"magnetic": false` place clips with `start`. Mode `create` (the
        default) fails if the project exists; mode `replace` needs `base_revision` (from show_timeline; also
        allowed inside the plan) or `"replace_unconditionally": true`. It is atomic: the project is built in memory
        and saved once, and a plan with ANY problem creates and changes nothing. `dry_run=true` only validates and
        describes the result. A tool error for an invalid plan carries EVERY problem as JSON
        `{"error": "plan_invalid", "errors": [{"path": "/tracks/0/clips/3/out", "code": "plan.clip.out_of_range",
        "message", "hint"}]}`: fix them all and call again. A stale `base_revision` is a revision_conflict error.
        Returns project, revision, action, clips, duration, per-track summary, warnings and plan_hash."""
        return await run(lambda: edit_plan.apply_plan(media, store, plan, dry_run=dry_run, base_revision=base_revision))

    @server.tool(annotations=_READ_ONLY)
    async def get_edit_plan_schema() -> dict[str, Any]:
        """The JSON Schema (draft 2020-12) of the edit plan that apply_plan takes, `schema_version` 1: every field,
        its type, range and default. Constraints a schema cannot express (assets exist, in/out within the asset,
        no overlaps) are checked by apply_plan and reported with stable `plan.*` codes."""
        return edit_plan.schema()

    @server.tool(annotations=_REPLACES_PROJECT)
    async def apply_ops(
        ops: dict[str, Any],
        dry_run: bool = False,
        base_revision: int | None = None,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        """Edit an EXISTING timeline with a BATCH of operations, all or none, in one call (and try it first with
        `dry_run=true`, which writes nothing and returns the resulting project as `timeline`). `ops` is the document
        `{"schema_version": 1, "project": "<name>", "ops": [{"op": "add_clip", "id": "c1", "asset": "a_...", "in": 0,
        "out": 10}, {"op": "set_transition", "clip": "c1", "transition": "crossfade", "duration": 1}, ...]}`; the ops
        are add_clip, trim_clip, move_clip, remove_clip, set_transition, set_clip_properties (incl. `crop`), add_text_clip,
        set_text_properties, add_track, remove_track, move_track, place_clip; call get_edit_ops_schema for every field
        (strict: unknown ops and fields are errors). Clips and tracks are addressed by id (show_timeline lists them);
        give a creating op (add_clip, add_text_clip, add_track) an `id` and later ops in the batch can use it. Indexes
        are zero-based. The ops run in order on an in-memory copy and are saved once only if every one succeeds; the
        first op the project refuses stops the batch. `continue_on_error=true` (dry run only) keeps going to report
        every independent problem (ops that use an id from a failed op are `ops.skipped_dependency`).
        `base_revision` (from show_timeline) fails with revision_conflict, writing nothing, if the timeline has moved
        on. A tool error for an invalid batch carries the report as JSON `{"error": "ops_invalid", "errors":
        [{"path": "/ops/3", "code": "ops.clip.not_found", "message", "hint"}], "applied", "results"}`. Returns project,
        revision, applied, per-op results with the ids they touched, the per-track summary and warnings."""
        return await run(
            lambda: edit_ops.apply_ops(
                media, store, ops, dry_run=dry_run, base_revision=base_revision, continue_on_error=continue_on_error
            )
        )

    @server.tool(annotations=_READ_ONLY)
    async def get_edit_ops_schema() -> dict[str, Any]:
        """The JSON Schema (draft 2020-12) of the operations document apply_ops takes, `schema_version` 1: every op
        and its fields. Constraints a schema cannot express (ids exist, assets exist, in/out within the asset, no
        overlaps) are checked by apply_ops and reported with stable `ops.*` / `plan.*` codes."""
        return edit_ops.schema()

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
    async def set_clip_crop(
        name: str,
        clip: int,
        track: int | None = None,
        x: float | None = None,
        y: float | None = None,
        w: float | None = None,
        h: float | None = None,
        aspect: str | None = None,
        center_x: float = 0.5,
        center_y: float = 0.5,
        zoom: float = 1.0,
        clear: bool = False,
        base_revision: int | None = None,
    ) -> dict[str, Any]:
        """Crop clip number `clip` on `track` (default the main video track) to a region of its source frame,
        e.g. a 9:16 slice of a 16:9 recording for a vertical short. Give ONE of: `x`,`y`,`w`,`h` together (fractions
        of the source frame, origin top-left; x,y 0..1, w,h 0.02..1, x+w and y+h <= 1); or `aspect` such as "9:16"
        (the largest region of that shape, centred on `center_x`/`center_y`, 0..1, and zoomed in by `zoom` >= 1;
        computed from the clip's source size, so you never do the arithmetic); or `clear=true` to remove the crop.
        Render at the same aspect (render_timeline width/height, e.g. 1080x1920) and the cropped picture fills the
        frame. Text and audio clips can't be cropped. `base_revision` (optional): fail with revision_conflict
        instead of editing if the timeline has changed since that revision."""
        explicit = [v is not None for v in (x, y, w, h)]
        if any(explicit) and not all(explicit):
            raise ToolError("give all of x, y, w, h together")
        if sum([all(explicit), aspect is not None, clear]) != 1:
            raise ToolError("give exactly one of x/y/w/h, aspect, or clear")
        project_ = await run(
            lambda: timeline_service.set_clip_crop(
                store,
                name,
                timeline_clip(name, clip, track, base_revision),
                media=media,
                crop={"x": x, "y": y, "w": w, "h": h} if all(explicit) else None,  # type: ignore[dict-item]
                aspect=aspect,
                center_x=center_x,
                center_y=center_y,
                zoom=zoom,
                clear=clear,
                base_revision=base_revision,
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
    async def render_timeline(
        name: str,
        output: str | None = None,
        overwrite: bool = False,
        width: int | None = None,
        height: int | None = None,
        preset: str | None = None,
        fit: str | None = None,
        captions: str | bool | None = None,
        caption_range: dict[str, float] | None = None,
        caption_font_size: float | None = None,
        caption_highlight_color: str | None = None,
        caption_position: str | None = None,
        caption_tracks: list[int] | None = None,
    ) -> dict[str, Any]:
        """Render the timeline to an mp4 in the root (default '<name>-render.mp4'). Refuses to replace an
        existing file unless overwrite=true. Re-encodes, so it can take a while for long clips. `width` and
        `height` (both or neither; even whole numbers 64..4096, e.g. 1080 x 1920 for a vertical short) set the
        output frame; by default it is the first clip's size. `preset` (see list_export_presets: youtube-1080p,
        vertical-1080, square-1080, draft-720) sets size, frame rate and encoder in one go; an explicit width and
        height beat the preset's size (explicit > preset > first clip). `fit` chooses what happens when a picture's
        aspect differs from the frame: "letterbox" (default: scale to fit, black bars) or "crop" (scale to cover and
        cut the overflow, centered); a clip with its own crop is not affected. `captions` burns in word-highlighted
        captions from the clips' transcripts (transcribe the assets first): a style name from list_caption_styles
        (classic, shorts, minimal) or true for the default; they sit on top of everything, at the output size. By
        default they come from the main video track's unmuted clips (`caption_tracks`: 1-based track numbers
        instead); `caption_range` {start, end} (timeline seconds) limits them; `caption_font_size` (fraction of the
        frame height), `caption_highlight_color` (#RRGGBB) and `caption_position` (top, middle, bottom) override the
        style. Captions are a render option, not part of the timeline. Returns name, output, duration and the
        preset, fit, width, height, fps used and `captions` (null, or style, clips_with_transcript,
        clips_without, words, lines, events)."""

        def work() -> dict[str, Any]:
            caption_request = captions_module.request(
                captions, caption_range, caption_font_size, caption_highlight_color, caption_position, caption_tracks
            )
            project_, filename, out_id = timeline_service.plan_render(
                store, media, name, output, overwrite, width, height, preset, fit, caption_request
            )
            used = timeline_service.run_render(
                project_, media, out_id, width=width, height=height, preset=preset, fit=fit,
                caption_request=caption_request,
            )  # fmt: skip
            return timeline_service.render_result(name, filename, project_, used)

        return await run(work)

    @server.tool(annotations=_READ_ONLY)
    async def list_export_presets() -> dict[str, Any]:
        """The export presets render_timeline takes by name (`preset`), each with label, description, width,
        height, fps, codecs, quality (crf), audio settings and container, plus the valid `fits`."""
        return presets.listing()

    @server.tool(annotations=_READ_ONLY)
    async def list_caption_styles() -> dict[str, Any]:
        """The caption styles render_timeline takes by name (`captions`), each with font size (a fraction of the
        frame height), colours, outline, position, rows and characters per row and whether it is upper-case,
        plus the valid `positions` for the override."""
        return captions_module.listing()

    return server
