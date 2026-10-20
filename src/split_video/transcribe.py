"""Local speech-to-text with word timestamps (faster-whisper), stored with the asset.

Highlights and captions both start from what was said, so every asset can have one transcript: the
words, each with the second it starts and ends. The model runs on this machine (CPU, int8); nothing is
uploaded. The decision record (why faster-whisper, why `base.en`, measured numbers) is
`docs/decisions/transcription.md`.

Optional dependency
-------------------
`faster-whisper` is the `transcribe` extra (`uv sync --extra transcribe`, `pip install
'split-video[transcribe]'`, or the Docker image built with `--build-arg WITH_TRANSCRIPTION=1`). It is
imported only when a transcription runs, so everything else works without it. When it is missing every
surface says so with the install hint in `UNAVAILABLE`: the CLI exits with code 4, the MCP tool raises a
tool error, and `POST /api/v1/assets/{id}/transcribe` answers 501 with that text as its `detail`.

The model
---------
`base.en` by default (English, 139 MB); `--model` / `model` takes any faster-whisper model name, e.g.
`small.en`, or the multilingual `base` / `small` (then the language is detected, or given with
`language`). The model is downloaded on first use into `MediaStore.models_dir()` (locally
`<workspace root>/.split-video-models/`, so it survives a `--rm`'d container like the media does) and
never into `~/.cache`: before faster-whisper is imported, `HF_HOME` is pointed there and the Hugging Face
xet downloader (which keeps its own cache under the home directory) is switched off. A model that is
already there is loaded with no network access at all. A Docker image built with `--build-arg
TRANSCRIPTION_MODEL=base.en` carries a copy in `$SPLIT_VIDEO_MODEL_SEED`, which is read when the workspace has none.

The transcript
--------------
Stored through `MediaStore.save_cache(<asset's media id>, "transcript", ...)`, so it is keyed by the
original file's size and mtime and goes stale when the file changes. `load` returns this JSON-safe
document (the shape other cards read; `VERSION` is bumped if it changes):

    {"version": 1,
     "asset": "a_0123456789abcdef",
     "model": "base.en",
     "language": "en",               # detected, or what was asked for
     "language_probability": 1.0,
     "duration": 86.345,             # seconds of audio that were transcribed
     "vad": true,
     "segments": [
        {"start": 0.45, "end": 5.52, "text": "Mister Quilter is ...",
         "words": [{"start": 0.45, "end": 0.78, "word": "Mister", "probability": 0.93}, ...]}]}

All times are seconds from the start of the asset's audio, rounded to milliseconds. `word` has no
surrounding space (punctuation stays attached: "gospel."). `text` is the words joined by single spaces.
Words are in order and never overlap by more than rounding; `end >= start`. Silence is not stored: a
recording with no speech has `segments: []`. A small second entry, "transcript_info", holds
`{version, asset, model, language, duration, segments, words}` (counts), so listings can say whether an
asset has a transcript without reading the whole document. `words_between` and `text_between` cut a
transcript to a time range (what highlight and caption code does with it); `transcript_search.py` reads it in
pages and searches it (what agents do, without loading hours of text).

Long recordings
---------------
The audio is read with `ffmpeg.iter_pcm_chunks` (16 kHz mono, a few seconds at a time), never decoded
whole. It is collected into windows of about `WINDOW_SECONDS` (5 min: 10 MB of PCM, and about 700 MB peak for the
whole process, where 10-minute windows measured 1.1 GB) which are given to the model one after another. A window ends at the quietest 0.1 s found in its last `SEARCH_SECONDS`
(30 s), and the next one starts exactly there: windows do not overlap, so no word is transcribed twice,
and a cut in silence rarely lands inside a word. A window's times are offset by its start sample, so the
stitched transcript is in absolute time. Voice-activity filtering (the Silero model that ships inside
faster-whisper, so it needs no download) skips the silence and music between speech instead of letting
the model hallucinate text there, and `condition_on_previous_text` is off so one bad passage cannot make
the model repeat itself for the rest of the file. Progress is seconds of audio done.

Cancelling
----------
The model call is not interruptible: `WhisperModel.transcribe` runs the voice-activity filter before it returns and
then does its work lazily, one 30 s chunk per segment it yields, and one chunk takes seconds on a CPU. So with a cancel
token the model (and loading it) runs on a helper thread and the job's own thread waits for its segments with a short
poll (`CANCEL_POLL_SECONDS`): a cancel is seen within a fraction of a second, whatever the model is in the middle of,
the job becomes `cancelled`, and the decode is terminated. The helper thread then stops after the chunk it is on (at most a few
seconds of CPU, nothing is stored from it).
"""

