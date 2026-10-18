"""Only the local store knows where project files live."""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "split_video"

# The engine, the adapters and the editor routes: everything that must go through ProjectStore.
GUARDED = [
    "project.py",
    "timeline_service.py",
    "timeline.py",
    "timeline_ops.py",
    "project_cli.py",
    "compose_cli.py",
    "mcp_server.py",
    "editor/app.py",
    "editor/compose.py",
]
FORBIDDEN = re.compile(
    r"_project_path|project_path|FILE_SUFFIX|\.split-video-project\.json|\.split-video-timeline\.json"
)


@pytest.mark.parametrize("module", GUARDED)
def test_module_does_not_build_project_paths(module):
    code = []
    in_doc = False
    for line in (SRC / module).read_text().splitlines():
        # Prose in docstrings and comments may name the files; code may not.
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith(("#", '"""')):
            continue
        code.append(line)
    assert not FORBIDDEN.findall("\n".join(code)), f"{module} builds a project path itself"


def test_the_local_store_is_the_one_place_with_the_file_names():
    text = (SRC / "local_store.py").read_text()
    assert ".split-video-project.json" in text and ".split-video-timeline.json" in text
