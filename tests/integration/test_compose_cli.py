import json
import shutil

from typer.testing import CliRunner

from split_video.cli import app

runner = CliRunner()


def run(root, *args):
    return runner.invoke(app, [str(a) for a in args])


def run_json(*args):
    result = runner.invoke(app, ["compose", *[str(a) for a in args], "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_build_edit_and_render_a_two_clip_project(three_songs_clip, tmp_path):
    root = three_songs_clip.parent
    shutil.copy(three_songs_clip, root / "b.mp4")
    r = ["--root", root]

    assert run_json("new", "mix", *r)["tracks"][0]["clips"] == []
    run_json("add-clip", "mix", three_songs_clip.name, "--out", 6, *r)
    body = run_json("add-clip", "mix", "b.mp4", "--in", "0:02", "--out", "0:06", *r)
    clips = body["tracks"][0]["clips"]
    assert [c["index"] for c in clips] == [1, 2]
    assert (clips[1]["start"], clips[1]["duration"]) == (6, 4)

    body = run_json("transition", "mix", 2, "--kind", "crossfade", "--duration", 1, *r)
    assert body["duration"] == 9
    body = run_json("trim", "mix", 1, "--in", 1, *r)
    assert body["duration"] == 8
    body = run_json("move", "mix", 2, "--to", 1, *r)
    assert body["tracks"][0]["clips"][0]["source"] == "b.mp4"
    assert run_json("list", *r)["projects"] == ["mix"]

    out = run_json("render", "mix", *r)
    assert (root / out["output"]).is_file()
    again = runner.invoke(app, ["compose", "render", "mix", "--root", str(root)])
    assert again.exit_code == 1 and "already exists" in again.output

    assert len(run_json("remove", "mix", 1, *r)["tracks"][0]["clips"]) == 1
    shown = runner.invoke(app, ["compose", "show", "mix", "--root", str(root)])
    assert shown.exit_code == 0 and "mix" in shown.output


def test_errors_exit_nonzero_with_a_message(three_songs_clip):
    root = three_songs_clip.parent
    r = ["--root", str(root)]
    result = runner.invoke(app, ["compose", "show", "nope", *r])
    assert result.exit_code == 1 and "no project" in result.output
    runner.invoke(app, ["compose", "new", "mix", *r])
    result = runner.invoke(app, ["compose", "remove", "mix", "1", *r])
    assert result.exit_code == 1 and "no clip 1" in result.output
    result = runner.invoke(app, ["compose", "add-clip", "mix", "../x.mp4", *r])
    assert result.exit_code == 1 and "outside" in result.output
    result = runner.invoke(app, ["compose", "trim", "mix", "1", *r])
    assert result.exit_code != 0
