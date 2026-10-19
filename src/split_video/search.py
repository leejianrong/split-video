"""Find a moment by what was said or shown: one search over the transcript and the on-screen text (SPL-99).

An agent asked for "the part where the build fails" has no single place to look: someone says "the build failed"
while the terminal prints `FAILED tests/...`. `search_asset` searches every source the asset has, merges the hits
that are about the same moment and returns one ranked, paged list. `visual_search` is the same question put to what
the screen showed only.

Sources
-------
`SOURCES` is the registry of what can be searched: a `Source` has a `name`, a `kind` ("transcript" or "visual"),
`available(media, asset)` (is there stored data to search?), `hits(...)` (every hit as a dict with `time`, `end`,
`score` in 0..1, `text`, `context_before`, `context_after`, plus a fingerprint of the stored data) and a `hint` (how to
make the data). Today there are two: `transcript` (`transcript_search.transcript_hits`) and `ocr`
(`ocr.ocr_hits`, kind "visual"). Searching is done through the registry, so a source added to it is searched,
merged, filtered by `sources` and reported in the `sources` summary without touching the code below. The place for
VLM captions (SPL-97, deferred) is a third entry, `descriptions`, of kind "visual", added by whatever code makes the
captions; until that exists the source is simply absent and nothing here pretends otherwise. `visual_search` today
is the OCR search (`ocr.ocr_search`: its hits, order and cursors), and a second visual source would have to be
merged into it the way `search_asset` merges all of them.

How a query is matched
----------------------
Each source gets the whole query as a phrase (the transcript: the exact words in a row; on-screen text: all the
words in one frame, fuzzy: see `ocr.py`). When the query has several words each content word (not a stopword or a single letter, at
most `MAX_TOKENS` = 8) is also searched alone in every source, so that "build failed" finds a transcript saying "the
build failed" AND a frame that only shows `FAILED`. A single-word hit that lies inside a phrase hit of the same
source is dropped. Matching is by word: no stemming (`fails` does not find `failed`), no embeddings.

Comparable scores
-----------------
Every hit first gets a source score in 0..1: on-screen text keeps `ocr.ocr_search`'s own (1.0 = every word exactly,
in order, on one line; fuzzy and partial matches less); a transcript match is an exact phrase match, so it scores
0.9 + 0.1 * the mean probability Whisper gave the matched words (0.9 to 1.0). That is then multiplied by the share
of the query's content words the hit covers (a phrase hit covers all: 1.0; a single word of a two-word query: 0.5).
So an exact phrase in the transcript or an exact frame of text is about 1.0 whichever source it comes from, and half
a query is about 0.5.

Moments
-------
Hits within `WINDOW` = 15 seconds of each other (by `time`) are merged into one moment, so the same thing said and
shown is one result with both as `evidence`. Walking the hits best first, a hit starts a moment and takes in the
nearby hits that come from a source the moment does not have yet or that cover a query word it does not. A moment's
score is `base` = the best `score * share covered` among its evidence; with more than one piece of evidence it is
`base + (1 - base) * 0.5 * (share of the query covered by all the evidence together)`: agreement between sources (or
between several words) always raises the score and never takes it over 1. So "the build failed" said AND `FAILED`
on screen (0.99 +) beats an exact phrase in one source only (0.99), which beats `build` and `FAILED` found
separately but close (0.75), which beats one word alone (0.5). A moment has the fields of its best piece of
evidence (`source`, `text`, `context_before`, `context_after`), `time` = the earliest evidence time (where to
seek), `end` = the latest evidence end, `score`, `found_in` (the sources, best evidence first), `match`
("phrase" if any evidence matched the whole query, else "words") and `evidence`, each piece being a hit of its
source (`source`, `time`, `end`, `score` = its source score, `text`, context and the source's own fields) with
`covers` (the query words it matched). Moments come best first, ties by time.

Paging
------
Moments are ordered by (score descending, time, source) and cut into pages of `limit` (default 20, at most 200).
`next_cursor` is opaque and carries the position in that order plus a fingerprint of the query, the filters and
the stored transcript and on-screen text (their stamps), so it is refused when any of them changed or when used
with another query; unchanged data gives every moment exactly once. Each page recomputes the ranking from the
stored documents (a few tens of ms for three hours of speech and 360 frames).

Reports and errors
------------------
The result has `sources`: for every registered source `"searched"`, `"not run: <how to make it>"` or
`"not requested"` (left out by the `sources` filter). If none of the requested sources has data `SearchNotFoundError`
says which commands/tools/routes make it. A bad query, span, limit, cursor, context or source name is a
`SearchQueryError`. A missing asset raises the usual `AssetNotFoundError`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from split_video import assets, ocr, transcribe, transcript_search
from split_video.media_store import MediaStore

WINDOW = 15.0  # seconds: hits this close (by time) are one moment
CORROBORATION = 0.5  # how much of the way to 1.0 agreement between pieces of evidence takes a moment's score
MAX_TOKENS = 8  # content words of a query searched alone
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
MAX_CONTEXT = 10
MAX_QUERY_CHARS = 500

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "so",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "part",
    ]
)

TRANSCRIBE_HINT = (
    "no transcript: run `split-video transcribe <target>` (CLI), the transcribe_asset tool (MCP) or "
    "POST /api/v1/assets/{id}/transcribe (HTTP)"
)
OCR_HINT = (
    "no on-screen text: run `split-video ocr <target>` (CLI), the ocr_asset tool (MCP) or "
    "POST /api/v1/assets/{id}/ocr (HTTP)"
)


class SearchNotFoundError(LookupError):
    """None of the requested sources has anything stored for the asset. The message says how to make it."""


class SearchQueryError(ValueError):
    """A bad query, span, limit, cursor, context or source name."""


# --- the sources ----------------------------------------------------------------------------------

# (media, asset id, query, start, end, context) -> (fingerprint of the stored data, every hit, `score` in 0..1)
HitsFn = Callable[[MediaStore, str, str, float | None, float | None, int | None], tuple[str, list[dict[str, Any]]]]


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "transcript" (what was said) or "visual" (what the screen showed)
    available: Callable[[MediaStore, assets.Asset], bool]
    hits: HitsFn
    hint: str  # how to make the data, for the `sources` summary and the error


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:12]


def _transcript_hits(media, asset_id, query, start, end, context):
    _, stamp, found = transcript_search.transcript_hits(media, asset_id, query, start=start, end=end, context=context)
    hits = []
    for h in found:
        hit = {k: v for k, v in h.items() if k != "probability"}
        hits.append({**hit, "source": "transcript", "score": round(0.9 + 0.1 * min(1.0, h["probability"]), 4)})
    return stamp, hits


def _ocr_hits(media, asset_id, query, start, end, context):
    document, hits = ocr.ocr_hits(media, asset_id, query, start=start, end=end, context=context)
    frames = document["frames"]
    stamp = _fingerprint(
        document["engine"], document["interval"], len(frames), sum(len(f["text"]) for f in frames), document["duration"]
    )
    return stamp, hits


SOURCES: dict[str, Source] = {
    s.name: s
    for s in (
        Source("transcript", "transcript", transcribe.has_transcript, _transcript_hits, TRANSCRIBE_HINT),
        Source("ocr", "visual", ocr.has_ocr, _ocr_hits, OCR_HINT),
    )
}


# --- arguments and cursors ------------------------------------------------------------------------


def _span(start: float | None, end: float | None) -> tuple[float, float]:
    for name, value in (("start", start), ("end", end)):
        if value is not None and math.isnan(value):
            raise SearchQueryError(f"{name} must be a number of seconds")
    lo = 0.0 if start is None else start
    hi = math.inf if end is None else end
    if lo < 0:
        raise SearchQueryError("start must not be negative")
    if hi <= lo:
        raise SearchQueryError(f"end ({hi:g}) must be after start ({lo:g})")
    return lo, hi


def _bounded(name: str, value: int | None, default: int | None, low: int, high: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SearchQueryError(f"{name} must be a whole number from {low} to {high}")
    return value


def _chosen(sources: str | list[str] | None) -> list[str]:
    """The source names asked for (all registered ones when None), in registry order."""
    if sources is None:
        return list(SOURCES)
    names = [s.strip().lower() for s in (sources.split(",") if isinstance(sources, str) else sources) if s.strip()]
    unknown = [n for n in names if n not in SOURCES]
    if unknown or not names:
        raise SearchQueryError(
            f"sources must be a comma-separated list of {', '.join(SOURCES)}"
            + (f" (not {', '.join(unknown)})" if unknown else "")
        )
    return [n for n in SOURCES if n in names]


def _encode(position: int, total: int, tag: str) -> str:
    raw = json.dumps({"k": "moment", "i": position, "n": total, "q": tag}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str, total: int, tag: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position, count, kind, found = int(data["i"]), int(data["n"]), data["k"], data["q"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise SearchQueryError("cursor is not one returned by this tool") from None
    if kind != "moment":
        raise SearchQueryError("cursor is not one returned by this tool")
    if found != tag or count != total or not 0 <= position <= total:
        raise SearchQueryError(
            "cursor belongs to a different query, filter or version of the transcript / on-screen text: start without it"
        )
    return position


def _stop(value: float) -> float | None:
    return None if math.isinf(value) else value


# --- the combined search --------------------------------------------------------------------------


def _query_words(query: str) -> tuple[list[str], list[str]]:
    """`(all the words, the content words)` of a query: the words searched alone are those that are not stopwords
    (or, if all are, the first ones), deduplicated, at most `MAX_TOKENS`."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise SearchQueryError(f"query must be text of at most {MAX_QUERY_CHARS} characters")
    words = ocr.tokens_of(query)
    if not words:
        raise SearchQueryError("query is empty: give a word or phrase to look for")
    unique = list(dict.fromkeys(words))
    content = [w for w in unique if w not in STOPWORDS and len(w) > 1] or unique
    return words, content[:MAX_TOKENS]


