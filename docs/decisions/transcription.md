# Decision: local transcription with faster-whisper (SPL-42)

Status: accepted (2026-10-19). Card SPL-42. Code: `src/split_video/transcribe.py`.

## Decision

- **Library: faster-whisper 1.2.x** (MIT; the CTranslate2 runtime, also MIT). Word timestamps, a CPU int8 mode
  and a bundled voice-activity model (Silero VAD, an ONNX file inside the wheel, so no extra download). Weights are
  OpenAI's Whisper models converted by Systran (`Systran/faster-whisper-*` on Hugging Face), all MIT.
- **Optional extra**: `[project.optional-dependencies] transcribe = ["faster-whisper>=1.1"]`, imported lazily.
  The default `uv sync` and the default Docker image do not contain it.
- **Default model: `base.en`** (English, int8, CPU). `--model` / `model` takes any faster-whisper name, so
  `small.en` is the careful option and `base` / `small` the multilingual ones (language detected or given).
- **Model location**: downloaded on first use into the workspace (`MediaStore.models_dir()`, locally
  `<root>/.split-video-models/`), never `~/.cache`, never baked into the base image. Present means offline.
- **Windows**: audio is streamed (16 kHz mono) and transcribed in 5-minute windows cut at the quietest 0.1 s of the
  last 30 s, not overlapping, so seams cannot duplicate words. `vad_filter=True`, `condition_on_previous_text=False`.

## Measured on this machine

16 cores (WSL2), 7.8 GB RAM of which 2-5 GB free, no GPU, Python 3.12, faster-whisper 1.2.1, ctranslate2 4.8.3,
onnxruntime 1.31. Speech: 8 LibriSpeech dev-clean utterances joined, 86.3 s, English read speech. Real-time factor
(RTF) = transcription seconds / audio seconds, model already loaded, int8, beam 5, VAD on, word timestamps on.
"WER" is a rough word error rate on lower-cased text against the reference; "Mister" vs "Mr" counts as an error, so
read it as relative only.

| Model | Download | Load | RAM after load | Peak RAM, 86 s | RTF | Words (ref 198) | WER |
|---|---|---|---|---|---|---|---|
| tiny.en | 75 MB | 0.8 s | 247 MB | 388 MB | 0.035 | 199 | 8.0% |
| **base.en** | **145 MB** (139 MB model.bin) | 0.8 s | 338 MB | 456 MB | **0.051** | 198 | 8.0% |
| small.en | 484 MB (462 MB model.bin) | 1.4 s | 622 MB | 907 MB | 0.153 | 200 | 9.0% |
| base (multilingual) | 145 MB | 7.2 s | 374 MB | 550 MB | 0.057 | 200 | not scored |

On clean read speech the three English models are indistinguishable by this measure, so the choice is cost:
`base.en` is the cheapest model with good word timing and a small download. `tiny.en` is 30% faster and, by this sample,
no worse, but a smaller model is the first to degrade on noisy audio (not measured here); `small.en` triples the time and the download for
no gain on this sample. Real recordings (music, crowds) are harder than this sample, which is the argument for
leaving `small.en` one flag away. Turning VAD off changed nothing on this clean speech (RTF 0.054, 198 words); it
matters on recordings with long non-speech stretches.

CPU threads: the library default (4) is right. 8 threads gave the same RTF (0.051); 16 threads, all cores, was
13 times slower (RTF 0.668), apparently oversubscription under WSL2. So `cpu_threads` is left at the default.

**Long file, end to end** (real model, through the CLI): a 1 h 0 m 40 s mp4 (AAC audio, tiny video) made by
repeating the 86 s sample with 5 s gaps 40 times: **146.5 s wall, RTF 0.040, peak RSS 795 MB**, 8013 words in 614
segments across 13 windows; "apostle" was found 40 times (once per repeat, at the right second to within 0.12 s), no
word repeated at a seam, word times monotonic, last word ends at 3638.7 s of 3640. A 20-minute cut of it:
RTF 0.038-0.044 and peak RSS by window length 60 s: 491 MB, 120 s: 539 MB, 300 s: 703 MB, 600 s: 1090 MB. Memory is
set by the window, not by the file length (the 20-minute and 1-hour runs peak alike), and it was 5 minutes that made
the 7 GB machine comfortable, so that is `WINDOW_SECONDS`.

A real 11 s clip is the test fixture (`tests/fixtures/speech_clip.ogg`, 27 KB, licence in
`speech_clip.LICENSE.txt`): `base.en` transcribes it with all but at most three words right and the pause
between its two sentences at the second it was inserted.

## Install weight

