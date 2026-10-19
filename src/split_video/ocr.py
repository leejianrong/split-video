"""On-screen text (OCR) of a screen recording, per frame, stored with the asset and searchable by text.

A coding stream is about what is on the screen: file names, error messages, terminal output. The audio says none of
it. This module reads the screen at the moments that matter and keeps the text with a timestamp, so an agent can ask
"when did `test_cut_burst_keeps_strongest` fail?" without looking at hours of frames. Engine, resolution and
preprocessing come from `docs/decisions/vision-and-ocr.md` (SPL-96).

The engine
----------
Tesseract, the `tesseract` program on PATH (Debian: `tesseract-ocr tesseract-ocr-eng`; the Docker image has it with
`--build-arg WITH_OCR=1`). There is no Python OCR package and no model to download. Without the program every entry
point raises `OcrUnavailableError`, whose message is the install hint: the CLI exits with code 4, the MCP tool is a
tool error and `POST /assets/{id}/ocr` answers 501 before queueing anything. The engine sits behind the small
`OcrEngine` protocol (`TesseractEngine` is the real one, `default_engine()` makes it); tests pass a fake.

Which frames
------------
`schedule` picks them: the asset's scene changes (`scenes.load`, plus `SCENE_OFFSET` = 0.5 s so a transition has
settled) and one frame every `interval` seconds (default 30), merged so that no two are closer than `MIN_GAP` = 2 s
(a scene frame beats an interval frame that is in its way), and capped at `max_frames` (default 2000): past the cap
the interval frames are thinned evenly first, then the scene frames, and the result says so (`capped`, `note`). If the
asset has no scene document the schedule is the interval alone (`scenes_used` is false and `note` says to run
`scenes` first): analysing scenes is its own job and is not started behind your back. A document made without scenes
is redone when scenes appear.

Each frame is read from the ORIGINAL (`frames.extract_frame(source="original", width=1280)`): the 640 px proxy cannot
read code. ffmpeg then prepares it (`prepare_graph`: one gray plane, inverted when the picture's mean luma is under
128 because Tesseract wants dark text on a light page, 2x Lanczos upscale) and `tesseract <png> stdout --psm 6 -l eng`
reads it. The gray plane is not the luma but the brightest R/G/B channel (darkest for a light picture): the decision
note's plain grayscale loses coloured text on a mid-dark background (a red `FAILED` line on a grey terminal went
missing from 60 of 180 frames of a synthetic 3 hour stream; with the channel max it was read in all of them). All
temporary files live in a temp directory and go with it.

Skipping unchanged frames
-------------------------
Every frame also gets a small gray `signature` (384x216, area-averaged). A frame is `changed` unless fewer than
`CHANGE_PIXELS` = 12 of its signature pixels differ by 24 grey levels or more from the signature of the last frame
that WAS read (a changed digit or word counts, a single blinking cursor does not, and slow drift adds up against the
same reference). The bar is deliberately low: a needless read costs a second, a missed edit loses text. Unchanged frames are not
read: they are recorded with `changed: false` and `reused_from` (the time of the frame whose text is still on
screen). A changed frame has `until`: when the next changed frame took over, or the duration. Counts are in the
result (`ocr_frames`, `skipped_unchanged`).

Concurrency
-----------
Up to `WORKERS` = 4 frames are in flight at once (extracting, preparing or reading) on a thread pool, each Tesseract
with `OMP_THREAD_LIMIT=1`. Progress counts frames. A cancelled job stops between frames and terminates the running
ffmpeg and tesseract processes (`ffmpeg.run_cancellable`); nothing is stored.

The document
------------
Stored through `MediaStore.save_cache(<the asset's media id>, "ocr", ...)`, so it is keyed by the original's size and
mtime and is stale when the file changes (`VERSION` is bumped if the shape changes):

    {"version": 1, "asset": "a_...", "engine": "tesseract 4.1.1", "duration": 5400.0, "interval": 30.0,
     "max_frames": 2000, "scenes_used": true, "capped": false, "frames_before_cap": 212,
     "frames": [{"time": 12.0, "text": "def f():\\n    pass", "lines": ["def f():", "    pass"], "changed": true,
                 "source_scene": false, "until": 42.0},
                {"time": 42.0, "text": "", "lines": [], "changed": false, "source_scene": true,
                 "reused_from": 12.0}, ...]}

(`time` is the time of the frame actually decoded.) Text is normalised: control characters dropped, trailing
whitespace removed, blank lines dropped, at most `MAX_TEXT_CHARS` = 4000 characters per frame. A small second entry
"ocr_info" holds the counts (the dict `recognize` returns) so listings can say whether an asset has text without
reading it. `recognize` is idempotent: a current document made with the same `interval`, `max_frames` and scenes
availability is reused.

Reading and searching
---------------------
`load`, `info`, `has_ocr`, `excerpt` (a document cut to a span), `ocr_between(media, asset, start, end)` (the text
states on screen in a span: `{time, until, text, lines, source_scene}`, what a media report calls) and
`ocr_range` (the same, paged, with opaque cursors) read the stored text. `ocr_search` finds text, and is the function
a combined search (SPL-99) merges with `transcript_search.transcript_search`: its hits all carry
`source: "ocr"`, `time`, `score` (0..1) and `text`.

Tesseract's recall is imperfect (token recall about 0.83 on code, exact lines about 40 %) and it splits `a_b` into
`a b`, so matching is fuzzy and by token: the query and the text are lower-cased and split on anything that is not a
letter or digit; every query token must be found in the same frame (a frame is the unit: no window across frames).
A token matches a text token if equal (score 1.0), if the text token contains it (0.85, for tokens of 4+ letters:
OCR often glues words) or if it is close (`difflib` ratio >= 0.8, tokens of 5+ letters, score ratio * 0.8); two or
three adjacent text tokens run together (`07` `7` for `077`, `handl` `er` for `handler`) also count, at 0.9 of the
score. A hit's
score is 0.75 * the mean token score + 0.25 * adjacency (1.0 if the tokens follow each other in order on one line,
0.5 if they are on one line, else 0). Hits are ordered by score, then time; `text` is the line that matched best,
`context_before` / `context_after` are the lines around it, `matched_tokens` the text tokens that matched, `time`
the first sampled time the text was seen and `end` when it was replaced. A cursor is tied to the stored document and
the query; paging yields every hit exactly once.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Protocol

import numpy as np

from split_video import assets, frames, scenes
from split_video.ffmpeg import ExtractError, _require_binary, run_cancellable
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore

VERSION = 1
CACHE_NAME = "ocr"
INFO_NAME = "ocr_info"

DEFAULT_INTERVAL = 30.0
MIN_INTERVAL = 1.0
DEFAULT_MAX_FRAMES = 2000
MAX_FRAMES_LIMIT = 10_000
MIN_GAP = 2.0
SCENE_OFFSET = 0.5
FRAME_WIDTH = 1280
WORKERS = 4
MAX_TEXT_CHARS = 4000

SIGNATURE_WIDTH = 384
SIGNATURE_HEIGHT = 216
DARK_LUMA = 128
CHANGE_PIXEL_DIFF = 24
CHANGE_PIXELS = 12

DEFAULT_RANGE_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 20
MAX_LIMIT = 200
DEFAULT_CONTEXT = 1
MAX_CONTEXT = 10
MAX_QUERY_CHARS = 500
GLUE_FACTOR = 0.9  # a match through tokens run together scores this much of the plain match
MAX_GLUED = 32

AUDIO_ONLY = "audio-only asset"

UNAVAILABLE = (
    "On-screen text needs the Tesseract OCR program, which is not installed. Install it with "
    "`sudo apt install tesseract-ocr tesseract-ocr-eng` (Debian/Ubuntu) or `brew install tesseract`, or use "
    "the Docker image built with `--build-arg WITH_OCR=1` (see the README)."
)
NOT_FOUND_HINT = (
    "run `split-video ocr <target>` (CLI), the ocr_asset tool (MCP) or POST /api/v1/assets/{id}/ocr (HTTP) to make it"
)

ProgressFn = Callable[[int, int], None]  # (frames done, frames in all)


class OcrUnavailableError(RuntimeError):
    """The `tesseract` program (or its English data) is missing. The message is the install hint."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(UNAVAILABLE + (f" ({detail})" if detail else ""))


