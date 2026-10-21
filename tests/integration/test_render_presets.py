"""Export presets and the letterbox-or-crop choice, rendered for real (SPL-52): ffprobe the files and look at pixels.

Marker source: a 640x360 (16:9) grey frame with a red bar on the left edge, a green bar on the right edge and a white
square in the centre, so we can tell which parts of the picture survive each fit.
"""

import json
import shutil
import struct
import subprocess
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import presets, timeline_ops, timeline_service
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import probe_media
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.render import render
from split_video.timeline import empty_project

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
RUNNER = CliRunner()
ENC = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]


def _ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


def _marker_source(path, size, rate=25, seconds=2):
    w, h = size
    bar, box = max(4, w // 16), max(8, w // 8)
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x808080:size={w}x{h}:rate={rate}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-vf", f"drawbox=x=0:y=0:w={bar}:h={h}:color=red:t=fill,drawbox=x={w - bar}:y=0:w={bar}:h={h}:color=0x00ff00:t=fill,"
        f"drawbox=x={(w - box) // 2}:y={(h - box) // 2}:w={box}:h={box}:color=white:t=fill",
        *ENC, str(path),
    )  # fmt: skip


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("presets")
    _marker_source(root / "wide.mp4", (640, 360))
    _marker_source(root / "four3.mp4", (640, 480))
    _marker_source(root / "square.mp4", (480, 480))
    # Left half red, right half blue (the SPL-51 crop source), for crop-fit combined with a clip crop.
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:size=320x360:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=blue:size=320x360:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex", "[0][1]hstack[v]", "-map", "[v]", "-map", "2:a", *ENC, str(root / "split.mp4"),
    )  # fmt: skip
    return root


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    return video, audio, data["format"]


def _moov_before_mdat(path):
    """Walk the top-level mp4 boxes: faststart puts `moov` ahead of `mdat`."""
    order = []
    with open(path, "rb") as f:
        while header := f.read(8):
            size, kind = struct.unpack(">I4s", header)
            order.append(kind.decode())
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0] - 8
                f.seek(size - 8, 1)
            else:
                f.seek(size - 8, 1)
    return order.index("moov") < order.index("mdat")


def _frame(path, t):
    info = probe_media(path)
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(out, dtype=np.uint8).reshape(info.height, info.width, 3)


def _share(region, color, tol=60):
    return float((np.abs(region.astype(int) - np.array(color)) <= tol).all(axis=2).mean())


RED, GREEN, WHITE, BLACK = (255, 0, 0), (0, 255, 0), (255, 255, 255), (0, 0, 0)


def _project(*sources, duration=2.0):
    p = empty_project()
    for source in sources:
        p, _ = timeline_ops.add_clip(p, p.tracks[0].id, source, duration)
    return p


def _render(root, project, name, **options):
    return render(project, LocalMediaStore(root), name, **options), root / name


# --- every preset, for real -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(presets.PRESETS))
def test_each_preset_renders_the_expected_file(root, name):
    preset = presets.PRESETS[name]
    used, out = _render(root, _project("wide.mp4", "wide.mp4"), f"{name}.mp4", preset=name)
    video, audio, fmt = _probe(out)
    assert (video["width"], video["height"]) == (preset.width, preset.height)
    assert video["r_frame_rate"] == video["avg_frame_rate"] == f"{preset.fps}/1"
    assert (video["codec_name"], video["pix_fmt"], audio["codec_name"]) == ("h264", "yuv420p", "aac")
    assert int(audio["sample_rate"]) == preset.audio_sample_rate
    assert float(fmt["duration"]) == pytest.approx(4.0, abs=0.15)
    assert _moov_before_mdat(out)
    assert used == {
        "preset": name, "fit": "letterbox", "width": preset.width, "height": preset.height, "fps": preset.fps,
    }  # fmt: skip


def test_without_a_preset_the_render_is_as_before(root):
    used, out = _render(root, _project("wide.mp4"), "plain.mp4")
    video, audio, _ = _probe(out)
    assert (video["width"], video["height"], video["r_frame_rate"]) == (640, 360, "30/1")
    assert used == {"preset": None, "fit": "letterbox", "width": 640, "height": 360, "fps": 30}
    assert _moov_before_mdat(out)


def test_an_explicit_size_beats_the_presets_but_keeps_its_frame_rate(root):
    used, out = _render(root, _project("wide.mp4"), "mixed.mp4", preset="draft-720", width=320, height=180)
    video, _, _ = _probe(out)
    assert (video["width"], video["height"], video["r_frame_rate"]) == (320, 180, "24/1")
    assert used["preset"] == "draft-720" and (used["width"], used["height"]) == (320, 180)


def test_a_crossfade_still_renders_at_the_presets_frame_rate(root):
    p = _project("wide.mp4", "wide.mp4", "wide.mp4")
    p = timeline_ops.set_transition(p, p.tracks[0].clips[2].id, "crossfade", 0.5)
    _, out = _render(root, p, "fade24.mp4", preset="draft-720")
    video, _, fmt = _probe(out)
    assert video["r_frame_rate"] == "24/1" and float(fmt["duration"]) == pytest.approx(5.5, abs=0.2)


