"""Output filenames and the manifest.json shape."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from split_video.segments import Segment


def segment_filename(index: int, total: int, basename: str, ext: str) -> str:
    width = max(2, len(str(total)))
    return f"{index:0{width}d} - {basename}{ext}"


def build_manifest(
    source_path: Path,
    segments: list[Segment],
    filenames: list[str],
    parameters: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "source_file": source_path.name,
        "generated_at": generated_at.isoformat(),
        "parameters": parameters,
        "segments": [
            {
                "index": segment.index,
                "file": filename,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "duration": round(segment.duration, 3),
            }
            for segment, filename in zip(segments, filenames)
        ],
    }


def write_manifest(manifest: dict[str, Any], output_dir: Path) -> Path:
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path