class OcrError(RuntimeError):
    """Reading a frame failed."""


class OcrNotFoundError(LookupError):
    """The asset has no current on-screen text (never made, or the file changed since)."""


class OcrArgumentError(ValueError):
    """A bad interval or frame cap."""


class OcrQueryError(OcrArgumentError):
    """A bad range, limit, cursor or query."""


# --- the engine ---------------------------------------------------------------------------------


class OcrEngine(Protocol):
    """What the pass needs from an OCR engine; tests pass a fake with the same two calls."""

    name: str  # recorded in the document, e.g. "tesseract 4.1.1"

    def signature(self, image: bytes, cancel: CancelToken | None) -> bytes:
        """`SIGNATURE_WIDTH * SIGNATURE_HEIGHT` gray bytes (area-averaged) of the encoded image."""
        ...

    def read(self, image: bytes, *, time: float, dark: bool, cancel: CancelToken | None) -> str:
        """The text in the encoded image (`dark`: it is a light-on-dark picture, to be inverted first)."""
        ...


_version_cache: dict[str, str] = {}


def _tesseract_version(binary: str) -> str:
    if binary not in _version_cache:
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, timeout=30)
            found = re.search(r"tesseract\s+(\S+)", out.stdout + out.stderr)
        except (OSError, subprocess.SubprocessError):
            found = None
        _version_cache[binary] = f"tesseract {found.group(1)}" if found else "tesseract"
    return _version_cache[binary]