def _candidates(
    sources: list[Source],
    media: MediaStore,
    asset_id: str,
    query: str,
    words: list[str],
    content: list[str],
    lo,
    hi,
    context,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every piece of evidence (phrase hits, then single content words) of every source, and the data fingerprints.
    Each has `covers`: the content words it stands for."""
    found: list[dict[str, Any]] = []
    prints: list[str] = []
    start, end = lo, _stop(hi)
    for source in sources:
        stamp, phrase = source.hits(media, asset_id, query, start, end, context)
        prints.append(f"{source.name}:{stamp}")
        phrase = [{**h, "source": source.name, "covers": list(content), "match": "phrase"} for h in phrase]
        found.extend(phrase)
        if len(words) == 1:
            continue  # the phrase is the one word
        for token in content:
            _, alone = source.hits(media, asset_id, token, start, end, context)
            for h in alone:
                if any(p["time"] <= h["time"] < p["end"] for p in phrase):
                    continue  # inside a phrase hit of the same source
                found.append({**h, "source": source.name, "covers": [token], "match": "words"})
    return found, prints


def _moments(found: list[dict[str, Any]], content: list[str]) -> list[dict[str, Any]]:
    """Merge the evidence into moments and rank them (see the module docstring)."""
    for h in found:
        h["_share"] = len(h["covers"]) / len(content)
        h["_value"] = h["score"] * h["_share"]
    by_time = sorted(range(len(found)), key=lambda k: (found[k]["time"], found[k]["source"]))
    times = [found[k]["time"] for k in by_time]
    taken = [False] * len(found)
    best_first = sorted(range(len(found)), key=lambda k: (-found[k]["_value"], found[k]["time"], found[k]["source"]))
    moments = []
    for k in best_first:
        if taken[k]:
            continue
        taken[k] = True
        anchor = found[k]
        evidence = [anchor]
        sources = {anchor["source"]}
        covered = set(anchor["covers"])
        near = by_time[bisect_left(times, anchor["time"] - WINDOW) : bisect_right(times, anchor["time"] + WINDOW)]
        for j in sorted((j for j in near if not taken[j]), key=lambda j: (-found[j]["_value"], found[j]["time"])):
            other = found[j]
            if other["source"] not in sources or not covered.issuperset(other["covers"]):
                taken[j] = True
                evidence.append(other)
                sources.add(other["source"])
                covered.update(other["covers"])
        base = anchor["_value"]
        score = base
        if len(evidence) > 1:
            score = base + (1 - base) * CORROBORATION * len(covered) / len(content)
        evidence.sort(key=lambda e: (e["time"], e["source"]))
        moments.append(
            {
                "source": anchor["source"],
                "time": min(e["time"] for e in evidence),
                "end": max(e["end"] for e in evidence),
                "score": round(score, 4),
                "text": anchor["text"],
                "context_before": anchor["context_before"],
                "context_after": anchor["context_after"],
                "match": "phrase" if any(e["match"] == "phrase" for e in evidence) else "words",
                "found_in": [anchor["source"], *dict.fromkeys(e["source"] for e in evidence if e is not anchor)],
                "evidence": [{k2: v for k2, v in e.items() if not k2.startswith("_")} for e in evidence],
            }
        )
    moments.sort(key=lambda m: (-m["score"], m["time"], m["source"]))
    return moments


def search_asset(
    media: MediaStore,
    asset_id: str,
    query: str,
    *,
    sources: str | list[str] | None = None,
    start: float | None = None,
    end: float | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context: int | None = None,
) -> dict[str, Any]:
    """Find the moments `query` was said or shown, best first, one page (see the module docstring).

    Returns `{asset, query, tokens, range: {start, end}, sources: {name: status}, total_matches, count, hits,
    next_cursor}`; each hit is `{source, time, end, score, text, context_before, context_after, match, found_in,
    evidence}`. `sources` is a comma-separated string or list of names from `SOURCES` (default all)."""
    words, content = _query_words(query)
    lo, hi = _span(start, end)
    size = _bounded("limit", limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    around = _bounded("context", context, None, 0, MAX_CONTEXT)
    wanted = _chosen(sources)
    asset = assets.get_asset(media, asset_id)
    ready = [SOURCES[n] for n in wanted if SOURCES[n].available(media, asset)]
    summary = {
        name: "searched"
        if SOURCES[name] in ready
        else f"not run: {SOURCES[name].hint}"
        if name in wanted
        else "not requested"
        for name in SOURCES
    }
    if not ready:
        raise SearchNotFoundError(
            f"asset {asset_id} has nothing to search in {', '.join(wanted)}: "
            + "; ".join(SOURCES[n].hint for n in wanted)
        )
    found, prints = _candidates(ready, media, asset.id, query, words, content, lo, hi, around)
    moments = _moments(found, content)
    tag = _fingerprint(words, content, [s.name for s in ready], lo, _stop(hi), WINDOW, prints)
    at = _decode(cursor, len(moments), tag) if cursor else 0
    page = moments[at : at + size]
    nxt = at + len(page)
    return {
        "asset": asset.id,
        "query": query,
        "tokens": content,
        "range": {"start": lo, "end": _stop(hi)},
        "sources": summary,
        "total_matches": len(moments),
        "count": len(page),
        "hits": page,
        "next_cursor": _encode(nxt, len(moments), tag) if nxt < len(moments) else None,
    }


def visual_search(
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
    """Find what the screen showed: `ocr.ocr_search` (same hits, order and cursors; every hit has
    `source: "ocr"`) plus `sources`, the visual sources and whether they were searched. `AssetNotFoundError`; the
    `ocr` errors without stored on-screen text (the message names how to make it)."""
    found = ocr.ocr_search(media, asset_id, query, start=start, end=end, limit=limit, cursor=cursor, context=context)
    visual = {name: "searched" for name, s in SOURCES.items() if s.kind == "visual"}
    return {**found, "sources": visual}
