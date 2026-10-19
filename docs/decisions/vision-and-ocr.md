# Decision: local vision-to-text and OCR models (SPL-96)

Status: accepted (2026-10-19). Card SPL-96, a spike: no feature code, no dependency added to `pyproject.toml`,
`uv.lock` or the `Dockerfile`. Feeds SPL-97 (frame descriptions), SPL-98 (on-screen text) and, through them,
SPL-99 and SPL-100.

## Recorded decision

| | Decision |
|---|---|
| **SPL-98, OCR** | **GO. Tesseract 5 (Apache-2.0)**, run as the `tesseract` binary on a frame preprocessed by ffmpeg (grayscale, invert if the picture is dark, 2x upscale). Fallback: **RapidOCR** (`rapidocr-onnxruntime`, Apache-2.0), which keeps identifiers and punctuation more exactly but costs 11x the RAM and about 200 MB more image. |
| **SPL-97, VLM** | **Conditional GO, opt-in, low expectations. Florence-2-base (MIT)** through the `florence-community/Florence-2-base` port (no `trust_remote_code`), `<CAPTION>` by default, `<MORE_DETAILED_CAPTION>` on request. Fallback: **no local VLM**: the agent reads contact sheets (SPL-46) and the OCR text. The local second choice, SmolVLM-256M (Apache-2.0), is faster but its captions were wrong or empty on every screen frame, so it is not recommended. |
| Optional extras | `ocr` (empty or `pillow`, plus the system `tesseract-ocr` package) and `describe` (`torch` CPU build + `transformers`). Both lazy, both outside the default `uv sync` and the default image. |
| Model location | OCR has no model download (the English traineddata ships with the package). Florence-2 is fetched on first use into `MediaStore.models_dir()` (`<workspace>/.split-video-models/`), as for transcription. |
| Resolution | **OCR needs frames from the original (1080p source, at least 1280 px wide).** The 640 px proxy reads slides and big type only, not code or terminals. |

Why only a conditional GO for the VLM: on a coding stream the screen frames are most of the footage, and no small
model described them usefully (the best said "a screenshot of a computer screen with a code on it"; the detailed
mode invents text and colours). On gig frames Florence-2 is good (it gets the guitar, the hat, the lights, even a
banner), but "a band performing on a stage" is also what 90 minutes of a gig look like, so the captions help triage
and search less than they promise. The torch dependency is heavy (about 1 GB installed). It is worth building only as
an opt-in extra. If that weight is not acceptable the honest alternative is no local VLM.

## Machine and honesty notes

16 cores, WSL2, 7.8 GB RAM of which 2-5 GB was free, **no GPU**, Python 3.12, `uv` throwaway venvs outside the repo.
Everything below is CPU, one model at a time, warm (model loaded, first frame not counted unless said). **No GPU
number was measured and none is quoted**: there is no GPU here, so "modest GPU" is untested. Expect a GPU to cut the
VLM times a lot, but that is a guess, not a result.

Timings come from a loaded WSL2 box. One SmolVLM run logged a 20,090 s time for one frame (a clock jump; the same
frame took 3.2-4.5 s on every other run), which is excluded. Treat times as good to about +-20%.

## Test set (12 frames)

Built by hand, deliberately small. The throwaway scripts (HTML page generator, Playwright screenshot, OCR and VLM
bench loops) were not committed, to keep this PR docs-only; the recipe below is enough to rebuild the set.

- **5 synthetic coding-stream screens, 1920x1080**, rendered from HTML with Playwright/Chromium using DejaVu Sans
  Mono, so the text is known exactly: `code_small` (editor, 14 px code, 48 real lines of
  `src/split_video/scenes.py`, file tree, tabs), `code_large_terminal` (22 px code, a terminal panel with a failing
  pytest), `terminal_fail` (18 px terminal: a failing `make test` with red error text, a `>` assertion line, a
  `where 3 = len([...])` line, a rustc-style error), `browser_docs` (a docs page, 24-26 px body, URL bar), and
  `slide_title` (white title text on a blue gradient). Ground truth is the exact text. They are cleaner than a real
  recording (no video compression, no webcam overlay, no anti-aliasing differences between machines), so real-world
  accuracy will be lower; the ranking should hold.
