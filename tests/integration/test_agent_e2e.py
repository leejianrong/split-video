"""End to end: a scripted MCP client does what an agent would, on a long synthetic recording (SPL-57).

It starts the real server (`split-video mcp`, a subprocess over stdio) and follows the agent guide: scan_library,
prepare_asset, media_report, transcribe, analyze_audio, suggest_highlights, a confirming look at the words,
apply_plan (dry run, then for real), set_clip_crop to 9:16, captions, queue_renders and render_queue_status. Then it
checks the files with ffprobe and one decoded frame of a vertical clip. What it does not do is call a model: the
"agent" is this script, and the transcript is a synthetic one stored through `transcribe.save` (a public function;
CI has no faster-whisper model). See docs/e2e-agent-test.md.

The recording is generated with ffmpeg (nothing is committed): quiet noise, with THREE planted highlights (a loud
tone with dense speech and the keyword "finally") and one loud DECOY with no speech, at known times. Length:
`SPLIT_VIDEO_E2E_HOURS` (default 0.4 h = 24 min; run 3 for the long version).

Every call to the server has a deadline, the whole run has a global one, and the server is killed by pid at the end.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import anyio
import pytest
from fake_transcript import document_from_words
from mcp import Client, StdioServerParameters

from split_video import assets, transcribe
from split_video.local_media_store import LocalMediaStore

HOURS = float(os.environ.get("SPLIT_VIDEO_E2E_HOURS", "0.4"))
DURATION = round(HOURS * 3600)
GLOBAL_DEADLINE = float(os.environ.get("SPLIT_VIDEO_E2E_DEADLINE", str(600 + 4 * 3600 * HOURS)))
PLANT_SECONDS = 40
# Where the planted highlights and the decoy are, as fractions of the recording.
PLANTED = [round(DURATION * f) for f in (0.2, 0.5, 0.8)]
DECOY = round(DURATION * 0.35)
DECOY_SECONDS = 25
VERTICAL = 3
CLIP_SECONDS = 20
# The picture: red box on the left, yellow on the right of a 640x360 frame (x, w; y 100..260 for both).
RED = (20, 120)
YELLOW = (500, 120)
REPORT: list[str] = []


def run_ffmpeg(args: list[str], timeout: float) -> None:
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]


def make_recording(path, duration: int) -> None:
    bursts = "+".join(f"between(t,{a},{a + PLANT_SECONDS})" for a in PLANTED)
    audio = (
        f"aevalsrc='0.01*(random(0)*2-1)+({bursts})*0.5*sin(2*PI*330*t)*(0.6+0.4*sin(2*PI*3*t))"
        f"+between(t,{DECOY},{DECOY + DECOY_SECONDS})*0.5*(random(1)*2-1)':s=8000:d={duration}"
    )
    picture = (
        "color=c=0x305090:s=640x360:r=5,"
        f"drawbox=x={RED[0]}:y=100:w={RED[1]}:h=160:color=red:t=fill,"
        f"drawbox=x={YELLOW[0]}:y=100:w={YELLOW[1]}:h=160:color=yellow:t=fill"
    )
    run_ffmpeg(
        ["-f", "lavfi", "-i", picture, "-f", "lavfi", "-i", audio, "-t", str(duration)]
        + ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-g", "50", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", "-b:a", "24k", "-ac", "1", "-shortest", str(path)],
        timeout=300 + 600 * HOURS,
    )


def transcript_words(duration: int) -> list[tuple[float, float, str]]:
    """Sparse chatter all the way through, dense speech (with "finally") in the planted windows, none in the decoy."""
    vocab = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    words, t, i = [], 1.0, 0
    while t < duration - 2:
        planted = next((a for a in PLANTED if a <= t < a + PLANT_SECONDS), None)
        if planted is not None:
            word = "finally" if abs(t - (planted + 10)) < 0.2 else vocab[i % 8]
            words.append((round(t, 3), round(t + 0.3, 3), word))
            t += 0.35
        elif DECOY <= t < DECOY + DECOY_SECONDS:
            t = DECOY + DECOY_SECONDS
        else:
            words.append((round(t, 3), round(t + 0.4, 3), vocab[i % 8]))
            t += 2.5
        i += 1
    return words


def probe(path) -> dict:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    info = json.loads(done.stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    return {"w": video["width"], "h": video["height"], "duration": float(info["format"]["duration"]), "audio": audio}


def frame_rgb(path, at: float, size=(1080, 1920)) -> bytes:
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1"]
        + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0 and len(done.stdout) == size[0] * size[1] * 3, done.stderr[-400:]
    return done.stdout


def colour_box(rgb: bytes, size, wanted) -> tuple[int, tuple[int, int, int, int] | None]:
    """Pixel count and bounding box (x0, y0, x1, y1) of the pixels `wanted(r, g, b)` accepts, on a coarse grid."""
    width, height = size
    count, x0, y0, x1, y1 = 0, width, height, -1, -1
    for y in range(0, height, 4):
        row = y * width * 3
        for x in range(0, width, 4):
            r, g, b = rgb[row + x * 3], rgb[row + x * 3 + 1], rgb[row + x * 3 + 2]
            if wanted(r, g, b):
                count += 1
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return count, (x0, y0, x1, y1) if count else None


def is_yellow(r, g, b):
    return r > 200 and g > 200 and b < 90


def is_red(r, g, b):
    return r > 200 and g < 70 and b < 70


def overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def kill_servers(marker: str) -> None:
    """Belt and braces: SIGKILL every process whose command line has our unique workspace path (Linux)."""
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            if marker in cmd and "mcp" in cmd:
                os.kill(int(name), signal.SIGKILL)
        except OSError:
            pass


class Script:
    """The scripted client: every call has a deadline and none outlives the global one."""

    def __init__(self, mcp: Client, deadline: float):
        self.mcp, self.deadline, self.steps = mcp, deadline, {}

    async def raw(self, tool: str, args: dict | None = None, *, timeout: float = 120):
        remaining = self.deadline - time.monotonic()
        assert remaining > 0, f"global deadline passed before {tool}"
        started = time.monotonic()
        with anyio.fail_after(min(timeout, remaining)):
            result = await self.mcp.call_tool(tool, args or {})
        took = time.monotonic() - started
        self.steps[tool] = self.steps.get(tool, 0) + took
        if tool != "render_queue_status":
            REPORT.append(f"  {tool:<22} {took:7.2f} s")
        return result

    async def call(self, tool: str, args: dict | None = None, *, timeout: float = 120):
        result = await self.raw(tool, args, timeout=timeout)
        assert not result.is_error, (tool, result.content[0].text[:600])
        return result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.timeout(int(GLOBAL_DEADLINE) + 120)
async def test_agent_workflow_from_a_long_recording_to_a_reel_and_vertical_clips(tmp_path, anyio_backend):
    started = time.monotonic()
    deadline = started + GLOBAL_DEADLINE
    root = tmp_path / "e2e-workspace"
    root.mkdir()
    stream = root / "stream.mp4"
    make_recording(stream, DURATION)
    REPORT.append(f"recording {DURATION} s generated in {time.monotonic() - started:.1f} s")
    marker = str(root)
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", marker]
    )
    try:
        async with Client(params) as mcp:
            agent = Script(mcp, deadline)
            tools = {t.name for t in (await mcp.list_tools()).tools}
            assert {"scan_library", "suggest_highlights", "queue_renders", "set_clip_crop"} <= tools

            # 1. Import
            scan = await agent.call("scan_library", {"folder": ""}, timeout=300)
            [added] = [a for a in scan["added"] if a["path"] == "stream.mp4"]
            asset = added["id"]
            await agent.call("prepare_asset", {"asset": asset}, timeout=300 + 600 * HOURS)

            # 2. Describe
            report = await agent.call("media_report", {"asset": asset, "prepare": True}, timeout=300 + 600 * HOURS)
            assert report["signals"]["available"]["loudness"] and report["signals"]["available"]["silence"]

            # 3. Analyse. The transcript is a synthetic one, stored through the public `transcribe.save` (CI has no
            # model); then transcribe_asset finds it current (or, without the extra, says so and carries on).
            store = LocalMediaStore(root)
            transcribe.save(
                store, assets.get_asset(store, asset), document_from_words(asset, transcript_words(DURATION))
            )
            transcribed = await agent.raw("transcribe_asset", {"asset": asset})
            if transcribed.is_error:
                assert "extra.transcription_missing" in transcribed.content[0].text
            else:
                assert transcribed.structured_content["made"] is False
            # YAMNet is a gitignored model: present on a dev machine, absent in CI.
            audio = await agent.raw("analyze_audio", {"video": "stream.mp4"}, timeout=300 + 600 * HOURS)
            audio_used = not audio.is_error
            if audio_used:
                assert audio.structured_content["total_seconds"] > 0
            else:
                assert "model.missing" in audio.content[0].text

            # 4. Pick moments, and confirm them by reading the words (what an agent does before trusting a pick).
            wanted = len(PLANTED) + 2
            found = await agent.call(
                "suggest_highlights",
                {"asset": asset, "preset": "stream", "count": wanted, "min_seconds": 20, "max_seconds": 60}
                | {"keywords": ["finally"]},
            )
            candidates = found["candidates"]
            assert found["signals"]["used"], found["signals"]
            recall_hits = [
                max(overlap(c["start"], c["end"], a, a + PLANT_SECONDS) for c in candidates) for a in PLANTED
            ]
            for planted_at, hit in zip(PLANTED, recall_hits, strict=True):
                assert hit >= PLANT_SECONDS * 0.5, (planted_at, hit, [(c["start"], c["end"]) for c in candidates])
            true_positive = [
                c
                for c in candidates
                if any(overlap(c["start"], c["end"], a, a + PLANT_SECONDS) >= 0.5 * c["duration"] for a in PLANTED)
            ]
            REPORT.append(
                f"highlights: {len(candidates)} candidates, recall {len(PLANTED)}/{len(PLANTED)} planted covered, "
                f"precision {len(true_positive)}/{len(candidates)}; audio classes used: {audio_used}"
            )
            confirmed = []
            for c in sorted(candidates, key=lambda c: c["rank"]):
                words = await agent.call("transcript_range", {"asset": asset, "start": c["start"], "end": c["end"]})
                if any("finally" in i["text"].lower() for i in words["items"]):
                    confirmed.append(c)
            assert len(confirmed) >= len(PLANTED), "the words should confirm every planted highlight"
            confirmed = sorted(confirmed, key=lambda c: c["start"])[:VERTICAL]

            # 5. Plan: one reel from the confirmed candidates (dry run, then for real).
            clips = [
                {"asset": asset, "in": c["start"], "out": c["end"]}
                | ({"transition": "crossfade", "transition_duration": 1} if n else {})
                for n, c in enumerate(confirmed)
            ]
            reel_plan = {"schema_version": 1, "project": "reel", "tracks": [{"kind": "video", "clips": clips}]}
            dry = await agent.call("apply_plan", {"plan": reel_plan, "dry_run": True})
            assert not dry["warnings"] or isinstance(dry["warnings"], list)
            reel_expected = dry["duration"]
            assert reel_expected == pytest.approx(
                sum(c["end"] - c["start"] for c in confirmed) - (len(clips) - 1), abs=0.5
            )
            applied = await agent.call("apply_plan", {"plan": reel_plan})
            assert applied["duration"] == pytest.approx(reel_expected, abs=0.01)

            # 6. Vertical: one clip each, cropped to 9:16 around the red box (left), the yellow box (right) or the middle.
            centres = [0.13, 0.87, 0.5]
            expected = {}
            renders = [{"project": "reel", "preset": "youtube-1080p", "output": "reel.mp4"}]
            for n, c in enumerate(confirmed, start=1):
                name = f"short{n}"
                plan = {
                    "schema_version": 1,
                    "project": name,
                    "tracks": [
                        {
                            "kind": "video",
                            "clips": [{"asset": asset, "in": c["start"], "out": c["start"] + CLIP_SECONDS}],
                        }
                    ],
                }
                await agent.call("apply_plan", {"plan": plan, "dry_run": True})
                await agent.call("apply_plan", {"plan": plan})
                await agent.call(
                    "set_clip_crop", {"name": name, "clip": 1, "aspect": "9:16", "center_x": centres[n - 1]}
                )
                renders.append(
                    {"project": name, "preset": "vertical-1080", "captions": "shorts", "output": f"{name}.mp4"}
                )
                expected[name] = CLIP_SECONDS

            # 7. Render everything at once and poll with a deadline.
            queued = await agent.call("queue_renders", {"renders": renders})
            batch, queued_at = queued["batch_id"], time.monotonic()
            status = None
            while time.monotonic() < deadline:
                status = await agent.call("render_queue_status", {"batch_id": batch})
                if status["finished"]:
                    break
                await anyio.sleep(2)
            assert status is not None and status["finished"], status
            REPORT.append(f"  render batch           {time.monotonic() - queued_at:7.2f} s")
            failed = [j for j in status["jobs"] if j["state"] != "done"]
            assert not failed, failed
    finally:
        kill_servers(marker)

    # 8. The files.
    reel = probe(root / "reel.mp4")
    assert (reel["w"], reel["h"]) == (1920, 1080) and reel["audio"]
    assert reel["duration"] == pytest.approx(reel_expected, abs=1.0)
    shots = {}
    for name, seconds in expected.items():
        info = probe(root / f"{name}.mp4")
        assert (info["w"], info["h"]) == (1080, 1920) and info["audio"], (name, info)
        assert info["duration"] == pytest.approx(seconds, abs=1.0), (name, info)
        shots[name] = frame_rgb(root / f"{name}.mp4", 5.0)
    # The crop: short1 follows the red box (left) and short2 the yellow one (right); neither shows the other.
    size = (1080, 1920)
    for name, wanted_box, other_box in (("short1", is_red, is_yellow), ("short2", is_yellow, is_red)):
        count, box = colour_box(shots[name], size, wanted_box)
        assert count > 500 and box is not None, (name, count)
        assert colour_box(shots[name], size, other_box)[0] == 0, f"{name} shows the wrong box"
        w, h = box[2] - box[0], box[3] - box[1]
        # The box was 120x160 of a 360 px high picture, shown 1920 px high: 640x853, not stretched or letterboxed.
        assert 0.6 * 640 < w < 1.1 * 640 and 0.9 * 853 < h < 1.1 * 853, (name, box)
        REPORT.append(f"frame {name}: box bbox {box}, {count} grid pixels, size {w}x{h}")
    # short3 is centred: neither whole box fits in the middle 203 px, so at most slivers of them show.
    middle = colour_box(shots["short3"], size, lambda r, g, b: is_red(r, g, b) or is_yellow(r, g, b))[0]
    REPORT.append(f"frame short3: {middle} red/yellow grid pixels")
    REPORT.append(f"total {time.monotonic() - started:.1f} s for a {DURATION} s recording")
    print("\nE2E timings\n" + "\n".join(REPORT))