Default install (`uv sync`): `.venv` is 200 MB. With the extra: 560 MB, **+360 MB**. Wheel downloads, 108 MB in
all: ctranslate2 40.3 MB (137 MB installed, with its bundled libs), av 35.0 MB (127 MB installed: ffmpeg libraries),
onnxruntime 23.8 MB (63 MB), hf_xet 4.2 MB, tokenizers 3.4 MB, faster-whisper 1.1 MB, huggingface-hub 0.8 MB, plus
pyyaml, tqdm, filelock and fsspec. numpy, anyio, httpx and click are already dependencies. `uv.lock` gains the extra's
packages; the default `uv sync --frozen` is unchanged (CI runs it and installs none of them).

Known sharp edges, all handled:

- faster-whisper 1.2.1 opens *file paths* with PyAV, and the newest PyAV (19) rejects the arguments it passes
  (`open() got an unexpected keyword argument 'metadata_errors'`). We never pass a path: audio arrives as a numpy
  array from our own ffmpeg decode, so it is not hit. Do not switch to faster-whisper's file input without
  pinning `av`.
- huggingface-hub's xet downloader writes its cache under `~/.cache/huggingface/xet` even when `download_root` is
  set. `transcribe.py` sets `HF_HOME` to the workspace models folder and `HF_HUB_DISABLE_XET=1` before importing, and
  a test (opt-in, `SPLIT_VIDEO_TEST_DOWNLOAD=1`) runs a real first-use download with a fresh `$HOME` and finds no
  Hugging Face file there. onnxruntime itself writes a 36-byte device-id file under `~/.cache/Microsoft`, which is
  not model data and which we cannot move.
- The multilingual `base` loads in 7 s the first time (language-detection tables), `base.en` in under a second.

## Docker image

The default image does not change (a build argument defaults to off). Variants:

```
docker build --build-arg WITH_TRANSCRIPTION=1 -t split-video:transcribe .
docker build --build-arg WITH_TRANSCRIPTION=1 --build-arg TRANSCRIPTION_MODEL=base.en -t split-video:transcribe .
```

The first installs the extra. The second also downloads `base.en` at build time into `/opt/split-video-models` and
sets `SPLIT_VIDEO_MODEL_SEED` to it: a read-only fallback consulted after the workspace's own folder and before the
network, so an offline machine works from the first run. Without it the model is fetched on first use into the
mounted folder's `.split-video-models/`. See the numbers measured for this card below.

Measured with `docker build` on this machine (Docker 29, `docker images` sizes; each build took about 8-20 s with warm
layer and wheel caches, so these timings say little about a cold CI build):

| Image | Size | Delta |
|---|---|---|
| default (`split-video:spl42-base`) | 781 MB | |
| `--build-arg WITH_TRANSCRIPTION=1` | 1.15 GB | +about 370 MB |
| `... --build-arg TRANSCRIPTION_MODEL=base.en` | 1.31 GB | +about 530 MB (the extra plus the 139 MB model, 160 MB on disk) |

Checked by running them: the default image's `transcribe` exits 4 with the install hint; the seeded image
transcribed the fixture clip with `--network none` (so offline, from the baked model, writing only
`.split-video-models/`, `.split-video-cache/` and `.split-video-assets.json` into the mounted folder); and
`--build-arg TRANSCRIPTION_MODEL=base.en` without `WITH_TRANSCRIPTION=1` fails the build with a message saying so.
Not verified: a cold-cache build in CI, and the publish workflow still builds only the default image (no matrix
entry was added for the variant, since it is opt-in).

## Surfaces and errors

- CLI: `split-video transcribe <path-or-asset> [--root DIR] [--model base.en] [--language en] [--force] [--json]`
  and `split-video transcript <path-or-asset> [--start S] [--end S] [--json]` (never runs the model).
- MCP: `transcribe_asset(asset, model, language, force)` and `get_transcript(asset, start, end)`.
- HTTP: `POST /api/v1/assets/{id}/transcribe` queues a `transcription` job (progress in thousandths of the audio,
  cancellable between segments with the decode terminated) and returns `{job_id}`; `GET /api/v1/assets/{id}/transcript`
  (404 until it exists; `?start=&end=` cuts it). Asset summaries gain `transcript: bool`.
- Missing dependency: CLI exit code 4 with the install hint; MCP tool error with the same text; HTTP 501 whose
  `detail` is `transcribe.UNAVAILABLE`, checked before anything is queued. No audio track: CLI/MCP error, HTTP 422.
- The job kind is `transcription`, the name `jobs.py` already reserved (the card says `transcribe`).

## Not done

Speaker labels and translation (out of scope). A GPU path (`device="cpu"` is fixed). Per-asset model memory is not
shared between jobs: each job loads the model (under a second for `base.en`) and frees it.