- **7 gig frames**, 16:9 crops at 1280x720 from Wikimedia Commons photos, all CC0 or public domain (a studio photo is
  not a stage recording: also cleaner than real footage): Bon Jovi live in Bucharest 2011 (CC0), Guitarist on stage
  with blue and red lights (CC0, Unsplash), Matthew Kane and the band live on stage (CC0), Man-person-people-crowd-
  concert-audience-41796 (CC0), Toby Keith on stage (public domain, US DoD), USAFE Jazz Band Chernivtsi (public domain,
  US Air Force), Vidpovid to be band live at Sophia Square Kyiv 2015 (CC0). No images are committed.
- The pipeline runs on the **640 px proxy**, so every frame was also resized to 640 px wide (Lanczos; the real proxy
  uses ffmpeg's scaler, close enough) and the models were run on those. OCR was run at 1920, 1280 and 640.

Quality scores are mine, from looking at the images: 0-2 for the gist (is it a band / a crowd / a code editor) and
0-2 for notable details (guitar colour, hat, the text on the page).

## Captioning models

All via `transformers` 5.20.1 + CPU `torch` 2.14.1, fp32, 8 threads (4 threads: +10%; do not use all 16, see
transcription). SmolVLM with image splitting off (`do_image_splitting=False`, one 512 px view, 84 input tokens).
Prompt for SmolVLM: "Describe this image in one or two sentences." Greedy decoding; Florence uses 3 beams.

| Model | Licence (model card) | Params | On disk | Warm load | Peak RAM | s / frame | New tokens | Gist (of 24) | Details (of 24) |
|---|---|---|---|---|---|---|---|---|---|
| SmolVLM-256M-Instruct | apache-2.0 | 256 M | 494 MB | 2.3 s | 1.9 GB | **1.45** | 17 | 13 | 4 |
| SmolVLM-500M-Instruct | apache-2.0 | 507 M | 973 MB | (first run 55 s with download) | **3.2 GB** | 2.4 | 23 | 18 | 7 |
| Florence-2-base `<CAPTION>` | mit | 232 M | 447 MB | 2.7 s | 1.8 GB | **2.47** | 14 | 21 | 9 |
| Florence-2-base `<MORE_DETAILED_CAPTION>` (max 120 tokens) | mit | 232 M | 447 MB | 2.7 s | 1.8 GB | 4.9 | 101 | **23** | **17** |

Notes:

- SmolVLM with its default image splitting (a 640 px frame becomes several 512 px tiles, 879 input tokens) was
  **14-15 s/frame** with the same captions quality, so a SmolVLM integration must switch splitting off.
- The 500M model's peak is 3.2 GB: too close to the 2-3 GB that is often free here. It also fell into a repetition
  loop on `gig_kane` (60 tokens of "There is a man standing and ..."). The 256M model and Florence peak at under 2 GB.
- Florence `<MORE_DETAILED_CAPTION>` with the default 60 tokens hit the limit on all 12 frames, mid-sentence on most (3.7 s/frame);
  120 tokens finishes most of them.
- Moondream2 (Apache-2.0, 1.9 B params, 3.9 GB of weights) was **not run**: it does not fit in the free RAM here. The
  SmolVLM-500M run (3.2 GB peak at half the parameters) shows why.
- Weights are bf16/fp16 files that torch upcasts to fp32 on load (hence 1.8 GB RAM for a 0.5 GB file). Loading in
  bf16 would roughly halve RAM; not measured (CPU bf16 matmul is often slower).

Per frame, `gist/detail` (0-2 each):

| Frame | SmolVLM-256M | SmolVLM-500M | Florence `<CAPTION>` | Florence detailed |
|---|---|---|---|---|
| browser_docs | 1/0 | 0/0 | 2/2 | 2/2 |
| code_large_terminal | 0/0 | 2/1 | 2/0 | 2/1 |
| code_small | 0/0 | 0/0 | 1/0 | 2/0 |
| terminal_fail | 1/0 | 0/0 | 1/0 | 1/0 |
| slide_title | 0/0 | 2/0 | 2/1 | 2/2 |
| gig_bonjovi | 2/1 | 2/1 | 2/0 | 2/2 |
| gig_crowd | 1/0 | 2/1 | 2/2 | 2/2 |
| gig_guitarist | 2/1 | 2/0 | 2/2 | 2/2 |
| gig_jazz | 1/1 | 2/1 | 2/0 | 2/1 |
| gig_kane | 2/0 | 2/1 | 2/0 | 2/2 |
| gig_keith | 2/1 | 2/1 | 2/2 | 2/2 |
| gig_kyiv | 1/0 | 2/1 | 1/0 | 2/1 |

On the 5 screens SmolVLM-256M scores 2/10 gist (it calls a code editor "a table" and a title slide "a person holding
a book"); on the 7 gig frames it scores 11/14. Florence is the only model that gets the screens right at all, and
only at the level of "a screenshot of a code editor".

Sample captions (frames 640 px wide):

- `gig_guitarist`. SmolVLM-256M: "In this image we can see a man playing guitar. In the background there are
  lights." Florence `<CAPTION>`: "A man playing a red guitar on a stage." Florence detailed: "The image shows a young
  man playing an electric guitar on a stage. He is wearing a black button-down shirt and has a beard. The stage is lit
  with blue and red lights, creating a dramatic atmosphere. ..."
- `gig_kane`. SmolVLM-500M: "In this image we can see a group of people playing musical instruments. There is a man
  standing and holding a guitar ... There is a man standing and playing a guitar. There is a man" (loop).
  Florence detailed: "The image shows a band performing on a stage ... The lead singer is wearing a black cowboy hat
  and ... playing a white electric guitar ... a large banner that reads 'Matthew Kann'." (it read the banner, and
  called a bass a guitar).
- `terminal_fail`. SmolVLM-256M: "In this image I can see the text in the middle." SmolVLM-500M: "In this image we can
  see a person standing on the ground." Florence `<CAPTION>`: "a screenshot of a computer screen with a black
  background". Florence detailed: "a screenshot of a code written in a programming language. The code is written in red
  text on a black background ... a header that reads 'Cancel' ..." (the colours are right, the header is invented).
- `slide_title`. Florence `<CAPTION>`: "building a video editor for agents" (it read the title). Florence detailed
  quotes all three lines correctly. SmolVLM-256M: "a person holding a book".
- A longer SmolVLM-256M prompt ("describe in detail, mention visible text and the application") made it worse: 80
  tokens of invented content ("a web page titled Class-based programming in JavaScript", "a banner that reads LAS
  ANGELES") at 3.2-4.5 s/frame.

Florence-2's `<OCR>` task, tried as a free OCR: fine on the slide (2.6 s, exact) and mostly right on the docs page
(6.1 s at 1080p), but **garbage on code and terminals** (`"def_init_self_threshold_float_min gap:..."`, no
whitespace or line breaks) at 8-10 s/frame, because it always rescales to 768x768. Not an OCR engine for this
purpose.

### Supply chain

- `microsoft/Florence-2-base` is the original repo (MIT); its model card has it load custom modeling code with
  `trust_remote_code=True` (not tried here). `florence-community/Florence-2-base` (MIT, 463 MB `model.safetensors`) is the
  in-library port: it loaded with `Florence2ForConditionalGeneration` and no remote code, which is what SPL-97 must
  use and what the test checked. Do not enable `trust_remote_code`. Pin the model revision (commit hash) in SPL-97,
  and load `safetensors` only.
- SmolVLM and Florence weights are `safetensors`; nothing was unpickled.

## OCR engines

Same frames. Scored against the exact source text: **line similarity** = mean over ground-truth lines of the best
difflib ratio against any OCR line (line-number gutters stripped), **exact lines** = share of ground-truth lines
recovered character for character, **token recall** = share of ground-truth whitespace tokens found. Averages over
the 3 code/terminal frames and the 2 docs/slide frames. Time is one frame, warm, including the preprocessing for
Tesseract (Pillow here).

| Engine | Input width | Code+terminal: line sim / exact / token recall | Docs+slide: line sim / token recall | s / frame (code) | Peak RAM |
|---|---|---|---|---|---|
| Tesseract 4.1.1, raw frame | 1920 | 0.73 / 0.24 / 0.54 | 0.99 / 1.00 | 1.2 | 65 MB |
| Tesseract 4.1.1, raw frame | 1280 | 0.48 / 0.08 / 0.23 | 0.99 / 0.99 | 0.8 | 65 MB |
| Tesseract 4.1.1, raw frame | 640 | 0.14 / 0.00 / 0.01 | 0.69 / 0.48 | 0.2 | 65 MB |
| **Tesseract, preprocessed** | 1920 | 0.90 / 0.36 / 0.83 | 0.99 / 1.00 | 1.4 | 70 MB |
| **Tesseract, preprocessed** | 1280 | 0.90 / 0.40 / 0.85 | 0.99 / 1.00 | 1.1 | 70 MB |
| **Tesseract, preprocessed** | 640 (3x up) | 0.80 / 0.11 / 0.57 | 0.99 / 0.99 | 1.1 | 70 MB |
| RapidOCR 1.4.4 (PP-OCRv4 ONNX) | 1920 | 0.90 / **0.56** / 0.79 | 0.99 / 0.99 | 2.7 | 780 MB |
| RapidOCR | 1280 | 0.88 / 0.33 / 0.58 | 0.92 / 0.63 | 2.0 | 780 MB |
| RapidOCR | 640 | 0.57 / 0.00 / 0.49 | 0.92 / 0.62 | 1.9 | 780 MB |
| Florence-2 `<OCR>` | 1920 | unusable on code (see above) | slide exact, docs page mostly right | 8-10 | 1.8 GB |

"Preprocessed" = grayscale, inverted when the picture is dark (mean luma under 128; Tesseract expects dark text on a
light page and reads light-on-dark code very badly raw), then upscaled 2x (3x from the proxy), `--psm 6`. Page
segmentation modes 3 and 6 were within noise of each other. EasyOCR was **not run**: it needs torch (the 740 MB
install above), so it is strictly heavier than RapidOCR for no accuracy edge that would change the choice; this is a
skip on weight, not a measurement.

Verified in Docker: the identical pipeline with no Python at all (ffmpeg `format=gray,negate,scale=iw*2:ih*2:flags=lanczos`
into `tesseract <png> <out> --psm 6`) in `python:3.12-slim` with Debian's **Tesseract 5.5.0** gave the same
quality (1920: 0.90 / 0.35 / 0.83; 1280: 0.90 / 0.42 / 0.85; 640: 0.80 / 0.10 / 0.54; docs 0.99 / 1.00 at all three),
at 0.5-2.2 s per frame including the process start. So the chosen path needs no Python OCR package, only the binary.

Sample, `terminal_fail` at 1920, the error lines (ground truth first):

```
E       AssertionError: assert 3 == 2
FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest - AssertionError
Tesseract: FAILED tests/unit/test_scenes.py::test_cut_burst_ keeps strongest - AssertionError
RapidOCR : FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest - AssertionError
```

Both read the failing test's name and the assertion. Tesseract splits an underscore run ("burst_ keeps") and adds a
stray quote before `'score'`; RapidOCR read `0.31` as `0.3l` (a lowercase L) and `Makefile:12` as `Makefile:l2`.
At 640 px both fail: Tesseract returns "Asserticofrror" and "Makefite:32; test} Error 3", RapidOCR drops half the
lines.

### What resolution OCR needs

Code and terminals need the **original**. In this test the code was 14, 18 and 22 px high at 1080p (line pitch 21-33
px); at the 640 px proxy that is 5-7 px, below what either engine reads (token recall 0.54 at best, exact lines about
10%). Slides and big type work at the proxy (the docs page, with 26 px body text, had token recall 0.99 with preprocessed
Tesseract; a title slide always). **1280 px wide from the original is as good as 1920 for preprocessed Tesseract**
(0.90 vs 0.90 line similarity; both are 2x upscaled to 2560/3840 before OCR), and is cheaper to decode and OCR, so
`extract_frame(source="original", width=1280)` is the right request. RapidOCR is the opposite: it was worse at 1280
than at 1920 (its detector resizes by side length), so use it at full size only. A 1080p source is assumed; a stream
recorded at 720p with small fonts will read worse, and a 4K one better.

Cost of getting those frames: an original 1080p frame at an arbitrary time takes 0.2-0.4 s with `-ss` before `-i`
(h264, one GOP of 250 frames, synthetic content). For 280 frames that is about 1.5 minutes on top of the OCR.

## Throughput, 90-minute recording

Assumption: one frame every 30 s (180) plus about 100 scene-change frames, 280 in all, every frame treated as a
worst case (all go through the model). Times are measured per-frame means multiplied by 280; sequential, one model at a
time, nothing else running. RAM is the measured peak of one process.

| Pass | Per frame | 280 frames | Peak RAM |
|---|---|---|---|
| Extract frames from the original (1280 px) | 0.3 s | 1.4 min | ffmpeg only |
| OCR, preprocessed Tesseract on a code/terminal frame | 1.1-1.4 s | 5-7 min | 70 MB |
| OCR, RapidOCR (fallback) at 1920 | 2.7 s | 12.6 min | 780 MB |
| Florence-2-base `<CAPTION>` | 2.5 s | 11.5 min | 1.8 GB |
| Florence-2-base `<MORE_DETAILED_CAPTION>` (120 tokens) | 4.9 s | 22.8 min | 1.8 GB |
| SmolVLM-256M (not recommended) | 1.45 s | 6.8 min | 1.9 GB |

Chosen stack, both passes: about **1.4 + 6 + 11.5 = about 19 minutes for a 90-minute stream** (0.21x real time),
peak **1.8 GB** (the VLM; the OCR pass is 70 MB and the two do not run at once). Tesseract is one process per frame and
single-threaded, so running 4 in parallel should bring the OCR pass to about 1.5-2 minutes; that is an estimate, not
measured. A gig has no screen text, so the OCR pass is skipped or cheap there, and a coding stream's screen frames
need no caption beyond "screen" (OCR already says what is on them), so real totals are lower than this worst case.

## Install weight

Measured with `uv pip install` into empty venvs (Python 3.12, x86-64 Linux); wheel sizes from PyPI/the PyTorch CPU index.

| Stack | Wheels downloaded | `site-packages` | Notes |
|---|---|---|---|
| Tesseract path | `pytesseract` 0.0 MB (optional) | about 0 | needs the **system** package. `tesseract-ocr` + English data in `python:3.12-slim` (Debian 13): **+65 MB** (measured by `du` before/after; ffmpeg and fonts already installed) |
| RapidOCR path (+ Pillow, numpy) | rapidocr-onnxruntime 14.9 (models inside), opencv-python 73.8, onnxruntime 23.8, shapely 2.3, pyclipper 1.0 | 363 MB total venv | onnxruntime is also in the `transcribe` extra, so shared there. In `python:3.12-slim` `import cv2` **fails** (`libxcb.so.1`), and `libgl1 libglib2.0-0` added **+211 MB** of apt packages, so a Docker image needs either those or a swap to `opencv-python-headless` (not tried) |
| VLM path (torch 2.14.1+cpu, transformers 5.20.1) | torch 196 MB, transformers 12.8, sympy 6.3, tokenizers 3.4, Pillow 7.1 | **996 MB** (torch alone 739 MB) | numpy is already a dependency. Weights extra: Florence-2-base 447 MB, SmolVLM-256M 494 MB |

Compare transcription: its extra is +360 MB. `describe` would be about **+1 GB installed** and `ocr` with Tesseract
**+65 MB** of apt, which is why the two are separate extras and neither goes in the default image.

A torch-free route for SPL-97 exists on paper and was **not measured**: `onnx-community/Florence-2-base` publishes ONNX
files (int8 decoder 98 MB, MIT) and onnxruntime is already in the `transcribe` extra. It needs a hand-written generation loop
and the image/text preprocessing without `transformers`. Treat it as a follow-up if the torch weight is rejected, not
as part of SPL-97.

## Docker image impact (estimates, not built, since no `Dockerfile` change is allowed in this card)

Default image 781 MB. Proposed opt-in build args, in the style of `WITH_TRANSCRIPTION`:

- `WITH_OCR=1`: `apt-get install --no-install-recommends tesseract-ocr tesseract-ocr-eng`: **+65 MB** (measured), nothing
  to download at runtime. About 850 MB.
- `WITH_DESCRIBE=1`: `uv sync --extra describe` (torch CPU): about **+1 GB** (the venv measure above; the real image
  delta not built), and optionally bake Florence-2-base (+450 MB) into `/opt/split-video-models` via
  `SPLIT_VIDEO_MODEL_SEED`, as for Whisper. Image about 1.8 GB, 2.3 GB with the model.

On first use otherwise: Florence-2-base downloads 463 MB into `<workspace>/.split-video-models/` with `HF_HOME` pointed
there and `HF_HUB_DISABLE_XET=1`, exactly as `transcribe.py` does (reuse its `_import_...`/seed lookup pattern).
Present means offline. Tesseract needs nothing downloaded.

## Implications for the follow-up cards

- **SPL-98 (on-screen text)**: a job over frames chosen by scene changes plus a regular interval. Extract each frame
  with `source="original"`, `width=1280` (not the proxy), preprocess in ffmpeg (grayscale, `negate` when the mean luma
  of the frame is below 128, `scale=iw*2:ih*2:flags=lanczos`), run `tesseract <png> stdout --psm 6`. Store text per
  frame in a sidecar through the MediaStore, keyed by asset id and frame time, so a changed file invalidates it. Because
  recall is imperfect (token recall 0.83, exact lines about 40%), search must be fuzzy: lower-case, split on
  non-alphanumerics (Tesseract breaks `a_b` into `a b`), and match tokens, not whole lines. Detect "no `tesseract`
  binary" and exit with an install hint (CLI code 4, MCP error, HTTP 501), as transcription does. Skip frames that did
  not change since the last OCRed one (scene scores already exist). Optional: RapidOCR behind a setting if exact
  identifier matches matter more than weight.
- **SPL-97 (frame descriptions)**: gate on the user having installed `describe`. Use Florence-2-base,
  `<CAPTION>` by default (2.5 s/frame), `<MORE_DETAILED_CAPTION>` as an option with `max_new_tokens` of at least 120.
  Feed it the **640 px proxy frames** (it rescales to 768x768 anyway, so the original adds nothing and the proxy
  is cheaper). Pass `num_beams=3`, `max_new_tokens`, and set torch threads to 8. Mark a caption as "screen" when a frame
  is mostly text and let SPL-98 describe it, instead of trusting "a screenshot of a code editor". Do not index the
  detailed caption's colours and text as fact on screen frames: it invents text. Do not use SmolVLM with image splitting on (14 s).
- **SPL-99 / SPL-100 (search, highlights over the visual signals)**: treat OCR text as the strong signal for coding
  streams (file names, test names, error lines) and captions as the weak one for gigs (they separate "crowd",
  "close-up of the singer", "wide stage shot", "band", not songs or moments). Do not rank highlights by caption
  content alone. Both must tolerate an asset with neither `ocr` nor `describe` installed, in which case the agent works
  from contact sheets and transcripts.
- **All**: never load both models in one process (VLM 1.8 GB plus the app); run them as separate jobs, cancellable
  between frames. Tests need the engine behind a seam (fake OCR and fake captioner) like `ModelLike` in `transcribe.py`;
  opt-in real-model tests behind an env var, like `SPLIT_VIDEO_TEST_DOWNLOAD`.

## What was not done

No GPU run (none here). Moondream2 and EasyOCR not run (weight). The ONNX/torch-free VLM path not built. Real
recorded streams and gig video not tested, only rendered screens and still photos; compression, a webcam overlay and
motion blur will lower every number. The VLM sample is 12 frames scored by one person. 640 px proxy frames were
resized with Lanczos, not the pipeline's ffmpeg scaler. Tesseract 4.1.1 on the host and 5.5.0 in Docker agreed on the
numbers measured, but only 5 screens were scored.
