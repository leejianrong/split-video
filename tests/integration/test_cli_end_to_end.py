import json

from typer.testing import CliRunner

from split_video.cli import app
from split_video.ffmpeg import probe_duration

runner = CliRunner()

# Real songs are seconds long here, so use a low min-song-length instead of
# the 30s default meant for real recordings.
COMMON_ARGS = ["--min-silence-duration", "2", "--min-song-length", "2"]


def test_three_songs_split_into_three_files(three_songs_clip):
    result = runner.invoke(app, [str(three_songs_clip), *COMMON_ARGS])
    assert result.exit_code == 0, result.output

    output_dir = three_songs_clip.with_name("three_songs_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 3

    # Cut points sit at the midpoint of each silence gap, so each song's
    # duration includes half of the adjoining silence: tone(5) + half of
    # silence(3) = 6.5s on the ends, and the middle song picks up half of
    # each neighboring gap: 5 + 1.5 + 1.5 = 8.0s.
    expected_durations = [6.5, 8.0, 6.5]
    for path, expected in zip(outputs, expected_durations):
        assert abs(probe_duration(path) - expected) < 1.0  # -c copy keyframe slack

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert len(manifest["segments"]) == 3
    assert manifest["source_file"] == three_songs_clip.name


def test_quiet_bridge_does_not_split_the_song(quiet_bridge_clip):
    result = runner.invoke(app, [str(quiet_bridge_clip), *COMMON_ARGS])
    assert result.exit_code == 0, result.output

    output_dir = quiet_bridge_clip.with_name("quiet_bridge_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 1
    assert abs(probe_duration(outputs[0]) - 10.5) < 1.5


def test_leading_and_trailing_silence_trimmed(leading_trailing_silence_clip):
    result = runner.invoke(app, [str(leading_trailing_silence_clip), *COMMON_ARGS])
    assert result.exit_code == 0, result.output

    output_dir = leading_trailing_silence_clip.with_name("leading_trailing_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 2
    # Leading/trailing 2s silences are trimmed entirely; the shared middle cut
    # sits at the gap's midpoint, so each song picks up half of that gap:
    # tone(5) + half of silence(3) = 6.5s.
    for path in outputs:
        assert abs(probe_duration(path) - 6.5) < 1.0


def test_dry_run_writes_no_files(three_songs_clip):
    result = runner.invoke(app, [str(three_songs_clip), *COMMON_ARGS, "--dry-run"])
    assert result.exit_code == 0, result.output

    output_dir = three_songs_clip.with_name("three_songs_split")
    assert not output_dir.exists()


def test_existing_output_requires_overwrite_flag(three_songs_clip):
    first = runner.invoke(app, [str(three_songs_clip), *COMMON_ARGS])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, [str(three_songs_clip), *COMMON_ARGS])
    assert second.exit_code != 0
    assert "overwrite" in second.output.lower()

    third = runner.invoke(app, [str(three_songs_clip), *COMMON_ARGS, "--overwrite"])
    assert third.exit_code == 0, third.output
