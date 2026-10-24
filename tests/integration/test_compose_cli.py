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


def test_multi_track_project_with_overlay_text_and_audio_renders(three_songs_clip):
    root = three_songs_clip.parent
    shutil.copy(three_songs_clip, root / "pip.mp4")
    r = ["--root", root]
    run_json("new", "mix", *r)
    run_json("add-clip", "mix", three_songs_clip.name, "--out", 8, *r)
    body = run_json("add-track", "mix", "--kind", "video", *r)
    assert [t["index"] for t in body["tracks"]] == [1, 2]
    run_json("add-track", "mix", "--kind", "audio", *r)

    body = run_json("add-clip", "mix", "pip.mp4", "--track", 2, "--start", "1", "--out", 4, *r)
    assert body["tracks"][1]["clips"][0]["start"] == 1
    body = run_json("add-clip", "mix", "pip.mp4", "--track", 3, "--start", "2", "--out", 5, *r)
    assert body["tracks"][2]["clips"][0]["start"] == 2

    body = run_json("set", "mix", 1, "--track", 2, "--opacity", 0.6, "--scale", 0.3, "--x", 0.8, "--mute", *r)
    clip = body["tracks"][1]["clips"][0]
    assert (clip["opacity"], clip["scale"], clip["x"], clip["muted"]) == (0.6, 0.3, 0.8, True)
    body = run_json("set", "mix", 1, "--gain", 0.5, "--saturation", 0.5, *r)
    assert body["tracks"][0]["clips"][0]["gain"] == 0.5

    body = run_json("place", "mix", 1, "--from-track", 2, "--start", "3", *r)
    assert body["tracks"][1]["clips"][0]["start"] == 3
    body = run_json("add-text", "mix", "Hello", "--start", "7", "--duration", 2, "--color", "#ff0000", *r)
    text = body["tracks"][1]["clips"][1]
    assert (text["kind"], text["text"], text["start"]) == ("text", "Hello", 7)
    body = run_json("set-text", "mix", 2, "--track", 2, "--text", "Bye", *r)
    assert body["tracks"][1]["clips"][1]["text"] == "Bye"

    out = run_json("render", "mix", *r)
    assert (root / out["output"]).is_file()
    body = run_json("move-track", "mix", 2, "--to", 3, *r)
    assert body["tracks"][2]["magnetic"] is False
    body = run_json("remove-track", "mix", 3, *r)
    assert len(body["tracks"]) == 2


def test_multi_track_errors_are_clear(three_songs_clip):
    root = three_songs_clip.parent
    r = ["--root", str(root)]
    runner.invoke(app, ["compose", "new", "mix", *r])
    result = runner.invoke(app, ["compose", "add-text", "mix", "Hi", *r])
    assert result.exit_code == 1 and "no overlay video track" in result.output
    result = runner.invoke(app, ["compose", "remove-track", "mix", "9", *r])
    assert result.exit_code == 1 and "no track 9" in result.output
    result = runner.invoke(app, ["compose", "set", "mix", "1", *r])
    assert result.exit_code != 0
    result = runner.invoke(app, ["compose", "place", "mix", "1", *r])
    assert result.exit_code != 0