def prepare_graph(dark: bool) -> str:
    """The ffmpeg graph that makes a frame readable for Tesseract: one gray plane, inverted when the picture is dark,
    upscaled 2x with Lanczos. The gray plane is the brightest of the R, G and B planes for a dark picture and the
    darkest for a light one, not the luma: luma turns coloured text (a red error line, a blue keyword) into a grey
    close to a mid-dark background and Tesseract then skips the line, while the brightest (darkest) channel keeps
    it as far from the background as the colour allows. White and black text come out as with luma."""
    mode = "lighten" if dark else "darken"
    return (
        "[0:v]format=rgb24,split=3[a][b][c];[a]extractplanes=r[r];[b]extractplanes=g[g];[c]extractplanes=b[bl];"
        f"[r][g]blend=all_mode={mode}[rg];[rg][bl]blend=all_mode={mode},"
        + ("negate," if dark else "")
        + "scale=iw*2:ih*2:flags=lanczos[o]"
    )


class TesseractEngine:
    """ffmpeg prepares the picture, the `tesseract` program reads it. Files only ever exist in a temp directory."""

    def __init__(self, binary: str, ffmpeg: str) -> None:
        self.binary = binary
        self.ffmpeg = ffmpeg
        self.name = _tesseract_version(binary)

    def _ffmpeg(self, source: str, graph: str, out: list[str], cancel: CancelToken | None) -> Any:
        """Run ffmpeg on one image. `graph` is a plain filter chain, or a `[0:v]...[o]` filter graph."""
        cmd = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", source, "-an", "-sn"]
        cmd += ["-filter_complex", graph, "-map", "[o]"] if graph.endswith("[o]") else ["-vf", graph]
        cmd += ["-frames:v", "1", *out]
        result = run_cancellable(cmd, cancel, text=False)
        if result.returncode != 0:
            tail = result.stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise ExtractError("could not prepare a frame for OCR: " + " ".join(tail))
        return result

    def signature(self, image: bytes, cancel: CancelToken | None) -> bytes:
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            source = os.path.join(tmp, "frame.png")
            with open(source, "wb") as f:
                f.write(image)
            vf = f"scale={SIGNATURE_WIDTH}:{SIGNATURE_HEIGHT}:flags=area,format=gray"
            data = self._ffmpeg(source, vf, ["-f", "rawvideo", "-"], cancel).stdout
        if len(data) != SIGNATURE_WIDTH * SIGNATURE_HEIGHT:
            raise ExtractError("could not make a signature of a frame")
        return data

    def read(self, image: bytes, *, time: float, dark: bool, cancel: CancelToken | None) -> str:
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            source, prepared = os.path.join(tmp, "frame.png"), os.path.join(tmp, "prepared.png")
            with open(source, "wb") as f:
                f.write(image)
            self._ffmpeg(source, prepare_graph(dark), ["-y", prepared], cancel)
            env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
            cmd = [self.binary, prepared, "stdout", "--psm", "6", "-l", "eng"]
            result = run_cancellable(cmd, cancel, env=env)
        if result.returncode != 0:
            detail = " ".join(result.stderr.strip().splitlines()[-3:])
            if "Failed loading language" in detail or "Error opening data file" in detail:
                raise OcrUnavailableError("the English data is missing: install tesseract-ocr-eng")
            raise OcrError(f"tesseract failed at {time:g} s: {detail}")
        return result.stdout


def default_engine() -> OcrEngine:
    """The real engine, or `OcrUnavailableError` (with the install hint) when `tesseract` is not on PATH."""
    binary = shutil.which("tesseract")
    if binary is None:
        raise OcrUnavailableError()
    return TesseractEngine(binary, _require_binary("ffmpeg"))


def require_available() -> None:
    """Raise `OcrUnavailableError` unless the engine can be made. Surfaces call it before queueing or reading."""
    default_engine()


def available() -> bool:
    try:
        require_available()
    except OcrUnavailableError:
        return False
    return True


# --- which frames -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    frames: list[tuple[float, bool]]  # (time, from a scene change), in time order
    before_cap: int  # how many frames the merge produced before the cap
    capped: bool