from __future__ import annotations

import os
import queue
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol

import numpy as np

from split_video import assets
from split_video.ffmpeg import iter_pcm_chunks
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore

VERSION = 1
CACHE_NAME = "transcript"
INFO_NAME = "transcript_info"

DEFAULT_MODEL = "base.en"
SAMPLE_RATE = 16000
WINDOW_SECONDS = 300.0
SEARCH_SECONDS = 30.0
FRAME_SECONDS = 0.1  # the resolution of the search for a quiet place to cut
MIN_WINDOW_SECONDS = 0.2  # a shorter tail is not worth a model call
CANCEL_POLL_SECONDS = 0.1  # how often a job waiting for the model looks at its cancel token

# Read-only folder of models baked into a Docker image (`--build-arg TRANSCRIPTION_MODEL=base.en`), tried after the
# workspace's own and before downloading.
SEED_ENV = "SPLIT_VIDEO_MODEL_SEED"

UNAVAILABLE = (
    "Transcription needs the optional faster-whisper package, which is not installed. Install it with "
    "`uv sync --extra transcribe` (a source checkout) or `pip install 'split-video[transcribe]'`, or use "
    "the Docker image built with `--build-arg WITH_TRANSCRIPTION=1` (see the README)."
)


class TranscriptionUnavailableError(RuntimeError):
    """faster-whisper is not installed (or cannot be imported). The message is the install hint."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(UNAVAILABLE + (f" ({detail})" if detail else ""))


class TranscriptionError(RuntimeError):
    """The transcription can't be done: no audio, or the model can't be loaded."""


class SegmentLike(Protocol):
    start: float
    end: float
    text: str
    words: Any


class ModelLike(Protocol):
    """The part of `faster_whisper.WhisperModel` used here; tests pass a fake with the same call."""

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Iterable[Any], Any]: ...


ModelFactory = Callable[[str, str], ModelLike]  # (model name, models directory) -> model
ProgressFn = Callable[[float, float], None]  # (seconds of audio done, seconds in all)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(asset_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(asset_id, threading.Lock())


# --- the optional dependency and the model ---------------------------------------------------


def _import_faster_whisper(models_dir: str | None = None) -> Any:
    """Import faster-whisper, after pointing the Hugging Face downloader at `models_dir` (only effective if
    huggingface_hub is not imported yet, which is why `load_model` also passes `download_root`)."""
    if models_dir is not None:
        os.environ.setdefault("HF_HOME", models_dir)
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # its cache would go under ~/.cache
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    try:
        import faster_whisper
    except ImportError as exc:
        raise TranscriptionUnavailableError(f"{type(exc).__name__}: {exc}") from None
    return faster_whisper


def require_available() -> None:
    """Raise `TranscriptionUnavailableError` (with the install hint) unless faster-whisper can be imported."""
    _import_faster_whisper()


def available() -> bool:
    try:
        require_available()
    except TranscriptionUnavailableError:
        return False
    return True


def load_model(name: str, models_dir: str, *, cpu_threads: int = 0) -> ModelLike:
    """The faster-whisper model `name` (CPU, int8) from `models_dir`, downloading it on first use. A model that is
    already there is opened with no network access; only a missing one is fetched."""
    module = _import_faster_whisper(models_dir)
    kwargs: dict[str, Any] = {
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": cpu_threads,
    }
    seed = os.environ.get(SEED_ENV)
    # A model already in the workspace, then one baked into the image, never touch the network; only then download.
    for root, local_only in [(models_dir, True), *([(seed, True)] if seed else []), (models_dir, False)]:
        try:
            return module.WhisperModel(name, local_files_only=local_only, **{**kwargs, "download_root": root})
        except Exception as exc:  # noqa: BLE001 - not downloaded yet, or the download failed
            if local_only:
                continue
            raise TranscriptionError(
                f"could not load the transcription model {name!r}: {exc}. The first use downloads the model "
                f"(about 140 MB for base.en) into the workspace, so it needs internet access once; model names "
                f"are faster-whisper's (tiny.en, base.en, small.en, base, small, ...)."
            ) from None
    raise AssertionError("unreachable")  # pragma: no cover


# --- windows of audio ------------------------------------------------------------------------


def quiet_cut(data: np.ndarray, search_samples: int, frame_samples: int) -> int:
    """The index in `data` to end a window at: the middle of the quietest `frame_samples` frame in its last
    `search_samples` samples (the latest of equally quiet ones, so a window stays long)."""
    n = len(data)
    first = max(0, n - search_samples)
    frames = (n - first) // frame_samples
    if frames < 2:
        return n
    block = data[first : first + frames * frame_samples].astype(np.float32).reshape(frames, frame_samples)
    energy = np.einsum("ij,ij->i", block, block)
    best = int(len(energy) - 1 - np.argmin(energy[::-1]))
    return first + best * frame_samples + frame_samples // 2


def windows(
    chunks: Iterable[np.ndarray],
    *,
    window_seconds: float = WINDOW_SECONDS,
    search_seconds: float = SEARCH_SECONDS,
    sample_rate: int = SAMPLE_RATE,
) -> Iterator[tuple[int, np.ndarray]]:
    """Group PCM chunks into `(first sample, int16 samples)` windows of about `window_seconds` that end in quiet
    places and do not overlap (see the module docstring). Memory is one window plus one chunk."""
    window = max(1, int(window_seconds * sample_rate))
    search = int(search_seconds * sample_rate)
    frame = max(1, int(FRAME_SECONDS * sample_rate))
    pending: list[np.ndarray] = []
    held = 0
    start = 0
    for chunk in chunks:
        pending.append(chunk)
        held += len(chunk)
        if held < window:
            continue
        data = np.concatenate(pending)
        cut = quiet_cut(data, search, frame)
        yield start, data[:cut]
        start += cut
        pending = [data[cut:]]
        held = len(pending[0])
    if held:
        yield start, np.concatenate(pending)


# --- running the model -----------------------------------------------------------------------


def _round(value: float) -> float:
    return round(float(value), 3)


def _segment_dict(segment: Any, offset: float) -> dict[str, Any] | None:
    """One model segment as stored, shifted by `offset` seconds; None when it holds no words."""
    words: list[dict[str, Any]] = []
    for w in segment.words or []:
        text = str(w.word).strip()
        if not text:
            continue
        start = _round(w.start + offset)
        words.append(
            {
                "start": start,
                "end": max(start, _round(w.end + offset)),
                "word": text,
                "probability": _round(getattr(w, "probability", 1.0)),
            }
        )
    if not words:
        return None
    return {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": " ".join(w["word"] for w in words),
        "words": words,
    }


def _interruptibly(fn: Callable[[], Any], cancel: CancelToken | None) -> Any:
    """`fn()`, which cannot be interrupted (loading the model: an import and a few seconds of reading), run on a helper
    thread when there is a `cancel` token so that a cancel is seen within `CANCEL_POLL_SECONDS`."""
    if cancel is None:
        return fn()
    done: queue.Queue[tuple[bool, Any]] = queue.Queue()

    def call() -> None:
        try:
            done.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - handed to the job's thread
            done.put((False, exc))

    threading.Thread(target=call, name="transcribe-load", daemon=True).start()
    while True:
        cancel.raise_if_cancelled()
        try:
            ok, value = done.get(timeout=CANCEL_POLL_SECONDS)
        except queue.Empty:
            continue
        if ok:
            return value
        raise value


def _model_events(
    model: ModelLike, audio: np.ndarray, kwargs: dict[str, Any], cancel: CancelToken | None
) -> Iterator[tuple[str, Any]]:
    """`("info", info)` and then `("segment", segment)` for each segment of `model.transcribe(audio, **kwargs)`.
    Without a `cancel` token it is the plain call. With one the model runs on a helper thread (see the module
    docstring) and `JobCancelledError` is raised within `CANCEL_POLL_SECONDS` of the cancel; the helper stops after
    the chunk it is on."""
    if cancel is None:
        iterator, info = model.transcribe(audio, **kwargs)
        yield "info", info
        for segment in iterator:
            yield "segment", segment
        return
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop = threading.Event()

    def produce() -> None:
        try:
            iterator, info = model.transcribe(audio, **kwargs)
            events.put(("info", info))
            for segment in iterator:
                if stop.is_set():
                    return
                events.put(("segment", segment))
            events.put(("end", None))
        except BaseException as exc:  # noqa: BLE001 - handed to the job's thread
            events.put(("error", exc))

    threading.Thread(target=produce, name="transcribe-model", daemon=True).start()
    try:
        while True:
            cancel.raise_if_cancelled()
            try:
                kind, value = events.get(timeout=CANCEL_POLL_SECONDS)
            except queue.Empty:
                continue
            if kind == "end":
                return
            if kind == "error":
                raise value
            yield kind, value
    finally:
        stop.set()


def run(
    pcm_chunks: Iterable[np.ndarray],
    model: ModelLike,
    *,
    language: str | None = None,
    total_seconds: float = 0.0,
    window_seconds: float = WINDOW_SECONDS,
    search_seconds: float = SEARCH_SECONDS,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Transcribe PCM chunks (int16, 16 kHz mono) with `model`. Returns `{"language", "language_probability",
    "duration", "segments"}` (the transcript without its identity fields). `JobCancelledError` through `cancel`,
    checked while the model works (see the module docstring)."""
    segments: list[dict[str, Any]] = []
    samples = 0
    found: tuple[str, float] | None = None
    total = max(total_seconds, 1e-9)
    for start, data in windows(pcm_chunks, window_seconds=window_seconds, search_seconds=search_seconds):
        samples = start + len(data)
        if cancel is not None:
            cancel.raise_if_cancelled()
        if len(data) < MIN_WINDOW_SECONDS * SAMPLE_RATE:
            continue
        offset = start / SAMPLE_RATE
        audio = data.astype(np.float32) / 32768.0
        kwargs = {
            "language": language or (found[0] if found else None),
            "word_timestamps": True,
            "vad_filter": True,
            "condition_on_previous_text": False,
        }
        for kind, item in _model_events(model, audio, kwargs, cancel):
            if kind == "info":
                if found is None and getattr(item, "language", None):
                    found = (str(item.language), float(getattr(item, "language_probability", 1.0)))
                continue
            stored = _segment_dict(item, offset)
            if stored is not None:
                segments.append(stored)
            if on_progress is not None:
                on_progress(min(offset + float(item.end), total), total)
        if on_progress is not None:
            on_progress(min(samples / SAMPLE_RATE, total), total)
    code, probability = found if found else (language or "", 1.0)
    return {
        "language": language or code,
        "language_probability": _round(1.0 if language else probability),
        "duration": _round(samples / SAMPLE_RATE),
        "segments": segments,
    }