# --- letterbox versus crop --------------------------------------------------------------------------------------------


def test_vertical_letterbox_keeps_the_whole_picture_between_black_bars(root):
    _, out = _render(root, _project("wide.mp4"), "v-letterbox.mp4", preset="vertical-1080", fit="letterbox")
    f = _frame(out, 1.0)
    assert f.shape[:2] == (1920, 1080)
    assert _share(f[:200], BLACK, 30) > 0.99 and _share(f[-200:], BLACK, 30) > 0.99  # bars above and below
    band = f[700:1220]  # the 1080x608 picture sits at rows 656..1264
    assert _share(band[:, :40], RED) > 0.9  # the left-edge marker is visible
    assert _share(band[:, -40:], GREEN) > 0.9  # and the right-edge one
    assert _share(f[900:1020, 480:600], WHITE) > 0.9  # and the centre


def test_vertical_crop_fills_the_frame_and_cuts_the_edges(root):
    used, out = _render(root, _project("wide.mp4"), "v-crop.mp4", preset="vertical-1080", fit="crop")
    assert used["fit"] == "crop"
    f = _frame(out, 1.0)
    assert f.shape[:2] == (1920, 1080)
    assert _share(f, BLACK, 30) < 0.001  # no bars anywhere
    assert _share(f, RED) < 0.001 and _share(f, GREEN) < 0.001  # both edge markers were cut off
    assert _share(f[800:1120, 380:700], WHITE) > 0.9  # the centre marker is in the middle of the frame


def test_the_default_fit_is_letterbox(root):
    _, out = _render(root, _project("wide.mp4"), "v-default.mp4", preset="vertical-1080")
    assert _share(_frame(out, 1.0)[:200], BLACK, 30) > 0.99


@pytest.mark.parametrize("source", ["four3.mp4", "square.mp4"])
def test_other_aspects_into_youtube_1080p(root, source):
    _, boxed = _render(root, _project(source), f"y-{source}", preset="youtube-1080p")
    f = _frame(boxed, 1.0)
    assert f.shape[:2] == (1080, 1920)
    assert _share(f[:, :100], BLACK, 30) > 0.99 and _share(f[:, -100:], BLACK, 30) > 0.99  # pillarbox
    assert _share(f, RED) > 0.01 and _share(f, GREEN) > 0.01  # both edge markers inside the picture

    _, covered = _render(root, _project(source), f"yc-{source}", preset="youtube-1080p", fit="crop")
    f = _frame(covered, 1.0)
    assert f.shape[:2] == (1080, 1920)
    assert _share(f, BLACK, 30) < 0.001  # no bars
    assert _share(f[:, :40], RED) > 0.9 and _share(f[:, -40:], GREEN) > 0.9  # width fits exactly, the height is cut
    assert _share(f[440:640, 860:1060], WHITE) > 0.9


