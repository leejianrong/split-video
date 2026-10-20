"""`split-video compose ...`: build and render multi-clip timeline projects
from the command line. Thin callers of `split_video.timeline_service`, which
the editor's compose page and the MCP server use too.

A project is a named `<name>.split-video-timeline.json` file in `--root`
(default: the current directory); clip sources are paths under that root.
Clips are numbered from 1 in track order, as in the editor. Every command
takes `--json`.

Clips reference media by asset id (`a_` + 16 hex digits, the content-derived ids
listed by `compose assets` and shown by `compose show`), so a renamed or moved
file is found again. `add-clip` takes a path or `--asset ID`. Opening a project
made before asset ids existed links its clips automatically; the first edit (or
`compose migrate`) rewrites the project file in the current format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from split_video import assets, timeline_service
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import MediaStore
from split_video.render import parse_size
from split_video.store import ProjectStore
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import Project, TimelineError

console = Console()
error_console = Console(stderr=True)

compose_app = typer.Typer(help="Build and render multi-clip timeline projects.", no_args_is_help=True)

_Root = typer.Option(
    Path("."), "--root", exists=True, file_okay=False, help="Directory holding the videos and project files."
)
_Json = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table.")
_Name = typer.Argument(..., help="Project name (no extension).")
_Clip = typer.Argument(..., min=1, help="Clip number on its track, from 1.")
_Track = typer.Option(None, "--track", min=1, help="Track number (see `show`); default: the main video track.")

_BaseRevision = typer.Option(
    None,
    "--base-revision",
    min=0,
    help="The project revision (shown by `show`) this change is based on. If the project has changed "
    "since, nothing is written and the command exits with code 3. Omit to edit the latest version.",
)

_EXPECTED_ERRORS = (TimelineError, FfmpegNotFoundError, ProbeError, ExtractError)

CONFLICT_EXIT_CODE = 3


def _fail(exc: Exception, as_json: bool = False) -> typer.Exit:
    """Report `exc` and return the exit to raise. A revision conflict exits with code 3 and says how
    to retry; with `--json` it is also printed to stdout as `{"error": "revision_conflict", ...}`."""
    if isinstance(exc, RevisionConflictError):
        error_console.print(
            f"[red]Error:[/red] revision conflict: project {exc.project!r} is at revision {exc.current}, but this "
            f"change was based on revision {exc.base}, so nothing was written. Run "
            f"`split-video compose show {exc.project}` to see the latest, re-apply your change, and retry with "
            f"--base-revision {exc.current}."
        )
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "error": "revision_conflict",
                        "current_revision": exc.current,
                        "base_revision": exc.base,
                        "message": str(exc),
                    },
                    indent=2,
                )
            )
        return typer.Exit(code=CONFLICT_EXIT_CODE)
    error_console.print(f"[red]Error:[/red] {exc}")
    return typer.Exit(code=1)


def _time(text: str) -> float:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _show(media: MediaStore, name: str, project: Project, as_json: bool, **extra: Any) -> None:
    info = timeline_service.describe(media, name, project)
    if as_json:
        typer.echo(json.dumps({**extra, **info}, indent=2))
        return
    total = max(project.duration, 1.0)
    table = Table(title=f"{name} — {format_timestamp(project.duration, total)} — revision {project.revision}")
    for col in ("Track", "#", "Source", "Asset", "In", "Out", "Start", "Length", "Details"):
        # An asset id is useless cut short, so that column never wraps or shrinks.
        table.add_column(
            col,
            justify="right" if col == "#" else "left",
            **({"min_width": 18, "no_wrap": True} if col == "Asset" else {}),
        )
    for track in info["tracks"]:
        for c in track["clips"]:
            details = []
            if c["transition"] != "cut":
                details.append(f"crossfade {c['transition_duration']:g}s")
            if c["muted"]:
                details.append("muted")
            elif c["gain"] != 1.0:
                details.append(f"gain {c['gain']:g}")
            if c["opacity"] != 1.0:
                details.append(f"opacity {c['opacity']:g}")
            if c["crop"]:
                box = c["crop"]
                details.append(f"crop x{box['x']:.3g} y{box['y']:.3g} w{box['w']:.3g} h{box['h']:.3g}")
            table.add_row(
                f"{track['index']} {track['kind']}",
                str(c["index"]),
                f"“{c['text']}”" if c["kind"] == "text" else c["source"],
                c["asset"] or ("" if c["kind"] == "text" else "(unlinked)"),
                format_timestamp(c["in"], total),
                format_timestamp(c["out"], total),
                format_timestamp(c["start"], total),
                format_timestamp(c["duration"], total),
                ", ".join(details),
            )
    console.print(table)


def _clip_id(
    store: ProjectStore, name: str, clip: int, track: int | None = None, base_revision: int | None = None
) -> str:
    """The id of clip number `clip`, numbered in the version of the project at `base_revision` (which
    must still be the current one, else `RevisionConflictError`: the number would mean another clip)."""
    loaded = timeline_service.load_existing(store, name, base_revision=base_revision)
    return timeline_service.clip_id_at(loaded, clip, track)


def _track_id(store: ProjectStore, name: str, track: int | None, base_revision: int | None = None) -> str:
    return timeline_service.track_at(timeline_service.load_existing(store, name, base_revision=base_revision), track).id


def list_cmd(root: Path = _Root, as_json: bool = _Json) -> None:
    """List the timeline projects in the root."""
    names = LocalProjectStore(root.resolve()).list_timelines()
    if as_json:
        typer.echo(json.dumps({"projects": names}, indent=2))
    else:
        console.print("\n".join(names) if names else "No projects yet.")


def new_cmd(name: str = _Name, root: Path = _Root, as_json: bool = _Json) -> None:
    """Create an empty project."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.create_project(store, name)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def show_cmd(name: str = _Name, root: Path = _Root, as_json: bool = _Json) -> None:
    """Show a project's clips."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.load_existing(store, name, media)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def add_clip_cmd(
    name: str = _Name,
    video: str | None = typer.Argument(None, help="Video path, relative to --root (or use --asset)."),
    asset: str | None = typer.Option(None, "--asset", help="An asset id from `compose assets`, instead of a path."),
    at: int | None = typer.Option(
        None, "--at", min=1, help="Main track: insert as this clip number (default: the end)."
    ),
    start: str | None = typer.Option(
        None, "--start", help="Other tracks: where on the timeline (default: after the last clip)."
    ),
    track: int | None = _Track,
    in_point: str | None = typer.Option(None, "--in", help="Start within the video: seconds or [H:]MM:SS."),
    out_point: str | None = typer.Option(None, "--out", help="End within the video (default: its end)."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Add a clip of a video (a path, or --asset ID) to the video track."""
    if (video is None) == (asset is None):
        raise typer.BadParameter("give a video path or --asset ID, not both")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.add_clip(
            store,
            media,
            name,
            video,
            asset=asset,
            track_id=None if track is None else _track_id(store, name, track, base_revision),
            index=None if at is None else at - 1,
            start=None if start is None else _time(start),
            in_point=0.0 if in_point is None else _time(in_point),
            out_point=None if out_point is None else _time(out_point),
            base_revision=base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def trim_cmd(
    name: str = _Name,
    clip: int = _Clip,
    in_point: str | None = typer.Option(None, "--in", help="New start within the source."),
    out_point: str | None = typer.Option(None, "--out", help="New end within the source."),
    track: int | None = _Track,
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Change which part of its source a clip uses."""
    if in_point is None and out_point is None:
        raise typer.BadParameter("pass --in and/or --out")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.trim_clip(
            store,
            media,
            name,
            _clip_id(store, name, clip, track, base_revision),
            in_point=None if in_point is None else _time(in_point),
            out_point=None if out_point is None else _time(out_point),
            base_revision=base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def move_cmd(
    name: str = _Name,
    clip: int = _Clip,
    to: int = typer.Option(..., "--to", min=1, help="New clip number."),
    track: int | None = _Track,
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Move a clip to another position."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.move_clip(
            store, name, _clip_id(store, name, clip, track, base_revision), to - 1, media, base_revision=base_revision
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def remove_cmd(
    name: str = _Name,
    clip: int = _Clip,
    track: int | None = _Track,
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Remove a clip."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.remove_clip(
            store, name, _clip_id(store, name, clip, track, base_revision), media, base_revision=base_revision
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def transition_cmd(
    name: str = _Name,
    clip: int = _Clip,
    kind: str = typer.Option(..., "--kind", help="cut or crossfade."),
    duration: float = typer.Option(1.0, "--duration", help="Crossfade length in seconds."),
    track: int | None = _Track,
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Set how a clip joins the one before it."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.set_transition(
            store,
            name,
            _clip_id(store, name, clip, track, base_revision),
            kind,
            0.0 if kind == "cut" else duration,
            media,
            base_revision=base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def add_track_cmd(
    name: str = _Name,
    kind: str = typer.Option("video", "--kind", help="video or audio."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Add an empty track on top of the stack (clips on it are placed by start time)."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.add_track(store, name, kind, media, base_revision=base_revision)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def remove_track_cmd(
    name: str = _Name,
    track: int = typer.Argument(..., min=1, help="Track number (see `show`)."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Remove a track and its clips (the last video track can't be removed)."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.remove_track(
            store, name, _track_id(store, name, track, base_revision), media, base_revision=base_revision
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def move_track_cmd(
    name: str = _Name,
    track: int = typer.Argument(..., min=1, help="Track number (see `show`)."),
    to: int = typer.Option(..., "--to", min=1, help="New track number; later tracks draw on top."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Change a track's place in the stack."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.move_track(
            store, name, _track_id(store, name, track, base_revision), to - 1, media, base_revision=base_revision
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def place_cmd(
    name: str = _Name,
    clip: int = _Clip,
    from_track: int | None = typer.Option(None, "--from-track", min=1, help="Track the clip is on (default: main)."),
    start: str | None = typer.Option(None, "--start", help="New start on the timeline: seconds or [H:]MM:SS."),
    to_track: int | None = typer.Option(
        None, "--to-track", min=1, help="Move it to this other track of the same kind."
    ),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Move a clip in time and/or to another track (not for the main track: use `move`)."""
    if start is None and to_track is None:
        raise typer.BadParameter("pass --start and/or --to-track")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.place_clip(
            store,
            name,
            _clip_id(store, name, clip, from_track, base_revision),
            start=None if start is None else _time(start),
            track_id=None if to_track is None else _track_id(store, name, to_track, base_revision),
            media=media,
            base_revision=base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def set_cmd(
    name: str = _Name,
    clip: int = _Clip,
    track: int | None = _Track,
    gain: float | None = typer.Option(None, "--gain", help="Volume multiplier, 0..4 (1 = unchanged)."),
    mute: bool | None = typer.Option(None, "--mute/--unmute", help="Silence the clip's audio."),
    x: float | None = typer.Option(None, "--x", help="Overlay centre, 0..1 across the frame."),
    y: float | None = typer.Option(None, "--y", help="Overlay centre, 0..1 down the frame."),
    scale: float | None = typer.Option(None, "--scale", help="Overlay width as a fraction of the frame, 0.05..4."),
    opacity: float | None = typer.Option(None, "--opacity", help="0..1."),
    brightness: float | None = typer.Option(None, "--brightness", help="-1..1 (0 = unchanged)."),
    contrast: float | None = typer.Option(None, "--contrast", help="0..3 (1 = unchanged)."),
    saturation: float | None = typer.Option(None, "--saturation", help="0..3 (1 = unchanged, 0 = greyscale)."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Set a clip's volume, overlay placement or colour adjustment."""
    values = {
        "gain": gain, "muted": mute, "x": x, "y": y, "scale": scale, "opacity": opacity,
        "brightness": brightness, "contrast": contrast, "saturation": saturation,
    }  # fmt: skip
    changes = {k: v for k, v in values.items() if v is not None}
    if not changes:
        raise typer.BadParameter("pass at least one property, e.g. --gain or --opacity")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.set_clip_properties(
            store,
            name,
            _clip_id(store, name, clip, track, base_revision),
            media=media,
            base_revision=base_revision,
            **changes,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def crop_cmd(
    name: str = _Name,
    clip: int = _Clip,
    track: int | None = _Track,
    x: float | None = typer.Option(None, "--x", help="Left edge of the region, 0..1 of the source frame width."),
    y: float | None = typer.Option(None, "--y", help="Top edge of the region, 0..1 of the source frame height."),
    w: float | None = typer.Option(None, "--w", help="Region width, 0.02..1 of the source frame width."),
    h: float | None = typer.Option(None, "--h", help="Region height, 0.02..1 of the source frame height."),
    aspect: str | None = typer.Option(
        None, "--aspect", help="Instead of --x/--y/--w/--h: the largest region of this shape, e.g. 9:16."
    ),
    center_x: float = typer.Option(0.5, "--center-x", help="With --aspect: where to centre it, 0..1 across."),
    center_y: float = typer.Option(0.5, "--center-y", help="With --aspect: where to centre it, 0..1 down."),
    zoom: float = typer.Option(1.0, "--zoom", help="With --aspect: zoom in by this factor (1 = the largest region)."),
    clear: bool = typer.Option(False, "--clear", help="Remove the crop (show the whole frame)."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Crop a clip to a region of its source frame, e.g. a 9:16 slice of a 16:9 recording for a vertical short.

    Either explicit --x --y --w --h (fractions of the source frame, origin top-left), or --aspect 9:16 with
    optional --center-x/--center-y/--zoom, which computes the region from the clip's source size. Render the
    result at the same aspect with `render --size 1080x1920` to fill the frame."""
    explicit = [v is not None for v in (x, y, w, h)]
    if any(explicit) and not all(explicit):
        raise typer.BadParameter("give all of --x --y --w --h together")
    if sum([all(explicit), aspect is not None, clear]) != 1:
        raise typer.BadParameter("give exactly one of --x/--y/--w/--h, --aspect, or --clear")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.set_clip_crop(
            store,
            name,
            _clip_id(store, name, clip, track, base_revision),
            media=media,
            crop={"x": x, "y": y, "w": w, "h": h} if all(explicit) else None,  # type: ignore[dict-item]
            aspect=aspect,
            center_x=center_x,
            center_y=center_y,
            zoom=zoom,
            clear=clear,
            base_revision=base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def add_text_cmd(
    name: str = _Name,
    text: str = typer.Argument(..., help="The words to show."),
    track: int | None = typer.Option(None, "--track", min=1, help="An overlay video track (default: the first one)."),
    start: str = typer.Option("0", "--start", help="When it appears: seconds or [H:]MM:SS."),
    duration: float = typer.Option(3.0, "--duration", help="How long it stays, in seconds."),
    size: float = typer.Option(0.08, "--size", help="Text height as a fraction of the frame height."),
    color: str = typer.Option("#ffffff", "--color", help="#rrggbb."),
    x: float = typer.Option(0.5, "--x", help="Centre, 0..1 across the frame."),
    y: float = typer.Option(0.85, "--y", help="Centre, 0..1 down the frame."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Add a text title on an overlay video track."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        loaded = timeline_service.load_existing(store, name, media, base_revision=base_revision)
        if track is None:
            chosen = next((t for t in loaded.tracks if t.kind == "video" and not t.magnetic), None)
            if chosen is None:
                raise TimelineError("no overlay video track; add one with `compose add-track --kind video`")
            track_id = chosen.id
        else:
            track_id = timeline_service.track_at(loaded, track).id
        project = timeline_service.add_text_clip(
            store,
            name,
            track_id,
            text,
            duration,
            media=media,
            base_revision=base_revision,
            start=_time(start),
            font_size=size,
            color=color,
            x=x,
            y=y,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def set_text_cmd(
    name: str = _Name,
    clip: int = _Clip,
    track: int = typer.Option(..., "--track", min=1, help="The text clip's track."),
    text: str | None = typer.Option(None, "--text", help="New words."),
    size: float | None = typer.Option(None, "--size", help="Text height as a fraction of the frame height."),
    color: str | None = typer.Option(None, "--color", help="#rrggbb."),
    base_revision: int | None = _BaseRevision,
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Change a text clip's words, size or colour."""
    if text is None and size is None and color is None:
        raise typer.BadParameter("pass --text, --size and/or --color")
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.set_text_properties(
            store,
            name,
            _clip_id(store, name, clip, track, base_revision),
            media=media,
            base_revision=base_revision,
            text=text,
            font_size=size,
            color=color,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def assets_cmd(root: Path = _Root, as_json: bool = _Json) -> None:
    """List the assets (media files used by clips) with their ids, paths and stream info."""
    media = LocalMediaStore(root.resolve())
    with assets.open_book(media) as book:
        rows = [(a, book.present(a)) for a in book.list()]
    if as_json:
        typer.echo(json.dumps({"assets": [a.summary(ok) for a, ok in rows]}, indent=2))
        return
    if not rows:
        console.print("No assets yet. Adding a clip to a project registers its file.")
        return
    table = Table(title="Assets")
    for col in ("Asset", "Path", "Length", "Video", "Audio"):
        table.add_column(col)
    for a, ok in rows:
        video = "-" if a.width is None else f"{a.width}x{a.height}" + (f" @ {a.fps:g}" if a.fps else "")
        table.add_row(
            a.id, a.path if ok else f"{a.path} (missing)", format_timestamp(a.duration, a.duration), video,
            "yes" if a.has_audio else "no",
        )  # fmt: skip
    console.print(table)


def migrate_cmd(
    name: str = _Name, base_revision: int | None = _BaseRevision, root: Path = _Root, as_json: bool = _Json
) -> None:
    """Link a project's clips to assets and rewrite its file in the current format now."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        project = timeline_service.migrate_project(store, media, name, base_revision=base_revision)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    _show(media, name, project, as_json)


def render_cmd(
    name: str = _Name,
    output: str | None = typer.Option(None, "--output", help="Output filename (default: <name>-render.mp4)."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the output if it exists."),
    size: str | None = typer.Option(
        None, "--size", help="Output frame size WxH in pixels, e.g. 1080x1920 (even, 64..4096). Default: first clip's."
    ),
    preset: str | None = typer.Option(
        None, "--preset", help="Export preset (see `split-video presets`): sets size, frame rate and encoder."
    ),
    fit: str | None = typer.Option(
        None, "--fit", help="Aspect mismatch: letterbox (default, black bars) or crop (cover the frame)."
    ),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Render the project to an mp4 in the root. Size: --size beats the --preset's, which beats the first clip's."""
    root = root.resolve()
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)
    try:
        width, height = parse_size(size)
        project, filename, out_id = timeline_service.plan_render(
            store, media, name, output, overwrite, width, height, preset, fit
        )
        used = timeline_service.run_render(project, media, out_id, width=width, height=height, preset=preset, fit=fit)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json)
    if as_json:
        typer.echo(json.dumps(timeline_service.render_result(name, filename, project, used), indent=2))
    else:
        console.print(f"[green]Rendered[/green] {filename}")


def register(app: typer.Typer) -> None:
    for command_name, fn in (
        ("list", list_cmd),
        ("new", new_cmd),
        ("show", show_cmd),
        ("add-clip", add_clip_cmd),
        ("trim", trim_cmd),
        ("move", move_cmd),
        ("remove", remove_cmd),
        ("transition", transition_cmd),
        ("add-track", add_track_cmd),
        ("remove-track", remove_track_cmd),
        ("move-track", move_track_cmd),
        ("place", place_cmd),
        ("set", set_cmd),
        ("crop", crop_cmd),
        ("add-text", add_text_cmd),
        ("set-text", set_text_cmd),
        ("assets", assets_cmd),
        ("migrate", migrate_cmd),
        ("render", render_cmd),
    ):
        compose_app.command(command_name)(fn)
    app.add_typer(compose_app, name="compose")
