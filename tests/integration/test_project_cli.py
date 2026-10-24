import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.classify import MODEL_PATH
from split_video.editor.schemas import StateParams

runner = CliRunner()


def run(*args):
    return runner.invoke(app, [str(a) for a in args])


def run_json(*args):
    result = run(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_segments_reports_the_whole_recording_before_anything_is_saved(three_songs_clip):
    body = run_json("segments", three_songs_clip)
    assert body["saved"] is False
    assert len(body["segments"]) == 1
    assert body["segments"][0]["start"] == 0


def test_propose_shows_without_saving_until_applied(three_songs_clip):
    proposed = run_json("propose", three_songs_clip, "--min-song-length", 2)
    assert len(proposed["segments"]) == 3
    assert proposed["applied"] is False
    assert run_json("segments", three_songs_clip)["saved"] is False

    applied = run_json("propose", three_songs_clip, "--min-song-length", 2, "--apply")
    assert applied["applied"] is True
    after = run_json("segments", three_songs_clip)
    assert after["saved"] is True
    assert len(after["segments"]) == 3


def test_add_move_remove_split_round_trip(three_songs_clip):
    added = run_json("add-split", three_songs_clip, "--at", "0:07.5")
    assert added["split"] == 1
    assert [s["end"] for s in added["segments"][:-1]] == [7.5]

    moved = run_json("move-split", three_songs_clip, 1, "--to", "9")
    assert moved["applied_to"] == 9
    assert moved["segments"][0]["end"] == 9

    removed = run_json("remove-split", three_songs_clip, 1)
    assert len(removed["segments"]) == 1


def test_label_sets_metadata_and_requires_something_to_set(three_songs_clip):
    run_json("add-split", three_songs_clip, "--at", 8)
    body = run_json("label", three_songs_clip, 2, "--label", "Encore", "--color", "#ff8800", "--exclude")
    seg = body["segments"][1]
    assert (seg["label"], seg["color"], seg["included"]) == ("Encore", "#ff8800", False)

    assert run("label", three_songs_clip, 2).exit_code != 0


def test_bad_input_exits_nonzero_with_a_message(three_songs_clip):
    result = run("remove-split", three_songs_clip, 1)
    assert result.exit_code == 1
    assert "single-segment" in result.output
    assert run("add-split", three_songs_clip, "--at", "nonsense").exit_code != 0


def test_export_writes_included_segments_and_only_overrides(three_songs_clip):
    run_json("propose", three_songs_clip, "--min-song-length", 2, "--apply")
    run_json("label", three_songs_clip, 2, "--exclude", "--export-name", "Middle")

    body = run_json("export", three_songs_clip)
    names = sorted(p.rsplit("/", 1)[-1] for p in body["files"])
    assert names == ["01 - three_songs.mp4", "02 - three_songs.mp4"]
    assert body["manifest"].endswith("manifest.json")

    # Re-running without --overwrite refuses to clobber.
    assert run("export", three_songs_clip).exit_code == 1

    only = run_json("export", three_songs_clip, "--only", 2, "--overwrite")
    assert [p.rsplit("/", 1)[-1] for p in only["files"]] == ["Middle.mp4"]


def test_export_rejects_a_malformed_or_unknown_only(three_songs_clip):
    assert run("export", three_songs_clip, "--only", "a,b").exit_code != 0
    assert run("export", three_songs_clip, "--only", 9).exit_code == 1


def test_cli_edits_are_visible_to_the_editor_api(three_songs_clip):
    """The CLI and the browser editor share one sidecar file."""
    run_json("add-split", three_songs_clip, "--at", 8)
    run_json("label", three_songs_clip, 1, "--label", "Opener")

    client = TestClient(
        create_app(
            three_songs_clip,
            StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15),
        )
    )
    state = client.get("/api/state", params={"path": three_songs_clip.name}).json()
    assert state["resumed"] is True
    first = state["segments"][0]
    assert (first["label"], first["end"]) == ("Opener", 8)


@pytest.mark.skipif(not MODEL_PATH.exists(), reason="YAMNet model not fetched (make fetch-model)")
def test_analyze_reports_buckets(three_songs_clip):
    body = run_json("analyze", three_songs_clip)
    assert body["analyzed"] is True
