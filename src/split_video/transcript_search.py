"""Read a transcript a page at a time, and search it (see `transcribe.py` for the stored format).

An hour of speech is about 10 000 words; an agent should never have to load that to find one sentence. These two
functions answer from the stored word list (no media is read) and always return a bounded page.

`transcript_range` returns the segments (or, with `granularity="word"`, the words) overlapping `[start, end)`:
an item that starts before `start` but ends after it is included, one that starts at or after `end` is not. The
items are whole (a segment is not cut to the range). A page holds at most `limit` items (default 50 segments or
200 words, at most `MAX_LIMIT` = 500) and, with `max_chars`, stops before the item that would take the text past
that many characters (it always holds at least one item, so a long segment cannot block paging). `next_cursor`
is null on the last page; otherwise pass it back, with the same range, to get the next page. Paging through
a transcript yields every item exactly once, in order.

`transcript_search` finds a phrase. Matching is on words, case-insensitive (unless `case_sensitive`), ignoring
punctuation at the edges of words and treating hyphens and slashes as word breaks, so `"Mister Quilter's"`
finds `mister quilter's manner`. The phrase may span segments (it is matched over the whole word sequence).
A match is whole words only (`quil` does not find `quilter`); there is no regex and no fuzzy or semantic match.
`start` / `end` keep only the hits whose first word overlaps that span. Hits come in time order, `limit` per page
(default 20, at most `MAX_SEARCH_LIMIT` = 200), each with `time` (the first matched word's start), `end`,
`text` (the matched words as stored), `context_before` / `context_after` (up to `context` words each, default 8,
at most 50, taken across segment boundaries), `segment` (index of the segment holding the first word) and
`word` (index of that word in the whole transcript). A query is at most `MAX_QUERY_CHARS` = 500 characters.
Overlapping matches (`a a` in `a a a`) are each reported.

Reading is fast because the last two transcripts used are kept parsed in memory, keyed by the asset and the
`stamp` of its stored entry (see `transcribe.save`): the small info entry is read on every call, so a transcript
made again, or a file that changed, is never served from a stale copy. A search of three hours of speech (36 000
words) takes about 20 ms the first time and a few ms after; a page of a range is under a millisecond once parsed.

Both return the same JSON-safe dicts on every surface (HTTP, CLI, MCP). A cursor is opaque; it is tied to the
transcript it came from and is refused if the transcript has since been made again with a different word count.
Errors are `TranscriptNotFoundError` (no current transcript: the message names the way to make one) and
`TranscriptQueryError` (bad range, limit, cursor or query); a missing asset raises the usual `AssetNotFoundError`.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import threading
from bisect import bisect_left, bisect_right
from itertools import accumulate
from typing import Any

from split_video import assets, transcribe
from split_video.media_store import MediaStore

DEFAULT_SEGMENT_LIMIT = 50
DEFAULT_WORD_LIMIT = 200
MAX_LIMIT = 500
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 200
DEFAULT_CONTEXT = 8
MAX_CONTEXT = 50
MAX_QUERY_CHARS = 500
MAX_CHARS = 200_000
TRANSCRIPT_PAGE_SEGMENTS = 100
TRANSCRIPT_PAGE_BYTES = 28_000  # JSON bytes of one `get_transcript` page's segments (docs/tool-response-caps.md)
GRANULARITIES = ("segment", "word")

NOT_FOUND_HINT = (
    "run `split-video transcribe <target>` (CLI), the transcribe_asset tool (MCP) or "
    "POST /api/v1/assets/{id}/transcribe (HTTP) to make one"
)


class TranscriptNotFoundError(LookupError):
    """The asset has no current transcript (never made, or the file changed since)."""


class TranscriptQueryError(ValueError):
    """A bad range, limit, cursor or query."""


# --- cursors and arguments --------------------------------------------------------------------


def _encode(kind: str, position: int, total: int) -> str:
    raw = json.dumps({"k": kind, "i": position, "n": total}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str, kind: str, total: int) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position, count, found = int(data["i"]), int(data["n"]), data["k"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise TranscriptQueryError("cursor is not one returned by this tool") from None
    if found != kind:
        raise TranscriptQueryError(f"cursor is for a different kind of page ({found}, not {kind})")
    if count != total or not 0 <= position <= total:
        raise TranscriptQueryError("cursor no longer matches the transcript (it was made again): start without it")
    return position


def _finite(name: str, value: float | None) -> float | None:
    if value is not None and math.isnan(value):
        raise TranscriptQueryError(f"{name} must be a number of seconds")
    return value


def _span(start: float | None, end: float | None) -> tuple[float, float]:
    _finite("start", start)
    _finite("end", end)
    lo = 0.0 if start is None else start
    hi = math.inf if end is None else end
    if lo < 0:
        raise TranscriptQueryError("start must not be negative")
    if hi <= lo:
        raise TranscriptQueryError(f"end ({hi:g}) must be after start ({lo:g})")
    return lo, hi


def _bounded(name: str, value: int | None, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TranscriptQueryError(f"{name} must be a whole number from {low} to {high}")
    return value


class _Index:
    """A transcript prepared for reading: the flat word list and the bisection arrays, tokens built on demand."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.segments = document["segments"]
        self.words: list[dict[str, Any]] = []
        self.segment_of: list[int] = []
        for s_index, segment in enumerate(self.segments):
            for w in segment["words"]:
                self.words.append(w)
                self.segment_of.append(s_index)
        self.segment_starts = [s["start"] for s in self.segments]
        self.segment_tops = list(accumulate((s["end"] for s in self.segments), max))
        self.word_starts = [w["start"] for w in self.words]
        self.word_tops = list(accumulate((w["end"] for w in self.words), max))
        self._tokens: dict[bool, tuple[list[str], list[int]]] = {}
        self.stamp = ""

    def tokens(self, case_sensitive: bool) -> tuple[list[str], list[int]]:
        """`(tokens, the index of the word each came from)`."""
        if case_sensitive not in self._tokens:
            tokens: list[str] = []
            word_of: list[int] = []
            for index, w in enumerate(self.words):
                for token in _tokens(w["word"], case_sensitive):
                    tokens.append(token)
                    word_of.append(index)
            self._tokens[case_sensitive] = (tokens, word_of)
        return self._tokens[case_sensitive]


