"""Burned-in captions rendered for real (SPL-53): decode frames and look at pixels.

Synthetic video: a flat grey 16:9 picture (so every non-grey pixel is caption) with a tone, and a transcript saved through
`transcribe.save` with known word times. `test_render_captions_real.py`-style checks with real speech live in the real-model
test at the bottom, which is skipped without faster-whisper and a downloaded model.
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fake_transcript import document_from_words
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import assets, captions, timeline_ops, timeline_service, transcribe
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import probe_media
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.render import find_font, render
from split_video.timeline import empty_project

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
RUNNER = CliRunner()
BG = (48, 48, 48)
CLIP_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"

# One line of eight words, half a second apart, source time 1.5 .. 5.4; the next sentence after a pause.
WORDS = [
    (1.5 + 0.5 * i, 1.5 + 0.5 * i + 0.4, w)
    for i, w in enumerate(["one", "two", "three", "four", "five", "six", "seven", "eight."])
]
WORDS += [
    (9.0, 9.4, "Then"),
    (9.5, 9.9, "a"),
    (10.0, 10.4, "second"),
    (10.5, 10.9, "line"),
    (11.0, 11.4, "of"),
    (11.5, 11.9, "words."),
]


def _ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("captions")
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=0x303030:size=640x360:rate=25:duration=14",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mp4"),
    )  # fmt: skip
    media = LocalMediaStore(root)
    asset = assets.register_path(media, "talk.mp4")
    transcribe.save(media, asset, document_from_words(asset.id, WORDS))
    # A second, different file with no transcript.
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=0x303030:size=320x180:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "silent.mp4"),
    )  # fmt: skip
    return root


def _frame(path, t, size=None):
    info = probe_media(path)
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(out, dtype=np.uint8).reshape(info.height, info.width, 3)


def _deviation(frame, tol=25):
    return (np.abs(frame.astype(int) - np.array(BG)) > tol).any(axis=2)


def _color_mask(frame, color, tol=70):
    return (np.abs(frame.astype(int) - np.array(color)) <= tol).all(axis=2)


def _centroid_x(mask):
    xs = np.nonzero(mask)[1]
    return float(xs.mean()) if len(xs) else None


def _bbox(mask):
    ys, xs = np.nonzero(mask)
    return (xs.min(), ys.min(), xs.max(), ys.max())


def _project(*clips):
    """`(in, out)` clips of talk.mp4 on the main track."""
    p = empty_project()
    for a, b in clips:
        p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "talk.mp4", 14.0, in_point=a, out_point=b)
    return p


def _render(root, project, name, **options):
    media = LocalMediaStore(root)
    used = render(project, media, name, **options)
    return used, root / name


YELLOW = (255, 230, 0)
GREEN = (57, 255, 20)
WHITE = (255, 255, 255)


# --- classic, landscape ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def landscape(root):
    # Source 1..6 s: the first sentence's words land at timeline 0.5, 1.0, ... (word n starts at 0.5 * n).
    request = captions.request("classic")
    used, out = _render(root, _project((1.0, 6.0)), "land.mp4", width=960, height=540, captions=request)
    return used, out


def test_the_render_result_reports_the_captions(landscape):
    used, out = landscape
    assert used["captions"]["style"] == "classic"
    assert used["captions"]["words"] == 8 and used["captions"]["clips_without"] == []
    assert used["captions"]["clips_with_transcript"] == 1 and used["captions"]["lines"] == 1
    assert used["captions"]["events"] == 9  # eight words and the hold
    assert (probe_media(out).width, probe_media(out).height) == (960, 540)


def test_caption_text_shows_in_the_window_and_only_there(landscape):
    _, out = landscape
    assert not _deviation(_frame(out, 0.2)).any(), "captions before the first word"
    during = _frame(out, 1.25)
    assert (_color_mask(during, WHITE, 40).sum() + _color_mask(during, YELLOW).sum()) > 400
    assert not _deviation(_frame(out, 4.9)).any(), "captions after the last word and the hold"


def test_the_highlight_moves_left_to_right_as_the_words_are_spoken(landscape):
    _, out = landscape
    # Word n (0-based) is spoken from 0.5 * (n + 1) to 0.5 * (n + 1) + 0.4; sample the middle of words 0, 2, 4, 7.
    xs = []
    for n in (0, 2, 4, 7):
        frame = _frame(out, 0.5 * (n + 1) + 0.2)
        x = _centroid_x(_color_mask(frame, YELLOW))
        assert x is not None, f"no highlight at word {n}"
        xs.append(x)
    assert xs == sorted(xs) and xs[-1] - xs[0] > 150, xs
    # Only one word is highlighted at a time: the highlight is a small part of the text.
    frame = _frame(out, 1.7)
    assert 0 < _color_mask(frame, YELLOW).sum() < 0.5 * _color_mask(frame, WHITE, 40).sum()


def test_the_text_sits_in_the_lower_third_inside_the_frame(landscape):
    _, out = landscape
    f = _frame(out, 1.7)
    x0, y0, x1, y1 = _bbox(_deviation(f))
    h, w = f.shape[:2]
    assert y0 > 0.66 * h and y1 < 0.97 * h and x0 > 0.04 * w and x1 < 0.96 * w
    assert abs((x0 + x1) / 2 - w / 2) < 0.03 * w  # centred


# --- shorts, vertical with a 9:16 crop --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vertical(root):
    p = _project((1.0, 6.0), (8.5, 13.0))
    crop = timeline_ops.crop_for_aspect(640, 360, 9, 16)  # the 9:16 slice of the 16:9 source (SPL-51)
    clips = tuple(
        replace(c, crop_x=crop["x"], crop_y=crop["y"], crop_w=crop["w"], crop_h=crop["h"]) for c in p.tracks[0].clips
    )
    p = replace(p, tracks=(replace(p.tracks[0], clips=clips),))
    request = captions.request("shorts")
    return _render(root, p, "vert.mp4", width=540, height=960, captions=request)


def test_shorts_text_is_inside_the_vertical_safe_zone_and_highlight_moves(vertical):
    used, out = vertical
    h, w = 960, 540
    assert used["captions"]["clips_with_transcript"] == 2 and used["captions"]["words"] == 8 + 6
    seen = 0
    for t in (0.7, 1.2, 1.7, 2.2, 3.7, 5.3, 6.0, 7.0, 8.0):
        f = _frame(out, t)
        mask = _deviation(f)
        if not mask.any():
            continue
        seen += 1
        x0, y0, x1, y1 = _bbox(mask)
        assert 0.10 * h <= y0 and y1 <= 0.90 * h, (t, y0, y1)  # the middle 80% of the height
        assert x0 >= 0.04 * w and x1 <= 0.96 * w, (t, x0, x1)  # not clipped at the sides
        assert y0 > 0.5 * h  # centre-lower area
    assert seen >= 6
    xs = [_centroid_x(_color_mask(_frame(out, 0.5 * (n + 1) + 0.2), GREEN, 60)) for n in (0, 1, 2)]
    assert None not in xs  # "one two three" is the first row of the line
    ys = [_centroid_x(_color_mask(_frame(out, 0.5 * (n + 1) + 0.2), GREEN, 60).T) for n in (0, 1, 2)]
    assert len({round(x) for x in xs} | {round(y) for y in ys}) > 1  # the highlight is somewhere else each time


def test_a_second_clip_with_a_cut_keeps_its_words_in_time(vertical):
    _, out = vertical
    # The second clip starts at timeline 5.0 and shows source 8.5..13: "Then" (9.0) lands at 5.5.
    assert not _deviation(_frame(out, 5.2)).any()  # the first sentence ended at 4.4 (+hold)
    assert _deviation(_frame(out, 5.6)).any()


# --- other options ----------------------------------------------------------------------------------------------------


def test_a_caption_range_limits_the_captions_in_timeline_time(root):
    request = captions.request("classic", caption_range=(2.0, 3.0))
    used, out = _render(root, _project((1.0, 6.0)), "range.mp4", width=480, height=270, captions=request)
    assert used["captions"]["words"] == 2
    assert not _deviation(_frame(out, 1.2)).any() and not _deviation(_frame(out, 3.8)).any()
    assert _deviation(_frame(out, 2.2)).any()


def test_position_and_size_overrides_move_and_grow_the_text(root):
    top = captions.request("minimal", position="top", font_size=0.08, highlight_color="#FF00FF")
    used, out = _render(root, _project((1.0, 3.0)), "top.mp4", width=640, height=360, captions=top)
    f = _frame(out, 0.7)
    x0, y0, x1, y1 = _bbox(_deviation(f))
    assert y1 < 0.4 * 360 and (y1 - y0) > 0.05 * 360
    assert _color_mask(f, (255, 0, 255)).any()


def test_clips_without_a_transcript_get_no_captions_and_are_reported(root):
    p = empty_project()
    p, a = timeline_ops.add_clip(p, p.tracks[0].id, "silent.mp4", 14.0, in_point=1.0, out_point=4.0)
    request = captions.request("classic")
    used, out = _render(root, p, "none.mp4", width=320, height=180, captions=request)
    assert used["captions"]["clips_without"] and used["captions"]["words"] == 0 and used["captions"]["events"] == 0
    assert not _deviation(_frame(out, 1.0)).any()


def test_no_captions_option_changes_nothing_and_has_no_report(root):
    used, out = _render(root, _project((1.0, 3.0)), "plain.mp4", width=320, height=180)
    assert "captions" not in used and not _deviation(_frame(out, 1.0)).any()


def test_captions_work_over_text_clips_and_overlays_on_top(root):
    p = _project((1.0, 6.0))
    p, track = timeline_ops.add_track(p, "video")
    p, _ = timeline_ops.add_text_clip(p, track.id, "TITLE", 5.0, start=0.0, x=0.5, y=0.86, font_size=0.09)
    used, out = _render(root, p, "ontop.mp4", width=640, height=360, captions=captions.request("classic"))
    f = _frame(out, 1.7)
    # The caption's opaque box covers the title where they meet: the box colour is under the caption text.
    assert used["captions"]["words"] == 8 and _color_mask(f, YELLOW).any()


def test_the_ass_file_goes_to_a_temporary_directory_never_the_workspace(root, monkeypatch):
    import tempfile

    seen = []
    real = tempfile.TemporaryDirectory

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.name)

    monkeypatch.setattr("split_video.render.tempfile.TemporaryDirectory", Spy)
    before = sorted(os.listdir(root))
    _, out = _render(
        root, _project((1.0, 3.0)), "tmpcheck.mp4", width=320, height=180, captions=captions.request("classic")
    )
    after = sorted(os.listdir(root))
    assert [n for n in after if n not in before] == ["tmpcheck.mp4"]
    assert not any(n.endswith(".ass") for n in after)
    assert len(seen) == 1 and not os.path.exists(seen[0]) and not seen[0].startswith(str(root))


def test_odd_characters_in_the_temp_and_font_paths_and_in_the_words_do_not_break_the_filter(tmp_path, monkeypatch):
    """The ASS path and the fonts directory go into the filter graph: give both every character it treats as special,
    and put the same into the words."""
    odd = tmp_path / "we'ird,dir:x [a];b"
    (odd / "fonts").mkdir(parents=True)
    (odd / "tmp").mkdir()
    font = Path(find_font())
    for f in font.parent.glob("DejaVuSans*.ttf"):
        shutil.copy(f, odd / "fonts" / f.name)
    monkeypatch.setenv("SPLIT_VIDEO_FONT", str(odd / "fonts" / font.name))
    monkeypatch.setattr(tempfile, "tempdir", str(odd / "tmp"))
    root = tmp_path / "ws"
    root.mkdir()
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=0x303030:size=320x180:rate=25:duration=3", "-f", "lavfi", "-i", "sine=duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "t.mp4"),
    )  # fmt: skip
    media = LocalMediaStore(root)
    asset = assets.register_path(media, "t.mp4")
    words = [(0.2, 0.7, "{bold}"), (0.8, 1.3, "a\\Nb,c:d"), (1.4, 1.9, "日本語"), (2.0, 2.4, "it's")]
    transcribe.save(media, asset, document_from_words(asset.id, words))
    p = empty_project()
    p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "t.mp4", 3.0)
    used, out = _render(root, p, "o.mp4", captions=captions.request("classic"))
    assert used["captions"]["words"] == 4
    assert _deviation(_frame(out, 0.4)).any() and _deviation(_frame(out, 1.0)).any()


# --- surfaces ---------------------------------------------------------------------------------------------------------


def _workspace(root, tmp_path, name):
    ws = tmp_path / name
    ws.mkdir()
    for f in root.iterdir():
        if f.name in ("talk.mp4", "silent.mp4") or f.name.startswith("."):
            (shutil.copytree if f.is_dir() else shutil.copy2)(f, ws / f.name)  # copy2: the cache is keyed by mtime
    media, store = LocalMediaStore(ws), LocalProjectStore(ws)
    timeline_service.create_project(store, "reel")
    timeline_service.add_clip(store, media, "reel", "talk.mp4", in_point=1.0, out_point=4.0)
    return ws


def _json_of(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_http_cli_and_mcp_style_listing_agree(tmp_path):
    from split_video.cli import app as cli

    http = TestClient(create_app(tmp_path, DEFAULTS)).get("/api/v1/caption-styles")
    assert http.status_code == 200 and http.json() == captions.listing()
    assert _json_of(RUNNER.invoke(cli, ["caption-styles", "--json"])) == captions.listing()
    table = RUNNER.invoke(cli, ["caption-styles"])
    assert table.exit_code == 0 and all(n in table.output for n in captions.names())


def _wait(client, job):
    for _ in range(400):
        status = client.get(f"/api/v1/compose/render/{job}").json()
        if status["status"] in ("done", "error", "cancelled"):
            return status
        time.sleep(0.1)
    raise AssertionError("render did not finish")


def test_http_and_cli_report_the_same_caption_result(root, tmp_path):
    cli_ws = _workspace(root, tmp_path, "cli")
    cli = _json_of(
        RUNNER.invoke(
            app,
            ["compose", "render", "reel", "--root", str(cli_ws), "--size", "480x270", "--captions", "shorts",
             "--caption-range", "0-2", "--caption-font-size", "0.06", "--caption-highlight", "#00FFFF",
             "--caption-position", "middle", "--json"],
        )
    )  # fmt: skip
    expected_captions = {
        "style": "shorts", "clips_with_transcript": 1, "clips_without": [], "words": 3, "lines": 1, "events": 4,
    }  # fmt: skip
    assert cli["captions"] == expected_captions
    f = _frame(cli_ws / "reel-render.mp4", 0.7)
    assert _color_mask(f, (0, 255, 255)).any()
    assert abs((_bbox(_deviation(f))[1] + _bbox(_deviation(f))[3]) / 2 - 135) < 30  # middle

    http_ws = _workspace(root, tmp_path, "http")
    client = TestClient(create_app(http_ws, DEFAULTS))
    body = {
        "width": 480, "height": 270, "captions": "shorts", "caption_range": {"start": 0, "end": 2},
        "caption_font_size": 0.06, "caption_highlight_color": "#00FFFF", "caption_position": "middle",
    }  # fmt: skip
    status = _wait(client, client.post("/api/v1/compose/projects/reel/render", json=body).json()["job_id"])
    assert status["status"] == "done", status
    assert status["captions"] == expected_captions
    # Existing fields keep their shape; a render without captions has `captions: null`.
    plain = _wait(
        client,
        client.post(
            "/api/v1/compose/projects/reel/render", json={"output": "p.mp4", "width": 320, "height": 180}
        ).json()["job_id"],
    )
    assert plain["captions"] is None and plain["width"] == 320
    # Style name `true` and false/null.
    default = _wait(
        client,
        client.post(
            "/api/v1/compose/projects/reel/render",
            json={"output": "d.mp4", "width": 320, "height": 180, "captions": True},
        ).json()["job_id"],
    )
    assert default["captions"]["style"] == "classic"
    off = _wait(
        client,
        client.post(
            "/api/v1/compose/projects/reel/render",
            json={"output": "n.mp4", "width": 320, "height": 180, "captions": False},
        ).json()["job_id"],
    )
    assert off["captions"] is None


def test_bad_caption_options_fail_the_same_way_on_http_and_cli(root, tmp_path):
    ws = _workspace(root, tmp_path, "bad")
    client = TestClient(create_app(ws, DEFAULTS))
    for body, needle in (
        ({"captions": "fancy"}, "valid styles: classic, minimal, shorts"),
        ({"captions": "classic", "caption_position": "left"}, "valid positions: top, middle, bottom"),
        ({"captions": "classic", "caption_font_size": 3}, "font_size"),
        ({"captions": "classic", "caption_highlight_color": "red"}, "#RRGGBB"),
        ({"captions": "classic", "caption_range": {"start": 5, "end": 1}}, "caption_range"),
        ({"captions": "classic", "caption_tracks": [7]}, "no track 7"),
    ):
        r = client.post("/api/v1/compose/projects/reel/render", json=body)
        assert r.status_code == 422 and needle in r.json()["detail"], (body, r.text)
    cli = RUNNER.invoke(app, ["compose", "render", "reel", "--root", str(ws), "--captions", "fancy"])
    assert cli.exit_code != 0 and "valid styles: classic, minimal, shorts" in cli.output.replace("\n", "")
    cli = RUNNER.invoke(
        app, ["compose", "render", "reel", "--root", str(ws), "--captions", "classic", "--caption-range", "9"]
    )
    assert cli.exit_code != 0 and "10-40" in cli.output.replace("\n", "")
    assert not (ws / "reel-render.mp4").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_renders_captions_and_lists_styles(root, tmp_path, anyio_backend):
    ws = _workspace(root, tmp_path, "mcp")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(ws)]
    )
    async with Client(params) as mcp:
        listed = await mcp.call_tool("list_caption_styles", {})
        assert listed.structured_content == captions.listing()
        done = await mcp.call_tool(
            "render_timeline",
            {
                "name": "reel",
                "width": 480,
                "height": 270,
                "captions": "classic",
                "caption_range": {"start": 0, "end": 2},
            },
        )
        assert not done.is_error, done.content
        assert done.structured_content["captions"]["words"] == 3
        assert _deviation(_frame(ws / "reel-render.mp4", 0.7)).any()
        bad = await mcp.call_tool("render_timeline", {"name": "reel", "captions": "fancy", "overwrite": True})
        assert bad.is_error and "valid styles" in str(bad.content)


# --- real speech, real model ---------------------------------------------------------------------------------------------

models_source = os.environ.get("SPLIT_VIDEO_TEST_MODELS")
real_model = [
    pytest.mark.real_model,
    pytest.mark.skipif(not transcribe.available(), reason="faster-whisper is not installed"),
    pytest.mark.skipif(
        not models_source or not (Path(models_source) / "models--Systran--faster-whisper-base.en").is_dir(),
        reason="set SPLIT_VIDEO_TEST_MODELS to a folder holding the base.en model",
    ),
]


@real_model[0]
@real_model[1]
@real_model[2]
def test_real_speech_gets_captions_whose_highlight_follows_the_transcribed_words(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copytree(models_source, ws / ".split-video-models")
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=0x303030:size=640x360:rate=25:duration=11.3", "-i", str(CLIP_FIXTURE),
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(ws / "talk.mp4"),
    )  # fmt: skip
    media = LocalMediaStore(ws)
    asset_id = assets.register_path(media, "talk.mp4").id
    transcribe.transcribe(media, asset_id)
    stored = transcribe.load(media, asset_id)
    words = [w for s in stored["segments"] for w in s["words"]]
    assert len(words) >= 20

    p = empty_project()
    p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "talk.mp4", 11.2, in_point=0.0, out_point=4.5)
    p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "talk.mp4", 11.2, in_point=6.8, out_point=11.0)
    request = captions.request("minimal", font_size=0.045)  # one row, so the highlight's x says where we are
    used, out = _render(ws, p, "real.mp4", width=960, height=540, captions=request)
    assert used["captions"]["words"] >= 20 and used["captions"]["clips_without"] == []

    # In the first clip the first line holds "Mr. Quilter is the apostle of the middle classes,": sample the middle of
    # each of its words and the highlight must move right.
    line = [w for w in words if w["end"] <= 2.9]
    xs = []
    for w in line:
        mid = (w["start"] + w["end"]) / 2
        mask = _color_mask(_frame(out, mid), (142, 200, 255), 45)
        xs.append(_centroid_x(mask))
    assert None not in xs, xs
    assert all(b > a for a, b in itertools.pairwise(xs)), xs
    # Before the first word is spoken (it starts at 0.18 s) there is nothing on screen.
    assert not _deviation(_frame(out, 0.05), 40).any()
