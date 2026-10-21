# The end-to-end agent test

The headline claim is that an agent can turn a long recording into a highlights reel and vertical shorts through the
MCP tools, without reading the recording. `tests/integration/test_agent_e2e.py` tests it. It runs in the normal
`uv run pytest` and in the CI `test` job.

## What it proves

A scripted client (a real MCP client over stdio, against a real `split-video mcp --root DIR` subprocess) follows the
[agent guide](../src/split_video/agent_guide/SKILL.md) on a synthetic recording:

1. `scan_library`, `prepare_asset`, `media_report` (prepare true), `transcribe_asset`, `analyze_audio`.
2. `suggest_highlights` (preset `stream`, keyword "finally"), then `transcript_range` on every candidate to confirm it
   by its words, the way an agent should.
3. `apply_plan` with `dry_run` first, then for real: one reel of the confirmed candidates (crossfaded), and one
   single-clip timeline per vertical clip, each cropped with `set_clip_crop` (`aspect` "9:16").
4. `queue_renders`: the reel at `youtube-1080p` and 3 clips at `vertical-1080` with `captions` "shorts", then
   `render_queue_status` polled until the batch is finished.

It asserts, with ffprobe and a decoded frame:

- the reel is 1920x1080 with an audio stream, its duration within 1 s of the dry run's `duration`;
- every vertical clip is 1080x1920 with audio, 20 s within 1 s;
- **highlights**: each of the 3 planted moments is covered (at least half of it) by a candidate; the precision
  (candidates that sit on a planted moment) is printed, not asserted, because the recording has a decoy;
- **crop**: in one frame of each clip the coloured box the crop follows is whole and unstretched (632x844 px for a
  box that was 120x160 in a 360 px high picture), and the box on the other side of the picture is absent.

## The synthetic recording

Made by ffmpeg `lavfi` in the test (a few seconds for 24 minutes; nothing is committed): 640x360 at 5 fps with a red
box at the left and a yellow one at the right, and 8 kHz mono audio of quiet noise with, at 20 %, 50 % and 80 % of the
length, a 40 s **planted highlight** (a loud pulsing tone) and, at 35 %, a 25 s **decoy** (loud noise, no speech).

The transcript is synthetic too: sparse chatter all the way through, dense speech and the word "finally" in each planted
window, nothing in the decoy. CI has no faster-whisper model, so the test stores it with `transcribe.save` (the
function the real transcription ends with) before calling `transcribe_asset`, which then finds it current
(`made` false). Without the optional extra the tool answers `extra.transcription_missing` and the test accepts that and
carries on. `analyze_audio` needs the YAMNet model, which is a gitignored file: with it the audio classes are used,
without it the test accepts `model.missing`. Both runs pass; the classes are not needed to find the planted moments.

Every call has a deadline, and there is a global one (`SPLIT_VIDEO_E2E_DEADLINE`, seconds; default 10 minutes plus
4 s per second of recording hours). ffmpeg runs with `-nostdin` and a timeout, pytest-timeout is set to the global
deadline, and the server (and anything with the workspace path in its command line) is killed in a `finally`.

## Running it

```bash
uv run pytest tests/integration/test_agent_e2e.py -s        # 24 min recording, about 1 min; -s prints the timings
SPLIT_VIDEO_E2E_HOURS=3 uv run pytest tests/integration/test_agent_e2e.py -s   # the 3 h version, about 2.5 minutes
```

## Results

Measured on a 16 core machine (CI numbers are in the PR). The 24 minute run, wall time by step (YAMNet present):
scan 0.1 s, prepare_asset 5.8 s, media_report 5.1 s, suggest_highlights 0.03 s, render batch (4 renders, 2 at a time)
36 s, about 54 s in all. It found 4 candidates for 3 planted moments: recall 3/3, precision 3/4 (the decoy is the
fourth: the loudness and the lack of silence rank it, with no words to mark it down). 

The 3 hour run (`SPLIT_VIDEO_E2E_HOURS=3`, 10800 s): recording made in 27 s, scan 0.1 s, prepare_asset 43 s,
media_report (prepare, with YAMNet) 37 s, suggest_highlights 0.14 s, render batch 36 s (the clips are the same length
whatever the recording), 147 s in all, peak 912 MB for the largest process (the server or the test). Same result:
recall 3/3, precision 3/4. Without the YAMNet model the same run is faster and the same picks are found.

## Running the same workflow on a real stream

The test cannot judge whether a pick is good, only that the tools produce, locate and render what they say. For a
real stream, drive the same calls with a model in the loop (an agent with the server attached) or by hand:

```bash
uv sync --extra transcribe                 # or the Docker image built with WITH_TRANSCRIPTION=1
split-video mcp --root /path/to/streams    # attach this to your agent; it should read guide://agent
```

or the same steps on the CLI, which takes the video path: `split-video library scan DIR`, `split-video transcribe stream.mp4`,
`split-video report stream.mp4 --prepare`, `split-video highlights stream.mp4 --preset stream`, `split-video apply-plan`
(see `--help` of each), then `split-video compose render-queue batch.json`.
What to expect for 2.5 hours of video on a 16 core CPU with no GPU:

| Step | Time | Memory |
| --- | --- | --- |
| `transcribe_asset` (base.en) | about 17 min, once | about 1 GB |
| `analyze_audio` / `media_report` with prepare | about 30 s | under 1 GB |
| `prepare_asset` (proxy, thumbnails) | minutes per hour of video | under 1 GB |
| `suggest_highlights`, searches | well under a second | small |
| each render | roughly real time of the clip or less | about 0.8 GB, 2 at a time |

Transcription is what dominates; everything else is seconds to minutes. Give the client a tool timeout above 20 minutes
or run the transcription as a job over HTTP, where it has progress and can be cancelled.