_CACHED = 2  # parsed transcripts kept: an agent works on one or two at a time
_cache: dict[tuple[str, str], _Index] = {}
_cache_lock = threading.Lock()


def _index(media: MediaStore, asset_id: str) -> _Index:
    """The asset's current transcript, prepared. Pages of one transcript are served from a small memory cache keyed
    by the asset and the `stamp` of the stored entry, which is read first (it is tiny and checked against the
    file like the transcript itself), so a transcript made again or a changed file is never served stale."""
    asset = assets.get_asset(media, asset_id)
    stamp = transcribe.current_stamp(media, asset)
    key = (asset.id, stamp or "")
    if stamp:
        with _cache_lock:
            found = _cache.get(key)
        if found is not None:
            return found
    loaded = transcribe.load_stamped(media, asset.id)
    if loaded is None:
        raise TranscriptNotFoundError(f"asset {asset_id} has no transcript yet: {NOT_FOUND_HINT}")
    document, stored_stamp = loaded
    index = _Index(document)
    index.stamp = stored_stamp or ""
    if stored_stamp:
        with _cache_lock:
            _cache[(asset.id, stored_stamp)] = index
            while len(_cache) > _CACHED:
                _cache.pop(next(iter(_cache)))
    return index


def _stop(value: float) -> float | None:
    return None if math.isinf(value) else value


# --- range ------------------------------------------------------------------------------------


