# Analysis tools: transcription, scenes, frames, OCR, search, report, highlights

Everything an agent (or you) can read about a long recording. Each tool exists on the CLI, over HTTP and as an MCP tool. Run the examples as `docker run ... ghcr.io/leejianrong/split-video ...`, or without the `docker run` prefix on a local install; the optional features need the image built with a build arg, shown below.

## Transcription (optional)

Speech to text with a word-level timestamp for every word, run locally (CPU,
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), MIT). It is not in
the default image because it adds about 340 MB. Build the image with it:

```bash
docker build --build-arg WITH_TRANSCRIPTION=1 -t split-video:transcribe .
# optional: also bake the 139 MB base.en model in, so the first run needs no internet
docker build --build-arg WITH_TRANSCRIPTION=1 --build-arg TRANSCRIPTION_MODEL=base.en -t split-video:transcribe .

docker run --rm -v "$PWD":/data split-video:transcribe transcribe liveshow.mp4
docker run --rm -v "$PWD":/data split-video:transcribe transcript liveshow.mp4 --start 60 --end 90
```

Outside Docker: `uv sync --extra transcribe` in a checkout, or `pip install
'split-video[transcribe]'`. Without it, `transcribe` (and the API and MCP
equivalents) say how to install it: the CLI exits with code 4, `POST
/api/v1/assets/{id}/transcribe` answers 501 with that text as its `detail`, and
the MCP tool returns a tool error.

`transcribe` takes a file under `--root` or an asset id, and `--model` (default
`base.en`; `small.en` is slower and a little more careful, `base` or `small`
handle other languages) and `--language`. The model downloads on first use into
`.split-video-models/` in the folder you mounted, not into the image or
`~/.cache`, so it is fetched once per folder, and with the model there it works
offline. The transcript is stored next to the video (`.split-video-cache/`) and
goes stale if the file changes: segments of words, each with `start`, `end`,
`word` and `probability`, in seconds from the start of the audio (format in
`src/split_video/transcribe.py`). A multi-hour file is read in 5-minute windows,
so memory stays flat. Over HTTP it is a `transcription` job
(`POST /api/v1/assets/{id}/transcribe`, with progress and cancel), read back with
`GET /api/v1/assets/{id}/transcript` (optionally `?start=&end=`); MCP has
`transcribe_asset` and `get_transcript`, and `list_assets` shows `transcript`.

Reading a long transcript without loading it whole: `split-video transcript
<target> --start 600 --end 900 --limit 50` prints one page of segments (`--words`
for words, `--max-chars` for a text budget) and the `--cursor` for the next;
`split-video transcript-search <target> "magic word" --context 8` lists where a
phrase is said, with the time and the words around it (case and punctuation
ignored, whole words, across segment boundaries; no regex or semantic search).
Over HTTP the same pages come from `GET /api/v1/assets/{id}/transcript?limit=&cursor=&granularity=`
(without those parameters it still returns the whole document) and
`GET /api/v1/assets/{id}/transcript/search?q=&start=&end=&limit=&cursor=&context=`;
MCP has `transcript_range` and `transcript_search`. All three return the same JSON.
A page holds at most 500 items (searches 200); a segment overlapping the start of
the range is included whole. Searching three hours of speech takes milliseconds.
Speaker labels and translation are not included. The numbers behind the
defaults are in [`docs/decisions/transcription.md`](decisions/transcription.md).

## Scene changes and screen activity (`scenes`)

For a coding stream or a lecture the interesting moments are on screen, not in the audio. One cheap ffmpeg
pass over the proxy (the original if there is none; 5 frames per second, 160 px wide, nothing re-encoded)
finds the cuts and measures how much the picture changes:

```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video scenes stream.mp4 --root /data [--threshold 0.15] [--screen-threshold 0.02] [--min-gap 1] [--json]
```

You get each scene change's `time`, `kind`, `score` and `changed`, and one 0..1 `activity` value per second (still
screen 0, typing low, a switch near 1). Two rules run in the same pass. `kind: "cut"` is ffmpeg's `scdet` (default
`--threshold 0.15`): a camera cut or a switch between windows with different backgrounds (a light slide to a dark
editor scores 0.8, a hard cut between two very different test pictures 0.2). `kind: "screen"` finds a text-only change on a screen with
the same background (one dark text pane swapped for another scores only 0.01 with scdet, which a threshold cannot
separate from noise): the fraction `changed` of the picture that changed in one isolated frame must reach
`--screen-threshold` (default 0.02; 0 turns it off; a pane swap measured 0.045, typing under 0.003, a blinking cursor
0.0008, scrolling code a continuous 0.26 that never fires). Scene changes under `--min-gap` seconds apart count as
one (a `cut` beats a `screen` change). OCR (`ocr`) reads a frame after every scene change of either kind. Results are stored next to the
video, record whether the proxy or the original was read, and go stale if the file changes (format in
`src/split_video/scenes.py`). Slow fades are not cuts. Over HTTP it is a `scene_analysis` job
(`POST /api/v1/assets/{id}/scenes`, with progress and cancel), read with `GET /api/v1/assets/{id}/scenes`
(optionally `?start=&end=`); MCP has `analyze_scenes` and `get_scenes`, and `list_assets` shows `scenes`.