def _thin(items: list[Any], keep: int) -> list[Any]:
    """`keep` of `items` spread evenly over them (all when `keep` is at least as many)."""
    if keep >= len(items):
        return list(items)
    if keep <= 0:
        return []
    if keep == 1:
        return [items[0]]
    return [items[round(k * (len(items) - 1) / (keep - 1))] for k in range(keep)]


def schedule(
    duration: float,
    scene_times: list[float],
    interval: float = DEFAULT_INTERVAL,
    *,
    min_gap: float = MIN_GAP,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> Plan:
    """The frames to read: a frame `SCENE_OFFSET` after each scene change plus one every `interval` seconds from 0,
    none closer than `min_gap` to its neighbour (a scene frame wins over an interval frame), at most `max_frames`
    (interval frames are thinned first, then scene frames)."""
    last = max(0.0, duration - 0.05)
    candidates = [(round(min(t + SCENE_OFFSET, last), 3), True) for t in scene_times if 0 <= t <= duration]
    k = 0
    while k * interval <= last:
        candidates.append((round(k * interval, 3), False))
        k += 1
    candidates.sort(key=lambda c: (c[0], not c[1]))
    merged: list[tuple[float, bool]] = []
    for time, is_scene in candidates:
        if merged and time - merged[-1][0] < min_gap:
            if is_scene and not merged[-1][1]:
                merged[-1] = (time, True)
            continue
        merged.append((time, is_scene))
    before = len(merged)
    if before <= max_frames:
        return Plan(merged, before, False)
    scene_frames = _thin([f for f in merged if f[1]], max_frames)
    interval_frames = _thin([f for f in merged if not f[1]], max_frames - len(scene_frames))
    return Plan(sorted(scene_frames + interval_frames), before, True)


# --- text ---------------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def normalise(raw: str) -> tuple[str, list[str]]:
    """`(text, lines)`: control characters dropped, trailing whitespace and blank lines removed, at most
    `MAX_TEXT_CHARS` characters (cut at a line)."""
    cleaned = _CONTROL.sub("", raw.replace("\r", "").replace("\x0c", "\n").replace("\t", "    "))
    lines: list[str] = []
    size = 0
    for line in cleaned.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        if size + len(line) + (1 if lines else 0) > MAX_TEXT_CHARS:
            if not lines:
                lines.append(line[:MAX_TEXT_CHARS])
            break
        size += len(line) + (1 if lines else 0)
        lines.append(line)
    return "\n".join(lines), lines


def has_changed(previous: bytes, current: bytes) -> bool:
    """Whether two signatures differ enough to read the frame again (see the module docstring)."""
    a = np.frombuffer(previous, np.uint8).astype(np.int16)
    b = np.frombuffer(current, np.uint8).astype(np.int16)
    if a.shape != b.shape:
        return True
    return int(np.count_nonzero(np.abs(a - b) >= CHANGE_PIXEL_DIFF)) >= CHANGE_PIXELS


def is_dark(signature: bytes) -> bool:
    return float(np.frombuffer(signature, np.uint8).mean()) < DARK_LUMA


# --- storage ------------------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(asset_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(asset_id, threading.Lock())


def _valid(payload: Any, asset: assets.Asset) -> bool:
    return isinstance(payload, dict) and payload.get("version") == VERSION and payload.get("asset") == asset.id


def load(media: MediaStore, asset_id: str) -> dict[str, Any] | None:
    """The asset's stored on-screen text (the document in the module docstring), or None if it has none or the file
    has changed since. `AssetNotFoundError` / `SourceNotFoundError` for a missing asset."""
    asset = assets.get_asset(media, asset_id)
    payload = media.load_cache(asset.path, CACHE_NAME)
    return payload if _valid(payload, asset) else None


def info(media: MediaStore, asset: assets.Asset) -> dict[str, Any] | None:
    """The counts entry for a current document, else None. Cheap."""
    payload = media.load_cache(asset.path, INFO_NAME)
    return payload if _valid(payload, asset) else None


def has_ocr(media: MediaStore, asset: assets.Asset) -> bool:
    """What an asset summary says (`ocr`)."""
    return info(media, asset) is not None


def _states(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The text states: each changed frame that read some text, with when it was replaced (`until`) and its index
    in `frames`."""
    out = []
    for index, frame in enumerate(document["frames"]):
        if frame.get("changed") and frame["text"]:
            out.append(
                {
                    "frame": index,
                    "time": frame["time"],
                    "until": frame.get("until", document["duration"]),
                    "text": frame["text"],
                    "lines": frame["lines"],
                    "source_scene": frame["source_scene"],
                }
            )
    return out


def _overlaps(state: dict[str, Any], lo: float, hi: float) -> bool:
    return state["time"] < hi and state["until"] > lo


def _span(start: float | None, end: float | None) -> tuple[float, float]:
    for name, value in (("start", start), ("end", end)):
        if value is not None and math.isnan(value):
            raise OcrQueryError(f"{name} must be a number of seconds")
    lo = 0.0 if start is None else start
    hi = math.inf if end is None else end
    if lo < 0:
        raise OcrQueryError("start must not be negative")
    if hi <= lo:
        raise OcrQueryError(f"end ({hi:g}) must be after start ({lo:g})")
    return lo, hi


def _stop(value: float) -> float | None:
    return None if math.isinf(value) else value


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in ("time", "until", "text", "lines", "source_scene")}


def ocr_between(
    media: MediaStore, asset_id: str, start: float | None = None, end: float | None = None
) -> list[dict[str, Any]]:
    """The text states on screen at some point in `[start, end)` (either may be None), in time order:
    `{time, until, text, lines, source_scene}`. A state that began before `start` and is still up is included.
    Raises `OcrNotFoundError` without stored text."""
    lo, hi = _span(start, end)
    document = _require(media, asset_id)
    return [_public(s) for s in _states(document) if _overlaps(s, lo, hi)]


def excerpt(document: dict[str, Any], start: float | None = None, end: float | None = None) -> dict[str, Any]:
    """The document cut to the frames from `start` to `end`: its frames whose time is in `[start, end)`, plus the
    changed frame whose text is still up at `start`. Whole when neither bound is given."""
    if start is None and end is None:
        return document
    lo, hi = _span(start, end)
    keep: set[int] = set()
    for index, frame in enumerate(document["frames"]):
        if lo <= frame["time"] < hi:
            keep.add(index)
            if not frame.get("changed"):
                source = frame.get("reused_from")
                keep.update(i for i, f in enumerate(document["frames"]) if f["time"] == source and f.get("changed"))
        elif frame.get("changed") and frame["time"] < lo < frame.get("until", document["duration"]):
            keep.add(index)
    return {**document, "frames": [f for i, f in enumerate(document["frames"]) if i in keep]}


def _require(media: MediaStore, asset_id: str) -> dict[str, Any]:
    document = load(media, asset_id)
    if document is None:
        raise OcrNotFoundError(f"asset {asset_id} has no on-screen text yet: {NOT_FOUND_HINT}")
    return document


# --- cursors and arguments ----------------------------------------------------------------------


def _encode(kind: str, position: int, total: int, tag: str = "") -> str:
    raw = json.dumps({"k": kind, "i": position, "n": total, "q": tag}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str, kind: str, total: int, tag: str = "") -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position, count, found, found_tag = int(data["i"]), int(data["n"]), data["k"], data.get("q", "")
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise OcrQueryError("cursor is not one returned by this tool") from None
    if found != kind:
        raise OcrQueryError(f"cursor is for a different kind of page ({found}, not {kind})")
    if found_tag != tag:
        raise OcrQueryError("cursor belongs to a different query or range: start without it")
    if count != total or not 0 <= position <= total:
        raise OcrQueryError("cursor no longer matches the stored text (it was made again): start without it")
    return position


def _bounded(name: str, value: int | None, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise OcrQueryError(f"{name} must be a whole number from {low} to {high}")
    return value


def _tag(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:10]


def ocr_range(
    media: MediaStore,
    asset_id: str,
    start: float | None = None,
    end: float | None = None,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One page of the text states on screen in `[start, end)`. Returns `{asset, engine, duration, range: {start,
    end}, total_in_range, count, items, next_cursor}`; `items` are `{time, until, text, lines, source_scene}` and
    `next_cursor` is null on the last page. Paging yields every state exactly once, in order."""
    lo, hi = _span(start, end)
    size = _bounded("limit", limit, DEFAULT_RANGE_LIMIT, 1, MAX_LIMIT)
    document = _require(media, asset_id)
    states = [s for s in _states(document) if _overlaps(s, lo, hi)]
    tag = _tag("range", lo, _stop(hi))
    at = _decode(cursor, "range", len(states), tag) if cursor else 0
    page = states[at : at + size]
    nxt = at + len(page)
    return {
        "asset": document["asset"],
        "engine": document["engine"],
        "duration": document["duration"],
        "range": {"start": lo, "end": _stop(hi)},
        "total_in_range": len(states),
        "count": len(page),
        "items": [_public(s) for s in page],
        "next_cursor": _encode("range", nxt, len(states), tag) if nxt < len(states) else None,
    }


# --- search -------------------------------------------------------------------------------------

_TOKEN = re.compile(r"[^\W_]+")


def tokens_of(text: str) -> list[str]:
    """Lower-cased runs of letters and digits (`test_cut_burst` gives test, cut, burst)."""
    return _TOKEN.findall(text.casefold())


def _match_vocabulary(token: str, vocabulary: set[str]) -> dict[str, float]:
    """Every word in `vocabulary` that stands for the query `token`, with how well (see the module docstring)."""
    found: dict[str, float] = {}
    for word in vocabulary:
        if word == token:
            found[word] = 1.0
        elif len(token) >= 4 and token in word:
            found[word] = 0.85
        elif len(token) >= 5 and abs(len(word) - len(token)) <= 2 and word[0] == token[0]:
            matcher = SequenceMatcher(None, token, word)
            if matcher.real_quick_ratio() >= 0.8 and matcher.quick_ratio() >= 0.8 and matcher.ratio() >= 0.8:
                found[word] = round(matcher.ratio() * 0.8, 3)
    return found


def _glued(tokens: list[str]) -> set[str]:
    """Adjacent tokens run together (`07` `7` gives `077`): OCR splits words and digits where there is no gap."""
    out = set()
    for p in range(len(tokens) - 1):
        out.add(tokens[p] + tokens[p + 1])
        if p + 2 < len(tokens):
            out.add(tokens[p] + tokens[p + 1] + tokens[p + 2])
    return {g for g in out if len(g) <= MAX_GLUED}


def _line_matches(line_tokens: list[str], query: list[str], matches: dict[str, dict[str, float]]) -> tuple[float, int]:
    """`(adjacency, tokens found)` of the query tokens in one line: adjacency is 1.0 when they follow each other
    in order, 0.5 when all are present, else 0."""
    present = set(line_tokens) | _glued(line_tokens)
    found = sum(1 for q in dict.fromkeys(query) if any(t in matches[q] for t in present))
    n = len(query)
    for p in range(len(line_tokens) - n + 1):
        if all(line_tokens[p + k] in matches[query[k]] for k in range(n)):
            return 1.0, found
    return (0.5 if found == len(set(query)) else 0.0), found


def ocr_search(
    media: MediaStore,
    asset_id: str,
    query: str,
    *,
    start: float | None = None,
    end: float | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context: int | None = None,
) -> dict[str, Any]:
    """Where the text `query` was on screen: fuzzy, token-based, best first (see the module docstring). Returns
    `{asset, query, tokens, range: {start, end}, total_matches, count, hits, next_cursor}`; each hit is
    `{source: "ocr", time, end, score, text, context_before, context_after, matched_tokens, frame}` (`frame` indexes
    the stored document's `frames`)."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise OcrQueryError(f"query must be text of at most {MAX_QUERY_CHARS} characters")
    wanted = tokens_of(query)
    if not wanted:
        raise OcrQueryError("query is empty: give a word or phrase to look for")
    lo, hi = _span(start, end)
    size = _bounded("limit", limit, DEFAULT_SEARCH_LIMIT, 1, MAX_LIMIT)
    around = _bounded("context", context, DEFAULT_CONTEXT, 0, MAX_CONTEXT)
    document = _require(media, asset_id)
    states = [s for s in _states(document) if _overlaps(s, lo, hi)]

    line_tokens = [[tokens_of(line) for line in s["lines"]] for s in states]
    real = [{t for tokens in per_line for t in tokens} for per_line in line_tokens]
    glued = [
        {g for tokens in per_line for g in _glued(tokens)} - r for per_line, r in zip(line_tokens, real, strict=True)
    ]
    vocabulary = set().union(*real, *glued)
    matches = {q: _match_vocabulary(q, vocabulary) for q in dict.fromkeys(wanted)}

    ranked: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, state in enumerate(states):
        per_line = line_tokens[index]
        frame_tokens = real[index]
        best_scores = []
        for words in matches.values():
            hit = [words[t] for t in frame_tokens if t in words] + [
                GLUE_FACTOR * words[t] for t in glued[index] if t in words
            ]
            if not hit:
                break
            best_scores.append(max(hit))
        else:
            scored = [_line_matches(tokens, wanted, matches) for tokens in per_line]
            best_line = max(range(len(scored)), key=lambda li: (scored[li][1], scored[li][0], -li))
            adjacency = scored[best_line][0]
            score = round(0.75 * sum(best_scores) / len(best_scores) + 0.25 * adjacency, 3)
            lines = state["lines"]
            seen = per_line[best_line]
            matched = []
            for pool in (seen, sorted(frame_tokens), sorted(_glued(seen)), sorted(glued[index])):  # plain words first
                matched = list(dict.fromkeys(t for t in pool if any(t in words for words in matches.values())))
                if matched:
                    break
            ranked.append(
                (
                    -score,
                    state["time"],
                    index,
                    {
                        "source": "ocr",
                        "time": state["time"],
                        "end": state["until"],
                        "score": score,
                        "text": lines[best_line].strip(),
                        "context_before": "\n".join(lines[max(0, best_line - around) : best_line]),
                        "context_after": "\n".join(lines[best_line + 1 : best_line + 1 + around]),
                        "matched_tokens": matched,
                        "frame": state["frame"],
                    },
                )
            )
    ranked.sort(key=lambda r: r[:3])
    tag = _tag("search", wanted, lo, _stop(hi))
    at = _decode(cursor, "hit", len(ranked), tag) if cursor else 0
    page = ranked[at : at + size]
    nxt = at + len(page)
    return {
        "asset": document["asset"],
        "query": query,
        "tokens": wanted,
        "range": {"start": lo, "end": _stop(hi)},
        "total_matches": len(ranked),
        "count": len(page),
        "hits": [r[3] for r in page],
        "next_cursor": _encode("hit", nxt, len(ranked), tag) if nxt < len(ranked) else None,
    }


# --- the pass -----------------------------------------------------------------------------------


def recognize(
    media: MediaStore,
    asset_id: str,
    *,
    interval: float = DEFAULT_INTERVAL,
    max_frames: int = DEFAULT_MAX_FRAMES,
    force: bool = False,
    engine: OcrEngine | None = None,
    workers: int = WORKERS,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Read the asset's on-screen text and store it, unless a current document made with the same `interval`,
    `max_frames` and scene availability exists (`force` redoes it). Returns `{asset, engine, duration, interval,
    max_frames, scenes_used, frames, scene_frames, ocr_frames, skipped_unchanged, capped, frames_before_cap,
    failed_frames, note, made}` (counts; `note` is a plain-language remark when the schedule was capped or had no scene data). An
    audio-only asset gives `{asset, skipped: "audio-only asset", made: False}`.

    Raises `OcrArgumentError` (interval under `MIN_INTERVAL`, a frame cap outside 1..`MAX_FRAMES_LIMIT`),
    `OcrUnavailableError` without tesseract (nothing is read first), `OcrError` / `ExtractError` if a frame fails,
    `AssetNotFoundError` / `SourceNotFoundError` for a missing asset and `JobCancelledError` through `cancel`; a
    failed or cancelled run stores nothing and leaves any earlier document alone."""
    if isinstance(interval, bool) or not isinstance(interval, int | float) or not interval >= MIN_INTERVAL:
        raise OcrArgumentError(f"interval must be at least {MIN_INTERVAL:g} seconds")
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= MAX_FRAMES_LIMIT:
        raise OcrArgumentError(f"max_frames must be a whole number from 1 to {MAX_FRAMES_LIMIT}")
    asset = assets.get_asset(media, asset_id)
    if not asset.has_video:
        return {"asset": asset.id, "skipped": AUDIO_ONLY, "made": False}
    engine = engine if engine is not None else default_engine()
    with _lock_for(asset.id):  # a second job for the same asset waits, then finds the document current
        scene_document = scenes.load(media, asset.id)
        scenes_used = scene_document is not None
        current = None if force else info(media, asset)
        if (
            current is not None
            and current["interval"] == interval
            and current["max_frames"] == max_frames
            and current["scenes_used"] == scenes_used
        ):
            return {**current, "made": False}
        plan = schedule(
            asset.duration,
            [s["time"] for s in scene_document["scenes"]] if scene_document else [],
            interval,
            max_frames=max_frames,
        )
        done = _read_frames(media, asset, plan, engine, workers, on_progress, cancel)
        document, summary = _build(asset, engine, interval, max_frames, scenes_used, plan, done)
        media.save_cache(asset.path, CACHE_NAME, document)
        media.save_cache(asset.path, INFO_NAME, summary)
        if on_progress is not None:
            on_progress(len(plan.frames), len(plan.frames))
        return {**summary, "made": True}


@dataclass
class _Read:
    time: float  # of the frame decoded
    source_scene: bool
    signature: bytes
    text: str = ""
    lines: list[str] | None = None
    changed: bool = True
    reused_from: float | None = None


def _read_frames(
    media: MediaStore,
    asset: assets.Asset,
    plan: Plan,
    engine: OcrEngine,
    workers: int,
    on_progress: ProgressFn | None,
    cancel: CancelToken | None,
) -> list[_Read]:
    """Extract, compare and read every planned frame with up to `workers` in flight. Frames are judged in time
    order against the last one that was read, so the result does not depend on which finished first."""
    total = len(plan.frames)
    if on_progress is not None:
        on_progress(0, total)

    failures: list[Exception] = []

    def grab(position: int) -> tuple[frames.Frame, bytes] | None:
        try:
            frame = frames.extract_frame(
                media,
                asset.id,
                plan.frames[position][0],
                width=FRAME_WIDTH,
                format="png",
                source="original",
                cancel=cancel,
            )
            return frame, engine.signature(frame.data, cancel)
        except (ExtractError, frames.FrameError) as exc:  # one undecodable frame does not sink an hour of work
            failures.append(exc)
            return None

    def text_of(frame: frames.Frame, dark: bool) -> str:
        return engine.read(frame.data, time=frame.time, dark=dark, cancel=cancel)

    results: list[_Read] = []
    reading: dict[int, Future[str]] = {}
    grabbing: dict[int, Future[tuple[frames.Frame, bytes] | None]] = {}
    settled = 0
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ocr")
    ahead = max(1, workers) * 2

    def collect(block: bool) -> None:
        nonlocal settled
        pending = [f for f in reading.values() if not f.done()]
        if block and pending:
            wait(pending, return_when=FIRST_COMPLETED)
        for position in [p for p, f in reading.items() if f.done()]:
            results[position].text = reading.pop(position).result()
            settled += 1
        if on_progress is not None:
            on_progress(min(settled, total), total)

    try:
        last_signature: bytes | None = None
        last_read: int | None = None
        submitted = 0
        for position in range(total):
            while submitted < total and submitted < position + ahead:
                grabbing[submitted] = pool.submit(grab, submitted)
                submitted += 1
            while len(reading) >= ahead:
                collect(True)
            if cancel is not None:
                cancel.raise_if_cancelled()
            grabbed = grabbing.pop(position).result()
            if grabbed is None:
                settled += 1
                collect(False)
                continue
            frame, signature = grabbed
            changed = last_signature is None or has_changed(last_signature, signature)
            item = _Read(frame.time, plan.frames[position][1], signature, changed=changed)
            results.append(item)
            if changed:
                reading[len(results) - 1] = pool.submit(text_of, frame, is_dark(signature))
                last_signature, last_read = signature, len(results) - 1
            else:
                assert last_read is not None
                item.reused_from = results[last_read].time
                settled += 1
            collect(False)
        while reading:
            collect(True)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if failures and not results:
        raise failures[0]
    return results


def _build(
    asset: assets.Asset,
    engine: OcrEngine,
    interval: float,
    max_frames: int,
    scenes_used: bool,
    plan: Plan,
    done: list[_Read],
) -> tuple[dict[str, Any], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    source: dict[str, Any] | None = None
    for item in done:
        if item.changed:
            text, lines = normalise(item.text)
            entry = {
                "time": item.time,
                "text": text,
                "lines": lines,
                "changed": True,
                "source_scene": item.source_scene,
                "until": asset.duration,
            }
            if source is not None:
                source["until"] = item.time
            source = entry
        else:
            entry = {
                "time": item.time,
                "text": "",
                "lines": [],
                "changed": False,
                "source_scene": item.source_scene,
                "reused_from": item.reused_from,
            }
        entries.append(entry)
    read = sum(1 for e in entries if e["changed"])
    notes = []
    if plan.capped:
        notes.append(
            f"capped at {len(plan.frames)} of {plan.before_cap} scheduled frames (max_frames={max_frames}): "
            "raise max_frames or the interval to read the rest"
        )
    if len(entries) < len(plan.frames):
        notes.append(f"{len(plan.frames) - len(entries)} frame(s) could not be decoded and were left out")
    if not scenes_used:
        notes.append(f"no scene data: read every {interval:g} s only; run scenes first to also read each scene change")
    summary = {
        "version": VERSION,
        "asset": asset.id,
        "engine": engine.name,
        "duration": asset.duration,
        "interval": interval,
        "max_frames": max_frames,
        "scenes_used": scenes_used,
        "frames": len(entries),
        "scene_frames": sum(1 for e in entries if e["source_scene"]),
        "ocr_frames": read,
        "skipped_unchanged": len(entries) - read,
        "capped": plan.capped,
        "frames_before_cap": plan.before_cap,
        "failed_frames": len(plan.frames) - len(entries),
        "note": "; ".join(notes) or None,
    }
    document = {
        "version": VERSION,
        "asset": asset.id,
        "engine": engine.name,
        "duration": asset.duration,
        "interval": interval,
        "max_frames": max_frames,
        "scenes_used": scenes_used,
        "capped": plan.capped,
        "frames_before_cap": plan.before_cap,
        "frames": entries,
    }
    return document, summary