def transcript_range(
    media: MediaStore,
    asset_id: str,
    start: float | None = None,
    end: float | None = None,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    granularity: str = "segment",
    max_chars: int | None = None,
) -> dict[str, Any]:
    """One page of the asset's transcript overlapping `[start, end)` (either may be None). See the module docstring.

    Returns `{asset, granularity, duration, range: {start, end}, total_in_range, count, covered: {start, end},
    items, next_cursor}` where `range.end` is null when open, `covered` spans this page's items (null when
    empty), and `items` are `{index, start, end, text}` segments or `{index, segment, start, end, word,
    probability}` words (`index` counts from 0 in the whole transcript)."""
    if granularity not in GRANULARITIES:
        raise TranscriptQueryError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    lo, hi = _span(start, end)
    words_mode = granularity == "word"
    size = _bounded("limit", limit, DEFAULT_WORD_LIMIT if words_mode else DEFAULT_SEGMENT_LIMIT, 1, MAX_LIMIT)
    budget = _bounded("max_chars", max_chars, MAX_CHARS, 1, MAX_CHARS)
    prepared = _index(media, asset_id)
    document = prepared.document
    if words_mode:
        pool, starts, tops = prepared.words, prepared.word_starts, prepared.word_tops
    else:
        pool, starts, tops = prepared.segments, prepared.segment_starts, prepared.segment_tops
    first, last = bisect_right(tops, lo), bisect_left(starts, hi)
    total = len(pool)
    at = max(first, _decode(cursor, granularity, total)) if cursor else first
    items: list[dict[str, Any]] = []
    chars = 0
    index = at
    while index < last and len(items) < size:
        text = pool[index]["word" if words_mode else "text"]
        if items and chars + len(text) > budget:
            break
        chars += len(text)
        item = pool[index]
        if words_mode:
            items.append(
                {
                    "index": index,
                    "segment": prepared.segment_of[index],
                    "start": item["start"],
                    "end": item["end"],
                    "word": item["word"],
                    "probability": item["probability"],
                }
            )
        else:
            items.append({"index": index, "start": item["start"], "end": item["end"], "text": item["text"]})
        index += 1
    return {
        "asset": document["asset"],
        "granularity": granularity,
        "duration": document["duration"],
        "range": {"start": lo, "end": _stop(hi)},
        "total_in_range": max(0, last - first),
        "count": len(items),
        "covered": {"start": items[0]["start"], "end": max(i["end"] for i in items)} if items else None,
        "items": items,
        "next_cursor": _encode(granularity, index, total) if index < last else None,
    }