# --- storing and reading ----------------------------------------------------------------------


def _info_of(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "asset": document["asset"],
        "model": document["model"],
        "language": document["language"],
        "duration": document["duration"],
        "segments": len(document["segments"]),
        "words": sum(len(s["words"]) for s in document["segments"]),
    }


def _valid(payload: Any, asset: assets.Asset) -> bool:
    return isinstance(payload, dict) and payload.get("version") == VERSION and payload.get("asset") == asset.id


def load_stamped(media: MediaStore, asset_id: str) -> tuple[dict[str, Any], str] | None:
    """`(transcript, stamp)` for the asset's current transcript, or None. The stamp is random and new each time a
    transcript is stored (`save`), for readers that keep a parsed copy; it is not part of the returned document."""
    asset = assets.get_asset(media, asset_id)
    payload = media.load_cache(asset.path, CACHE_NAME)
    if not _valid(payload, asset):
        return None
    return {k: v for k, v in payload.items() if k != "stamp"}, str(payload.get("stamp", ""))


def load(media: MediaStore, asset_id: str) -> dict[str, Any] | None:
    """The asset's stored transcript (the document in the module docstring), or None if it has none or the file
    has changed since. `AssetNotFoundError` / `SourceNotFoundError` for a missing asset."""
    found = load_stamped(media, asset_id)
    return found[0] if found else None


