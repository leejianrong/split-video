import json

from typer.testing import CliRunner

from split_video.cli import app
from split_video.ffmpeg import probe_duration

runner = CliRunner()

# Real songs are seconds long here, so use a low min-song-length instead of
# the 30s default meant for real recordings.
COMMON_ARGS = ["split", "--min-silence-duration", "2", "--min-song-length", "2"]


def test_three_songs_split_into_three_files(three_songs_clip):
    result = runner.invoke(app, [*COMMON_ARGS, str(three_songs_clip)])
    assert result.exit_code == 0, result.output

    output_dir = three_songs_clip.with_name("three_songs_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 3

    # Cuts now trim the silence out entirely, leaving only the default 0.15s
    # padding on each side: tone(5) + padding(0.15) = 5.15s on the outer
    # songs; the middle song picks up padding on both sides of its two
    # neighboring gaps: 5 + 0.15 + 0.15 = 5.3s.
    expected_durations = [5.15, 5.3, 5.15]
    for path, expected in zip(outputs, expected_durations):
        assert abs(probe_duration(path) - expected) < 1.0  # -c copy keyframe slack

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert len(manifest["segments"]) == 3
    assert manifest["source_file"] == three_songs_clip.name


def test_quiet_bridge_does_not_split_the_song(quiet_bridge_clip):
    result = runner.invoke(app, [*COMMON_ARGS, str(quiet_bridge_clip)])
    assert result.exit_code == 0, result.output

    output_dir = quiet_bridge_clip.with_name("quiet_bridge_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 1
    assert abs(probe_duration(outputs[0]) - 10.5) < 1.5


def test_leading_and_trailing_silence_trimmed(leading_trailing_silence_clip):
    result = runner.invoke(app, [*COMMON_ARGS, str(leading_trailing_silence_clip)])
    assert result.exit_code == 0, result.output

    output_dir = leading_trailing_silence_clip.with_name("leading_trailing_split")
    outputs = sorted(output_dir.glob("*.mp4"))
    assert len(outputs) == 2
    # Leading/trailing silences are trimmed to just the default 0.15s padding;
    # the middle gap is trimmed the same way, so each song picks up padding
    # on both of its sides: tone(5) + padding(0.15) + padding(0.15) = 5.3s.
    for path in outputs:
        assert abs(probe_duration(path) - 5.3) < 1.0


def test_dry_run_writes_no_files(three_songs_clip):
    result = runner.invoke(app, [*COMMON_ARGS, str(three_songs_clip), "--dry-run"])
    assert result.exit_code == 0, result.output

    output_dir = three_songs_clip.with_name("three_songs_split")
    assert not output_dir.exists()


def test_existing_output_requires_overwrite_flag(three_songs_clip):
    first = runner.invoke(app, [*COMMON_ARGS, str(three_songs_clip)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, [*COMMON_ARGS, str(three_songs_clip)])
    assert second.exit_code != 0
    assert "overwrite" in second.output.lower()

    third = runner.invoke(app, [*COMMON_ARGS, str(three_songs_clip), "--overwrite"])
    assert third.exit_code == 0, third.output
