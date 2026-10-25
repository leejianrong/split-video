"""scripts/check_wheel_contents.py recognises a package with and without the built frontend."""

import importlib.util
import tarfile
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_wheel_contents.py"
spec = importlib.util.spec_from_file_location("check_wheel_contents", SCRIPT)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

GOOD = [
    "split_video/editor/static_app/index.html",
    "split_video/editor/static_app/assets/index-BNJuYOLS.js",
]


def test_complete_archive_has_no_problems() -> None:
    assert check.problems(GOOD, "x.whl") == []
    assert check.problems([f"split_video-0.4.0/src/{n}" for n in GOOD], "x.tar.gz") == []


def test_missing_pieces_are_reported() -> None:
    assert len(check.problems(["split_video/editor/static/index.html"], "x.whl")) == 2
    assert check.problems(GOOD[:1], "x.whl") == ["x.whl: no hashed asset under split_video/editor/static_app/assets/"]


def test_main_checks_wheel_and_sdist(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "p-1-py3-none-any.whl", "w") as zf:
        for name in GOOD:
            zf.writestr(name, "x")
    assert check.main(["x", str(tmp_path)]) == 2  # no sdist yet
    member = tmp_path / "m"
    member.write_text("x")
    with tarfile.open(tmp_path / "p-1.tar.gz", "w:gz") as tf:
        tf.add(member, arcname="p-1/src/split_video/editor/static/index.html")
    assert check.main(["x", str(tmp_path)]) == 1  # sdist has no built app