def info(media: MediaStore, asset: assets.Asset) -> dict[str, Any] | None:
    """`{version, asset, model, language, duration, segments, words}` for a current transcript, else None. Cheap."""
    payload = media.load_cache(asset.path, INFO_NAME)
    if not _valid(payload, asset):
        return None
    return {k: v for k, v in payload.items() if k != "stamp"}


def current_stamp(media: MediaStore, asset: assets.Asset) -> str | None:
    """The stamp (see `load_stamped`) of the asset's current transcript, read from the small info entry; None
    if it has no current transcript (or one stored before stamps existed)."""
    payload = media.load_cache(asset.path, INFO_NAME)
    return (payload.get("stamp") or None) if _valid(payload, asset) else None


def has_transcript(media: MediaStore, asset: assets.Asset) -> bool:
    """What an asset summary says (`transcript`)."""
    return info(media, asset) is not None


def save(media: MediaStore, asset: assets.Asset, document: dict[str, Any]) -> dict[str, Any]:
    """Store `document` (the format in the module docstring) as the asset's transcript and return its info. Both
    stored entries get a fresh `stamp`, which readers that keep a parsed copy (`transcript_search`) compare."""
    stamp = uuid.uuid4().hex
    summary = _info_of(document)
    media.save_cache(asset.path, CACHE_NAME, {**document, "stamp": stamp})
    media.save_cache(asset.path, INFO_NAME, {**summary, "stamp": stamp})
    return summary