def transcript_page(
    media: MediaStore,
    asset_id: str,
    start: float | None = None,
    end: float | None = None,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """`get_transcript` bounded for a tool result: the document of `transcribe.excerpt(start, end)` with `segments`
    cut to one page (at most `limit`, default `TRANSCRIPT_PAGE_SEGMENTS`, and at most `TRANSCRIPT_PAGE_BYTES` of
    JSON; at least one segment) from `cursor`. Adds `total_segments`, `total_words` and `first_start`/`last_end`
    (of the whole range), `count`, `truncated`, `next_cursor` and `notices`. Concatenating the pages gives exactly
    the excerpt's segments."""
    size = _bounded("limit", limit, TRANSCRIPT_PAGE_SEGMENTS, 1, MAX_LIMIT)
    _finite("start", start)
    _finite("end", end)
    found = transcribe.load(media, asset_id)
    if found is None:
        raise TranscriptNotFoundError(f"asset {asset_id} has no transcript yet: {NOT_FOUND_HINT}")
    cut = transcribe.excerpt(found, start, end)
    segments = cut["segments"]
    total = len(segments)
    at = _decode(cursor, "get", total) if cursor else 0
    page: list[dict[str, Any]] = []
    used = 0
    index = at
    while index < total and len(page) < size:
        weight = len(json.dumps(segments[index], separators=(",", ":")))
        if page and used + weight > TRANSCRIPT_PAGE_BYTES:
            break
        used += weight
        page.append(segments[index])
        index += 1
    more = index < total
    notices = []
    if more:
        notices.append(
            f"segments {at + 1}-{index} of {total}: call again with cursor=next_cursor to read the rest, or narrow "
            "with start/end (transcript_range and transcript_search read a long recording in small pages)"
        )
    return {
        **cut,
        "segments": page,
        "total_segments": total,
        "total_words": sum(len(s["words"]) for s in segments),
        "first_start": segments[0]["start"] if segments else None,
        "last_end": segments[-1]["end"] if segments else None,
        "count": len(page),
        "truncated": at > 0 or more,
        "next_cursor": _encode("get", index, total) if more else None,
        "notices": notices,
    }


# --- search -----------------------------------------------------------------------------------

_EDGES = re.compile(r"^\W+|\W+$")
_BREAKS = re.compile(r"[-‐-―/]+")


def _tokens(word: str, case_sensitive: bool) -> list[str]:
    """What a stored word matches on: lower-cased (unless asked not), curly quotes straightened, punctuation
    stripped from its edges, split at hyphens and slashes. A word of only punctuation has none."""
    text = word.replace("’", "'").replace("‘", "'")
    if not case_sensitive:
        text = text.casefold()
    return [t for part in _BREAKS.split(text) if (t := _EDGES.sub("", part))]


def _find(prepared: _Index, query: str, lo: float, hi: float, case_sensitive: bool) -> tuple[int, list[int]]:
    """`(tokens in the query, token positions where the phrase starts)` for the matches whose first word overlaps
    `[lo, hi)`."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise TranscriptQueryError(f"query must be text of at most {MAX_QUERY_CHARS} characters")
    wanted = [t for word in query.split() for t in _tokens(word, case_sensitive)]
    if not wanted:
        raise TranscriptQueryError("query is empty: give a word or phrase to look for")
    words = prepared.words
    tokens, word_of = prepared.tokens(case_sensitive)
    n = len(wanted)
    head = wanted[0]
    found = []
    for p in range(len(tokens) - n + 1):
        if tokens[p] != head or tokens[p : p + n] != wanted:
            continue
        w = words[word_of[p]]
        if w["end"] > lo and w["start"] < hi:
            found.append(p)
    return n, found


def _hit(prepared: _Index, p: int, n: int, around: int, case_sensitive: bool) -> dict[str, Any]:
    words = prepared.words
    word_of = prepared.tokens(case_sensitive)[1]
    first, final = word_of[p], word_of[p + n - 1]
    return {
        "time": words[first]["start"],
        "end": words[final]["end"],
        "text": " ".join(w["word"] for w in words[first : final + 1]),
        "context_before": " ".join(w["word"] for w in words[max(0, first - around) : first]),
        "context_after": " ".join(w["word"] for w in words[final + 1 : final + 1 + around]),
        "segment": prepared.segment_of[first],
        "word": first,
    }


def transcript_hits(
    media: MediaStore,
    asset_id: str,
    query: str,
    *,
    start: float | None = None,
    end: float | None = None,
    context: int | None = None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Every place `query` is said, in time order, unpaged: `(document, stamp, hits)`. Each hit is what
    `transcript_search` returns plus `probability` (the mean of the matched words' probabilities). What a search over several sources (`search.py`) merges; `stamp` identifies this transcript."""
    lo, hi = _span(start, end)
    around = _bounded("context", context, DEFAULT_CONTEXT, 0, MAX_CONTEXT)
    prepared = _index(media, asset_id)
    n, found = _find(prepared, query, lo, hi, False)
    word_of = prepared.tokens(False)[1]
    hits = []
    for p in found:
        hit = _hit(prepared, p, n, around, False)
        matched = prepared.words[hit["word"] : word_of[p + n - 1] + 1]
        hit["probability"] = sum(w["probability"] for w in matched) / len(matched)
        hits.append(hit)
    return prepared.document, prepared.stamp, hits


def transcript_search(
    media: MediaStore,
    asset_id: str,
    query: str,
    *,
    start: float | None = None,
    end: float | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context: int | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """The places the phrase `query` is said, in time order, one page. See the module docstring.

    Returns `{asset, query, range: {start, end}, total_matches, count, hits, next_cursor}`; each hit is
    `{time, end, text, context_before, context_after, segment, word}`."""
    lo, hi = _span(start, end)
    size = _bounded("limit", limit, DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT)
    around = _bounded("context", context, DEFAULT_CONTEXT, 0, MAX_CONTEXT)
    prepared = _index(media, asset_id)
    n, found = _find(prepared, query, lo, hi, case_sensitive)
    total = len(prepared.tokens(case_sensitive)[0])

    at = _decode(cursor, "hit", total) if cursor else 0
    # The cursor is the token position of the first hit of the page; skip the hits before it.
    page = [p for p in found if p >= at]
    shown, rest = page[:size], page[size:]
    return {
        "asset": prepared.document["asset"],
        "query": query,
        "range": {"start": lo, "end": _stop(hi)},
        "total_matches": len(found),
        "count": len(shown),
        "hits": [_hit(prepared, p, n, around, case_sensitive) for p in shown],
        "next_cursor": _encode("hit", rest[0], total) if rest else None,
    }