def test_crop_fit_does_not_override_a_clips_own_crop(root):
    p = empty_project()
    track = p.tracks[0].id
    p, first = timeline_ops.add_clip(p, track, "split.mp4", 2.0)
    p = timeline_ops.set_clip_properties(p, first.id, crop={"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0})  # right half
    p, _ = timeline_ops.add_clip(p, track, "split.mp4", 2.0)  # uncropped
    _, out = _render(root, p, "crop-crop.mp4", preset="vertical-1080", fit="crop")
    own = _frame(out, 1.0)  # first clip: crop-then-fit, so only blue, in a band with black bars
    assert (
        _share(own, RED) < 0.001 and _share(own[:150], BLACK, 30) > 0.99 and _share(own[800:1100], (0, 0, 255)) > 0.99
    )
    covered = _frame(out, 3.0)  # second clip: the 16:9 source covers the vertical frame
    assert _share(covered, BLACK, 30) < 0.001
    assert (
        _share(covered[:, :500], RED) > 0.95 and _share(covered[:, 580:], (0, 0, 255)) > 0.95
    )  # centre seam, 2 halves


def test_an_overlay_covers_a_frame_shaped_box_under_crop_fit(root):
    p = _project("split.mp4", duration=2.0)
    p, track = timeline_ops.add_track(p, "video")
    p, ov = timeline_ops.add_clip(p, track.id, "wide.mp4", 2.0, start=0.0)
    p = timeline_ops.set_clip_properties(p, ov.id, scale=0.5, x=0.5, y=0.5)
    _, out = _render(root, p, "ov-crop.mp4", preset="vertical-1080", fit="crop")
    f = _frame(out, 1.0)
    # The overlay is 540x960 in the middle, cropped from the wide source: no grey edges bars, centre marker white.
    assert _share(f[960 - 40 : 960 + 40, 540 - 40 : 540 + 40], WHITE) > 0.9
    assert _share(f[960 - 400 : 960 + 400, 280:800], RED) < 0.001  # the source's red edge marker was cut off


# --- the same JSON from every surface ---------------------------------------------------------------------------------


def _build(ws):
    media, store = LocalMediaStore(ws), LocalProjectStore(ws)
    timeline_service.create_project(store, "reel")
    timeline_service.add_clip(store, media, "reel", "wide.mp4", out_point=2.0)


def _json_of(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_http_cli_and_engine_report_the_same_result_for_a_preset_render(root, tmp_path):
    expected = {
        "name": "reel", "output": "reel-render.mp4", "duration": 2.0,
        "preset": "vertical-1080", "fit": "crop", "width": 1080, "height": 1920, "fps": 30,
    }  # fmt: skip

    cli_ws = tmp_path / "cli"
    cli_ws.mkdir()
    shutil.copy(root / "wide.mp4", cli_ws / "wide.mp4")
    _build(cli_ws)
    cli = _json_of(
        RUNNER.invoke(
            app,
            [
                "compose",
                "render",
                "reel",
                "--root",
                str(cli_ws),
                "--preset",
                "vertical-1080",
                "--fit",
                "crop",
                "--json",
            ],
        )
    )
    assert cli == expected and probe_media(cli_ws / "reel-render.mp4").height == 1920

    http_ws = tmp_path / "http"
    http_ws.mkdir()
    shutil.copy(root / "wide.mp4", http_ws / "wide.mp4")
    _build(http_ws)
    client = TestClient(create_app(http_ws, DEFAULTS))
    job = client.post("/api/v1/compose/projects/reel/render", json={"preset": "vertical-1080", "fit": "crop"}).json()[
        "job_id"
    ]
    for _ in range(300):
        status = client.get(f"/api/v1/compose/render/{job}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert status["status"] == "done", status
    result = {k: v for k, v in status.items() if k not in ("status", "progress", "error", "captions")}
    assert result == expected
    assert (probe_media(http_ws / "reel-render.mp4").width, probe_media(http_ws / "reel-render.mp4").height) == (
        1080,
        1920,
    )
    # An explicit size overrides the preset's.
    job = client.post(
        "/api/v1/compose/projects/reel/render",
        json={"preset": "draft-720", "width": 320, "height": 180, "output": "o.mp4"},
    ).json()["job_id"]
    for _ in range(300):
        status = client.get(f"/api/v1/compose/render/{job}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert (status["width"], status["height"], status["fps"], status["preset"]) == (320, 180, 24, "draft-720")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess_matches_the_cli(root, tmp_path, anyio_backend):
    ws = tmp_path / "mcp"
    ws.mkdir()
    shutil.copy(root / "wide.mp4", ws / "wide.mp4")
    _build(ws)
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(ws)]
    )
    async with Client(params) as mcp:
        listed = await mcp.call_tool("list_export_presets", {})
        assert listed.structured_content == presets.listing()
        done = await mcp.call_tool("render_timeline", {"name": "reel", "preset": "vertical-1080", "fit": "crop"})
        assert not done.is_error, done.content
        assert done.structured_content == {
            "name": "reel", "output": "reel-render.mp4", "duration": 2.0,
            "preset": "vertical-1080", "fit": "crop", "width": 1080, "height": 1920, "fps": 30,
        }  # fmt: skip
        bad = await mcp.call_tool("render_timeline", {"name": "reel", "preset": "tiktok", "overwrite": True})
        assert bad.is_error and "unknown preset 'tiktok'; valid presets: draft-720" in bad.content[0].text
    assert (probe_media(ws / "reel-render.mp4").width, probe_media(ws / "reel-render.mp4").height) == (1080, 1920)


def test_unknown_preset_and_fit_fail_the_same_way_on_http_and_cli(root, tmp_path):
    ws = tmp_path / "bad"
    ws.mkdir()
    shutil.copy(root / "wide.mp4", ws / "wide.mp4")
    _build(ws)
    message = "unknown preset 'tiktok'; valid presets: draft-720, square-1080, vertical-1080, youtube-1080p"
    client = TestClient(create_app(ws, DEFAULTS))
    res = client.post("/api/v1/compose/projects/reel/render", json={"preset": "tiktok"})
    assert res.status_code == 422 and message in json.dumps(res.json())
    res = client.post("/api/v1/compose/projects/reel/render", json={"fit": "stretch"})
    assert res.status_code == 422 and "valid fits: letterbox, crop" in json.dumps(res.json())
    cli = RUNNER.invoke(app, ["compose", "render", "reel", "--root", str(ws), "--preset", "tiktok"])
    assert cli.exit_code == 1 and message in cli.output.replace("\n", "")
    assert not (ws / "reel-render.mp4").exists()


def test_the_presets_listing_is_the_same_on_http_and_cli(tmp_path):
    http = TestClient(create_app(tmp_path, DEFAULTS)).get("/api/v1/presets")
    assert http.status_code == 200 and http.json() == presets.listing()
    assert _json_of(RUNNER.invoke(app, ["presets", "--json"])) == presets.listing()
    table = RUNNER.invoke(app, ["presets"])
    assert table.exit_code == 0 and "vertical-1080" in table.output and "1080x1920" in table.output