def words_between(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    """The words that overlap the span `[start, end)` seconds (a word cut by the span's edge is included), in
    order, as stored (`start`, `end`, `word`, `probability`)."""
    return [
        w
        for s in transcript["segments"]
        if s["end"] > start and s["start"] < end
        for w in s["words"]
        if w["end"] > start and w["start"] < end
    ]


def excerpt(transcript: dict[str, Any], start: float | None = None, end: float | None = None) -> dict[str, Any]:
    """The transcript cut to `[start, end)` (either may be None): the same document with only the segments that
    overlap, each holding only its overlapping words (so a cut segment's `start`, `end` and `text` follow its
    words). With neither bound, the transcript itself."""
    if start is None and end is None:
        return transcript
    lo = 0.0 if start is None else start
    hi = float("inf") if end is None else end
    segments = []
    for s in transcript["segments"]:
        words = words_between({"segments": [s]}, lo, hi)
        if words:
            segments.append(
                {
                    "start": words[0]["start"],
                    "end": words[-1]["end"],
                    "text": " ".join(w["word"] for w in words),
                    "words": words,
                }
            )
    return {**transcript, "segments": segments}


def text_between(transcript: dict[str, Any], start: float, end: float) -> str:
    """The words of `words_between`, joined by spaces."""
    return " ".join(w["word"] for w in words_between(transcript, start, end))


def transcribe(
    media: MediaStore,
    asset_id: str,
    *,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    force: bool = False,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
    model_factory: ModelFactory | None = None,
) -> dict[str, Any]:
    """Transcribe the asset's audio and store the transcript, unless a current one made with the same `model` (and
    `language`, if given) exists (`force` redoes it). Returns the transcript's info (see `info`) plus `made`,
    whether this call did the work.

    Raises `TranscriptionUnavailableError` without the optional dependency (checked before anything is read),
    `TranscriptionError` for an asset without audio or a model that can't be loaded, `ExtractError` if ffmpeg
    fails, `AssetNotFoundError` / `SourceNotFoundError` for a missing asset and `JobCancelledError` through
    `cancel`; a failed or cancelled run stores nothing and leaves any earlier transcript alone."""
    if model_factory is None:
        require_available()
    asset = assets.get_asset(media, asset_id)
    if not asset.has_audio:
        raise TranscriptionError(f"{asset.path} has no audio track to transcribe")
    with _lock_for(asset.id):  # a second job for the same asset waits, then finds the transcript current
        current = None if force else info(media, asset)
        if current is not None and current["model"] == model and language in (None, current["language"]):
            return {**current, "made": False}
        with media.models_dir() as models:
            factory = model_factory or load_model
            loaded = _interruptibly(lambda: factory(model, str(models)), cancel)
        if on_progress is not None:
            on_progress(0.0, asset.duration)
        with media.local_path(asset.path) as source:
            pcm = iter_pcm_chunks(source, SAMPLE_RATE, cancel=cancel)
            try:
                body = run(
                    pcm,
                    loaded,
                    language=language,
                    total_seconds=asset.duration,
                    window_seconds=WINDOW_SECONDS,  # read now, so a test can shrink them
                    search_seconds=SEARCH_SECONDS,
                    on_progress=on_progress,
                    cancel=cancel,
                )
            finally:
                pcm.close()
        document = {
            "version": VERSION,
            "asset": asset.id,
            "model": model,
            "language": body["language"],
            "language_probability": body["language_probability"],
            "duration": body["duration"],
            "vad": True,
            "segments": body["segments"],
        }
        return {**save(media, asset, document), "made": True}
