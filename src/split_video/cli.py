"""Typer CLI: wires flags to the detect -> segment -> extract pipeline."""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError, detect_silence, extract_segment, probe_duration
from split_video.naming import build_manifest, segment_filename, write_manifest
from split_video.segments import compute_segments
from split_video.timefmt import format_timestamp

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)


@app.command()
def split(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to the input video/audio file."
    ),
    silence_threshold: str = typer.Option(
        "-35dB",
        "--silence-threshold",
        help="Audio level below which is considered silence (ffmpeg silencedetect noise syntax).",
    ),
    min_silence_duration: float = typer.Option(
        2.0,
        "--min-silence-duration",
        help="Minimum duration (seconds) of a quiet passage to count as a gap between songs.",
    ),
    min_song_length: float = typer.Option(
        30.0,
        "--min-song-length",
        help="Minimum duration (seconds) for a detected segment to be kept as its own song.",
    ),
    silence_padding: float = typer.Option(
        0.15,
        "--silence-padding",
        help="Seconds of near-silence to retain on each side of a cut, to avoid clipping a quiet attack or decay transient.",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Directory to write split files and manifest into. [default: '<source_basename>_split/' next to SOURCE]",
    ),
    output_format: Optional[str] = typer.Option(
        None,
        "--format",
        help="Force output container/codec (e.g. 'mp4', 'mkv'). Implies re-encoding.",
    ),
    precise: bool = typer.Option(
        False,
        "--precise/--no-precise",
        help="Re-encode for frame-accurate cuts instead of fast stream copy.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow overwriting existing files in the output directory."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print detected segments; write no files."
    ),
    manifest: bool = typer.Option(
        True, "--manifest/--no-manifest", help="Write manifest.json alongside output files."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show ffmpeg commands and raw silencedetect output for debugging."
    ),
) -> None:
    """Split a long recording of back-to-back songs into individual files by detecting silence gaps."""
    try:
        total_duration = probe_duration(source)
        silences = detect_silence(source, silence_threshold, min_silence_duration)
    except FfmpegNotFoundError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    except ProbeError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if verbose:
        for s in silences:
            console.print(f"[dim]silence: {s.start:.2f}s - {s.end:.2f}s ({s.end - s.start:.2f}s)[/dim]")

    segments = compute_segments(silences, total_duration, min_silence_duration, min_song_length, silence_padding)

    if not silences:
        console.print(
            "[yellow]Warning:[/yellow] no silence detected — treating the whole file as one song. "
            "Try loosening --silence-threshold or --min-silence-duration."
        )

    ext = source.suffix if output_format is None else f".{output_format.lstrip('.')}"
    filenames = [segment_filename(seg.index, len(segments), source.stem, ext) for seg in segments]

    table = Table(title=f"Detected segments ({len(segments)})")
    table.add_column("#", justify="right")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Duration")
    table.add_column("File")
    for seg, filename in zip(segments, filenames):
        table.add_row(
            str(seg.index),
            format_timestamp(seg.start, total_duration),
            format_timestamp(seg.end, total_duration),
            format_timestamp(seg.duration, total_duration),
            filename,
        )
    console.print(table)

    if dry_run:
        return

    resolved_output_dir = output_dir if output_dir is not None else source.with_name(f"{source.stem}_split")
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    conflicts = [f for f in filenames if (resolved_output_dir / f).exists()]
    if conflicts and not overwrite:
        error_console.print(
            f"[red]Error:[/red] {len(conflicts)} output file(s) already exist in "
            f"'{resolved_output_dir}' (use --overwrite to replace): {', '.join(conflicts)}"
        )
        raise typer.Exit(code=1)

    for seg, filename in zip(segments, filenames):
        out_path = resolved_output_dir / filename
        if verbose:
            console.print(f"[dim]extracting {filename}: {seg.start:.2f}s -> {seg.end:.2f}s[/dim]")
        try:
            extract_segment(source, seg.start, seg.end, out_path, precise, output_format)
        except ExtractError as exc:
            error_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1)

    if manifest:
        manifest_data = build_manifest(
            source_path=source,
            segments=segments,
            filenames=filenames,
            parameters={
                "silence_threshold": silence_threshold,
                "min_silence_duration": min_silence_duration,
                "min_song_length": min_song_length,
                "silence_padding": silence_padding,
                "precise": precise,
            },
            generated_at=datetime.now(timezone.utc),
        )
        manifest_path = write_manifest(manifest_data, resolved_output_dir)
        console.print(f"Wrote manifest to '{manifest_path}'")

    console.print(f"[green]Done.[/green] Wrote {len(segments)} file(s) to '{resolved_output_dir}'")


@app.command()
def edit(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to the video/audio file to edit."
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Interface to bind the local editor server to (use 0.0.0.0 in Docker)."
    ),
    port: int = typer.Option(8765, "--port", help="Port for the local editor server."),
    silence_threshold: float = typer.Option(
        -35.0, "--silence-threshold", help="Initial silence threshold in dB."
    ),
    min_silence_duration: float = typer.Option(
        2.0,
        "--min-silence-duration",
        help="Initial minimum duration (seconds) of a quiet passage to count as a gap between songs.",
    ),
    min_song_length: float = typer.Option(
        30.0,
        "--min-song-length",
        help="Initial minimum duration (seconds) for a detected segment to be kept as its own song.",
    ),
    silence_padding: float = typer.Option(
        0.15,
        "--silence-padding",
        help="Initial seconds of near-silence to retain on each side of a cut.",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't automatically open a browser tab (required inside Docker)."
    ),
) -> None:
    """Launch a local browser-based editor for adjusting song split points."""
    defaults = StateParams(
        silence_threshold=silence_threshold,
        min_silence_duration=min_silence_duration,
        min_song_length=min_song_length,
        padding=silence_padding,
    )
    editor_app = create_app(source, defaults)

    url = f"http://{host}:{port}/"
    console.print(f"Editor running at [bold]{url}[/bold] — press Ctrl+C to stop.")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()

    uvicorn.run(editor_app, host=host, port=port, log_level="warning")