## A picture at a time (`frame`)

Agents cannot judge an edit they cannot see. This returns one JPEG or PNG of a video at a time, optionally scaled:

```bash
docker run --rm -v "$PWD":/data -w /data ghcr.io/leejianrong/split-video frame stream.mp4 1:02:03 --root /data [--out shot.jpg] [--width 640] [--format png] [--source original]
```

Without `--out` the file is `<name>-<time>s.jpg` in the current directory (never in the workspace's cache). By
default the frame comes from the proxy when there is one (at most 640 px wide and 15 fps, so within 1/15 s of the
time you asked for); `--source original` reads the file itself for full detail. A time past the end is an error that
says how long the video is; a request at the end returns the last frame; pictures are never scaled up. Over HTTP it is
`GET /api/v1/assets/{id}/frame?time=&width=&height=&format=&source=` (the image bytes, with `X-Frame-*` headers); MCP
has `get_frame`, which returns an image block plus a short text block with the time, size and source.

## Skim a stretch in one picture (`contact-sheet`)

One image of 12 frames lets an agent (or you) skim ten minutes of a stream cheaply. Each tile has its time burned
into the top-left corner:

```bash
docker run --rm -v "$PWD":/data -w /data ghcr.io/leejianrong/split-video contact-sheet stream.mp4 --root /data --start 1:00:00 --end 1:10:00 [--count 12] [--columns 4] [--tile-width 320] [--out sheet.jpg] [--json]
```

The range is cut into `--count` equal slices (1 to 64, default 12) and each tile is the frame at the centre of its
slice, laid out left to right, top to bottom (12 tiles make 4x3; the image is at most 2560 px wide). It costs one
seek per tile, so it takes about as long on a 3 hour file as on a 3 minute one. `--json` (and the MCP text block)
gives the time and pixel position of every tile. Over HTTP it is
`GET /api/v1/assets/{id}/contact-sheet?start=&end=&count=&columns=&tile_width=&format=&source=` (the image, with
`X-Sheet-*` headers and the tile times in `X-Sheet-Times`) and `.../contact-sheet/info` (the same map as JSON, nothing
rendered); MCP has `get_contact_sheet`, an image block plus the tile times. Timestamps need a TrueType font (the
Docker image has one; elsewhere set `SPLIT_VIDEO_FONT`).

## On-screen text (`ocr`, optional)

A coding stream is about what is on the screen: file names, error messages, terminal output. This reads the screen
with Tesseract at each scene change and every 30 seconds, stores the text with its time, and lets you search it. It
needs the `tesseract` program, which is not in the default image (+65 MB, no model download). Build the image with it:

```bash
docker build --build-arg WITH_OCR=1 -t split-video:ocr .

docker run --rm -v "$PWD":/data split-video:ocr scenes stream.mp4 --root /data     # optional: also reads each scene change
docker run --rm -v "$PWD":/data split-video:ocr ocr stream.mp4 --root /data [--interval 30] [--max-frames 2000] [--force] [--json]
docker run --rm -v "$PWD":/data split-video:ocr ocr-search stream.mp4 "test_cut_burst" --root /data [--start 600 --end 900] [--limit 20] [--json]
docker run --rm -v "$PWD":/data split-video:ocr ocr-text stream.mp4 --root /data --start 600 --end 900
```

Frames come from the original file at 1280 px wide (the 640 px proxy cannot read code), are prepared with ffmpeg
(grayscale, inverted when the picture is dark, upscaled 2x) and read four at a time; a frame that looks like the last
one read is skipped. Run `scenes` first if you want a frame at every scene change, otherwise only the regular
frames are read (the output says so). Search is fuzzy and by word, because OCR is imperfect (`test_cut_burst`
also finds `test cut burst`, and a glued or slightly misread word still matches, with a lower score): you get the
time the text first appeared, when it was replaced, the best-matching line and the lines around it. Results are
stored next to the video (format in `src/split_video/ocr.py`) and go stale if the file changes. Over HTTP it is an
`ocr` job (`POST /api/v1/assets/{id}/ocr`, with progress by frame and cancel), read with `GET /api/v1/assets/{id}/ocr`
and `GET /api/v1/assets/{id}/ocr/search?q=` (paged); MCP has `ocr_asset`, `get_ocr` and `ocr_search`, and
`list_assets` shows `ocr`. Outside Docker: `sudo apt install tesseract-ocr tesseract-ocr-eng` (or `brew install
tesseract`). Without it the CLI exits with code 4, `POST .../ocr` answers 501 and the MCP tool returns a tool error,
all with the install hint. The choice of engine and the numbers behind it are in
[`docs/decisions/vision-and-ocr.md`](decisions/vision-and-ocr.md).

### Find a moment by what was said or shown (`search`, `visual-search`)

"The part where the build fails" is usually said in one place and shown in another. `search` looks in the transcript
and the on-screen text together and returns ranked moments: someone saying "the build failed" while the terminal
prints `FAILED` is one result with both as evidence, and ranks above either alone. It needs a transcript, an `ocr`
run, or both (with one it searches that one and says which was missing):

```bash
docker run --rm -v "$PWD":/data split-video:ocr search stream.mp4 "build failed" --root /data [--sources transcript,ocr] [--start 600 --end 900] [--limit 20] [--json]
docker run --rm -v "$PWD":/data split-video:ocr visual-search stream.mp4 "test_cut_burst" --root /data   # what the screen showed only
```

Scores are in 0..1 and comparable across sources (an exact phrase or frame is about 1, one word of a two-word query
about half), things within 15 seconds are merged, and matching is by word with no stemming (`fails` does not find
`failed`). The rules are in `src/split_video/search.py`; picture descriptions will plug into the same list of sources
when they exist. Over HTTP: `GET /api/v1/assets/{id}/search?q=&sources=` and `GET /api/v1/assets/{id}/search/visual?q=`
(both paged); MCP has `search_asset` and `visual_search`.

### What is happening at a time (`at`, `timeline-index`)

One call returns every stored signal around a timestamp: the audio class, loudness, silences, the words spoken, the
nearest scene cut and screen activity, and the on-screen text. It only reads what `analyze`, `propose`, `transcribe`,
`scenes` and `ocr` already stored (no decoding, no model); a signal that was never made is `null` and `missing` says
which command makes it.

```bash
docker run --rm -v "$PWD":/data split-video:ocr at stream.mp4 1:23:45 --root /data [--window 10] [--json]
docker run --rm -v "$PWD":/data split-video:ocr timeline-index stream.mp4 --root /data [--start 0 --end 3600 --step 60] [--json]
```

Over HTTP: `GET /api/v1/assets/{id}/at?time=&window=` and `GET /api/v1/assets/{id}/timeline?start=&end=&step=`; MCP has
`what_is_happening` and `timeline_index`. All three return the same JSON; its shape (version 1) is in
[docs/time-index.md](time-index.md).

## Everything about a recording in one call (`report`)

`report` returns duration, silence gaps, loudness, music / speech / applause regions, scene changes, screen activity, a
transcript summary and on-screen text for a whole recording as time-bucketed JSON that stays under 40 KB however long
the recording is (about 24 KB for 3 hours with every signal). It only reads what other commands stored; `--prepare`
first makes the cheap missing ones (loudness curve, silence scan, audio classes), never a transcript, scenes or text.

```bash
docker run --rm -v "$PWD":/data split-video:ocr report stream.mp4 --root /data --prepare          # a table
docker run --rm -v "$PWD":/data split-video:ocr report stream.mp4 --root /data --json [--bucket-seconds 60]
```

Over HTTP: `GET /api/v1/assets/{id}/report` and `POST /api/v1/assets/{id}/report/prepare` (a job); MCP has
`media_report`. All three return the same JSON, documented in [docs/media-report.md](media-report.md).

## Suggest highlights (`highlights`)

`highlights` is a deterministic first pass at "what should I cut?": it scores windows of a long recording from the
signals other commands stored (speech density, loudness, applause and music, scene changes, screen activity, on-screen
text, keywords, minus silence) and lists ranked, non-overlapping candidates with the reasons for each. Presets tune
the weights for a `gig`, a coding `stream` or a `lecture`; `--weight signal=value` overrides one. It only reads what was
stored (no decoding, no model) and proposes, it does not decide.

```bash
docker run --rm -v "$PWD":/data split-video:ocr highlights stream.mp4 --root /data --preset stream --count 10   # a table
docker run --rm -v "$PWD":/data split-video:ocr highlights gig.mp4 --root /data --preset gig --weight applause=3 --json
```

Over HTTP: `GET /api/v1/assets/{id}/highlights?preset=&count=&keywords=&weights=speech:2`; MCP has `suggest_highlights`. Reviewing the suggestions (accept, dismiss, adjust) and making them into clips on the timeline is
`highlight-reviews`, `review-highlight` and `make-short` (CLI), `get_highlight_reviews`, `review_highlight` and `make_short` (MCP) and
the `highlight-reviews` and `/project/highlights` routes: see [docs/highlights.md](highlights.md#review-and-make-a-short-spl-64).
All three return the same JSON; the method, the preset weight table and a worked example are in
[docs/highlights.md](highlights.md).
