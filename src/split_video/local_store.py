"""`ProjectStore` on local disk: the sidecar files next to the videos.

Split-point projects live in `<video>.split-video-project.json` right next to
the video; timeline projects live in `<root>/<name>.split-video-timeline.json`.
Both have to be files in the directory being edited, not a centralized path
like `~/.cache/`: `make dev` runs the editor in a `--rm`'d container with only
the video directory bind-mounted, so anything written elsewhere is wiped on
the next run. This is the only module that knows those file names.

Deliberately separate from `local_media_store.py`, which holds *derived*
results that are safe to lose; these files hold what the user placed by hand.
"""

from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path

from split_video import timeline
from split_video.store import SavedSegment
from split_video.timeline import Project, ProjectExistsError, ProjectNotFoundError

SPLIT_FORMAT_VERSION = 2
SPLIT_SUFFIX = ".split-video-project.json"
TIMELINE_SUFFIX = ".split-video-timeline.json"


def _write_json_atomically(path: Path, payload: object) -> None:
    """Temp file then rename, so a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    with tmp.open("w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


class LocalProjectStore:
    """Projects as sidecar files under `root`.

    A split project's id is the video's path relative to `root` (POSIX
    separators); it may not escape `root`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    # --- paths -----------------------------------------------------------------

    def _split_paths(self, project_id: str) -> tuple[Path, Path]:
        """The video's path and its sidecar's, for a project id."""
        rel = posixpath.normpath(project_id.lstrip("/")) if project_id.strip("/") else ""
        if not rel or rel == "." or rel == ".." or rel.startswith("../"):
            raise ValueError(f"invalid project id {project_id!r}")
        video = self.root / rel
        return video, video.parent / f"{video.name}{SPLIT_SUFFIX}"

    def split_sidecar_path(self, project_id: str) -> Path:
        return self._split_paths(project_id)[1]

    def timeline_path(self, name: str) -> Path:
        timeline.validate_name(name)
        return self.root / f"{name}{TIMELINE_SUFFIX}"

    # --- split-point projects ----------------------------------------------------

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        try:
            with self.split_sidecar_path(project_id).open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        try:
            return [
                SavedSegment(
                    start=float(s["start"]),
                    end=float(s["end"]),
                    label=str(s.get("label", "")),
                    color=s.get("color"),
                    included=bool(s.get("included", True)),
                    export_name=s.get("export_name"),
                )
                for s in data["segments"]
            ]
        except (KeyError, TypeError, ValueError):
            return None

    def save_split(self, project_id: str, segments: list[SavedSegment]) -> None:
        video, path = self._split_paths(project_id)
        payload = {
            "version": SPLIT_FORMAT_VERSION,
            "source_file": video.name,
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "label": s.label,
                    "color": s.color,
                    "included": s.included,
                    "export_name": s.export_name,
                }
                for s in segments
            ],
        }
        _write_json_atomically(path, payload)

    # --- timeline projects -------------------------------------------------------

    def list_timelines(self) -> list[str]:
        return sorted(p.name.removesuffix(TIMELINE_SUFFIX) for p in self.root.glob(f"*{TIMELINE_SUFFIX}"))

    def timeline_exists(self, name: str) -> bool:
        return self.timeline_path(name).exists()

    def load_timeline(self, name: str) -> Project:
        """The project, or an empty one if the file is missing or unreadable —
        a damaged file should open as a blank project, not crash."""
        path = self.timeline_path(name)  # validates the name; a bad one is an error, not a blank project
        try:
            with path.open() as f:
                return timeline.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return timeline.empty_project()

    def load_existing_timeline(self, name: str) -> Project:
        if not self.timeline_exists(name):
            raise ProjectNotFoundError(f"no project named {name!r}")
        return self.load_timeline(name)

    def save_timeline(self, name: str, project: Project) -> None:
        _write_json_atomically(self.timeline_path(name), timeline.to_dict(project))

    def create_timeline(self, name: str) -> Project:
        if self.timeline_exists(name):
            raise ProjectExistsError(f"project {name!r} already exists")
        project = timeline.empty_project()
        self.save_timeline(name, project)
        return project
