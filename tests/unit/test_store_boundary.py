"""Only the local stores know where project files, media and caches live."""

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
    # Prose in docstrings and comments may name the files; code may not.
    assert not FORBIDDEN.findall(_code(module)), f"{module} builds a project path itself"


# The engine proper: no pathlib at all. Every media location is an opaque id handled by the MediaStore.
ENGINE = [
    "project.py",
    "timeline_service.py",
    "timeline.py",
    "timeline_ops.py",
    "assets.py",
    "render.py",
    "editor/cache.py",
    "jobs.py",
    "job_work.py",
    "identity.py",
    "store_provider.py",
    "editor/compose.py",
]
# Adapters may take a `Path` root at their entry point (and app.py serves its static dir) but
# must not do filesystem work or root-relative arithmetic on media themselves.
ADAPTERS = ["mcp_server.py", "editor/app.py", "editor/media_response.py", "project_cli.py", "compose_cli.py"]
MEDIA_FORBIDDEN = re.compile(
    r"\.split-video-cache|CACHE_DIR_NAME|cache_store|resolve_within_root|resolve_source|relative_source"
    r"|\.relative_to\(|\.with_name\(|\.glob\(|\.rglob\(|\.stat\(\)"
    r"|FileResponse|PathEscapesRootError|editor\.browse|\broot\s*/"
)
# Adapters resolve their entry-point root once to build the local stores; the engine never resolves paths.
ENGINE_FORBIDDEN = re.compile(r"\.resolve\(\)|\.iterdir\(|\.is_file\(\)|\.is_dir\(\)|\.exists\(\)|\.mkdir\(|\.unlink\(")
PATHLIB_IMPORT = re.compile(r"^\s*(from pathlib import|import pathlib)", re.MULTILINE)


def _code(module: str) -> str:
    code = []
    in_doc = False
    for line in (SRC / module).read_text().splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith(("#", '"""')):
            continue
        code.append(line)
    return "\n".join(code)


@pytest.mark.parametrize("module", ENGINE + ADAPTERS)
def test_module_builds_no_media_paths_or_cache_locations(module):
    assert not MEDIA_FORBIDDEN.findall(_code(module)), f"{module} does its own media path handling"


@pytest.mark.parametrize("module", ENGINE)
def test_engine_modules_do_not_resolve_paths(module):
    assert not ENGINE_FORBIDDEN.search(_code(module)), f"{module} resolves a path"


@pytest.mark.parametrize("module", ENGINE)
def test_engine_modules_import_no_pathlib(module):
    assert not PATHLIB_IMPORT.search(_code(module)), f"{module} imports pathlib"


def test_identity_has_no_framework_imports():
    # Principal is engine-level; the FastAPI wiring (authenticators, middleware) lives under editor/.
    for module in ("identity.py", "store_provider.py"):
        assert not re.search(r"^\s*(from|import) (fastapi|starlette|uvicorn)", _code(module), re.MULTILINE), module


def test_the_browse_and_cache_helpers_are_gone():
    assert not (SRC / "editor" / "browse.py").exists()
    assert not (SRC / "editor" / "cache_store.py").exists()


def test_the_local_media_store_is_the_one_place_with_the_cache_dir_and_containment_checks():
    text = (SRC / "local_media_store.py").read_text()
    assert ".split-video-cache" in text and "is outside" not in text and "MediaOutsideRootError" in text
    others = [p for p in SRC.rglob("*.py") if p.name != "local_media_store.py"]
    assert not [p.name for p in others if ".split-video-cache" in _code(str(p.relative_to(SRC)))]


def test_the_local_store_is_the_one_place_with_the_file_names():
    text = (SRC / "local_store.py").read_text()
    assert ".split-video-project.json" in text and ".split-video-timeline.json" in text


def test_the_local_media_store_is_the_one_place_with_the_asset_table_file_name():
    assert ".split-video-assets.json" in (SRC / "local_media_store.py").read_text()
    others = [p for p in SRC.rglob("*.py") if p.name != "local_media_store.py"]
    assert not [p.name for p in others if ".split-video-assets.json" in _code(str(p.relative_to(SRC)))]


def test_the_local_store_is_the_one_place_with_the_project_lock_file():
    # Lock files sit next to the project files, so only the store that names those files knows them.
    text = (SRC / "local_store.py").read_text()
    assert 'LOCK_SUFFIX = ".lock"' in text and "fcntl.flock" in text
    others = [p for p in SRC.rglob("*.py") if p.name not in ("local_store.py",)]
    leaks = [
        p.name
        for p in others
        if re.search(r"fcntl|flock|LOCK_SUFFIX|\.lock[\"']", _code(str(p.relative_to(SRC))))
        and p.name != "local_media_store.py"
    ]
    assert not leaks, f"{leaks} handle project lock files"


def test_the_revision_bump_has_one_announcement_point():
    # SPL-37 hooks `store.announce_revision`; both stores must go through it and nothing else may fire it.
    callers = [p.name for p in SRC.rglob("*.py") if "announce_revision(" in _code(str(p.relative_to(SRC)))]
    assert sorted(callers) == ["local_store.py", "store.py"]
