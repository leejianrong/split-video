"""Reviewing highlight suggestions, and making them into clips on the timeline (SPL-64).

`suggest_highlights` (highlights.py) proposes; this module is the review step that docs/ux-spec.md#highlights asks
for. Two things, both engine rules that the HTTP routes, the CLI and the MCP tools only adapt:

**Review state.** Each suggestion has an identity, its `key`, made from the window the engine scored
(`h_<start ms>_<end ms>`), so the same suggestion has the same key on every run with the same parameters. A review
is `accepted` or `dismissed` (no entry: not reviewed yet, `pending`), and may carry an adjusted `start` / `end` (the
user trimmed the suggestion; an adjusted suggestion that is otherwise unreviewed is stored as `pending`). The reviews of an asset are one document stored through `MediaStore.save_cache(<asset
media id>, "highlight-reviews", ...)`: a sidecar next to the video, keyed by the file's size and mtime like every
derived document, so it survives the `make dev` container and a changed file starts afresh. The document has a
`revision` (0 when nothing was saved; each write adds 1) and writes take `base_revision`, as project writes do.

**Making clips.** `make_clips` turns suggestions (as given, with any adjusted edges) into one atomic edit-ops batch
on the video's own timeline (`add_clip` with `in` / `out` and a `label`, and for a short a 9:16 `crop` from
`timeline_ops.crop_for_aspect`), applies it through `edit_ops.apply_ops` with the project's `base_revision`, and then
marks those suggestions `accepted`. The clips are appended to the main track in the order given. A "short" is a
vertical crop of the range; the captions of a short are burned in at export (export_modes `highlights`), so they are
not a clip on the timeline. If the batch is refused nothing changes and no suggestion is accepted.
"""

from __future__ import annotations

import math
import threading
import uuid
from typing import Any

from split_video import assets, edit_ops, project, timeline_ops
from split_video import store as store_module
from split_video.highlights import HighlightsError
from split_video.media_store import MediaStore
from split_video.store import ProjectStore
from split_video.timeline import TimelineError

VERSION = 1
CACHE_NAME = "highlight-reviews"
STATES = ("accepted", "dismissed", "pending")
KINDS = ("short", "clip")
SHORT_ASPECT = (9, 16)
MIN_SECONDS = 1.0
MAX_ITEMS = 100
LABEL_LENGTH = 120

_LOCK = threading.Lock()


class ReviewError(HighlightsError):
    """The review request is unusable (unknown state, empty or reversed range, nothing to make)."""


def key_of(start: float, end: float) -> str:
    """The identity of a suggestion: the window the engine scored, to the millisecond."""
    return f"h_{round(start * 1000)}_{round(end * 1000)}"


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "revision": 0, "reviews": {}}


def load(media: MediaStore, asset_id: str) -> dict[str, Any]:
    """`{"version", "revision", "reviews": {key: {"state", "start", "end", "adjusted"}}}` for the asset (an empty
    document, revision 0, when nothing was reviewed). `AssetNotFoundError` for an unknown id."""
    asset = assets.get_asset(media, asset_id)
    doc = media.load_cache(asset.path, CACHE_NAME)
    if not isinstance(doc, dict) or doc.get("version") != VERSION or not isinstance(doc.get("reviews"), dict):
        return _empty()
    return {"version": VERSION, "revision": int(doc.get("revision", 0)), "reviews": dict(doc["reviews"])}


def _range(start: Any, end: Any, duration: float | None) -> tuple[float, float]:
    try:
        lo, hi = float(start), float(end)
    except (TypeError, ValueError):
        raise ReviewError("start and end must be numbers of seconds") from None
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo < 0:
        raise ReviewError("start must be 0 or more")
    if hi - lo < MIN_SECONDS:
        raise ReviewError(f"a highlight needs to be at least {MIN_SECONDS:g} second long (end after start)")
    if duration is not None and hi > duration + 0.001:
        raise ReviewError(f"end ({hi:g}s) is past the end of the recording ({duration:g}s)")
    return round(lo, 3), round(hi, 3)


def set_review(
    media: MediaStore,
    asset_id: str,
    key: str,
    state: str | None,
    *,
    start: float,
    end: float,
    adjusted_start: float | None = None,
    adjusted_end: float | None = None,
    base_revision: int | None = None,
) -> dict[str, Any]:
    """Record the review of suggestion `key` (whose scored window is `start`..`end`) and return the document.

    `state` is `accepted`, `dismissed` or `pending`; `None` (or `pending` without adjusted edges) clears the review
    (the suggestion is pending again, undo of a dismissal). `adjusted_start` / `adjusted_end` keep the user's own edges (both or neither). A stale
    `base_revision` is `RevisionConflictError`, nothing written."""
    if state is not None and state not in STATES:
        raise ReviewError(f"state must be one of {', '.join(STATES)} (or null to clear), not {state!r}")
    asset = assets.get_asset(media, asset_id)
    lo, hi = _range(start, end, asset.duration)
    if key != key_of(lo, hi):
        raise ReviewError(f"key {key!r} does not match the window {lo:g}..{hi:g} (expected {key_of(lo, hi)})")
    adjusted: tuple[float, float] | None = None
    if (adjusted_start is None) != (adjusted_end is None):
        raise ReviewError("give both adjusted_start and adjusted_end, or neither")
    if adjusted_start is not None:
        adjusted = _range(adjusted_start, adjusted_end, asset.duration)
    with _LOCK:
        doc = load(media, asset_id)
        store_module.check_base_revision("highlight reviews", asset_id, doc["revision"], base_revision)
        reviews = doc["reviews"]
        if state is None or (state == "pending" and (adjusted is None or adjusted == (lo, hi))):
            reviews.pop(key, None)
        else:
            entry: dict[str, Any] = {"state": state, "start": lo, "end": hi}
            if adjusted is not None and adjusted != (lo, hi):
                entry["adjusted"] = {"start": adjusted[0], "end": adjusted[1]}
            reviews[key] = entry
        doc = {"version": VERSION, "revision": doc["revision"] + 1, "reviews": reviews}
        media.save_cache(asset.path, CACHE_NAME, doc)
    return doc


def annotate(suggestions: dict[str, Any], doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The candidates of a `suggest_highlights` result, each with its `key` and its `review`
    (`{"state": "pending"|"accepted"|"dismissed", "start", "end"}`: the edges in force, adjusted or scored)."""
    out = []
    for cand in suggestions["candidates"]:
        key = key_of(cand["start"], cand["end"])
        entry = doc["reviews"].get(key)
        edges = (entry or {}).get("adjusted") or {"start": cand["start"], "end": cand["end"]}
        out.append({**cand, "key": key, "review": {"state": (entry or {}).get("state", "pending"), **edges}})
    return out


def _clean_items(items: Any, duration: float | None) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ReviewError("give at least one highlight to make a clip from")
    if len(items) > MAX_ITEMS:
        raise ReviewError(f"at most {MAX_ITEMS} highlights at once")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ReviewError(f"highlight {i + 1} must be an object with start and end")
        lo, hi = _range(item.get("start"), item.get("end"), duration)
        scored = item.get("scored")
        if isinstance(scored, dict) and "start" in scored and "end" in scored:
            s_lo, s_hi = _range(scored["start"], scored["end"], duration)
        else:
            s_lo, s_hi = lo, hi
        title = " ".join(str(item.get("title") or "").split())[:LABEL_LENGTH]
        out.append({"start": lo, "end": hi, "scored": (s_lo, s_hi), "title": title})
    return out


def label_for(kind: str, title: str, index: int) -> str:
    base = title or f"Highlight {index}"
    label = f"Short: {base}" if kind == "short" else base
    return label[:LABEL_LENGTH]


def make_ops(
    media: MediaStore, asset_id: str, items: list[dict[str, Any]], kind: str, *, video: str, first_index: int = 1
) -> dict[str, Any]:
    """The edit-ops document for `items` (already cleaned): `add_clip` per range, and for a `short` a 9:16
    `set_clip_properties` crop centred on the frame."""
    asset = assets.get_asset(media, asset_id)
    crop = None
    if kind == "short":
        if not asset.width or not asset.height:
            raise ReviewError(f"{asset.path} has no picture, so it cannot be made into a vertical short")
        try:
            crop = timeline_ops.crop_for_aspect(asset.width, asset.height, *SHORT_ASPECT)
        except TimelineError as exc:
            raise ReviewError(str(exc)) from None
    ops: list[dict[str, Any]] = []
    batch = uuid.uuid4().hex[:6]  # clip ids are unique in a project: a second batch must not reuse them
    for i, item in enumerate(items):
        clip_id = f"hl{batch}{i + 1}"
        ops.append(
            {
                "op": "add_clip",
                "id": clip_id,
                "asset": asset.id,
                "in": item["start"],
                "out": item["end"],
                "label": label_for(kind, item["title"], first_index + i),
            }
        )
        if crop is not None and crop != {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}:
            ops.append({"op": "set_clip_properties", "clip": clip_id, "crop": crop})
    return {"schema_version": edit_ops.SCHEMA_VERSION, "project": video, "ops": ops}


def make_clips(
    media: MediaStore,
    store: ProjectStore,
    video: str,
    asset_id: str,
    items: Any,
    kind: str = "short",
    *,
    base_revision: int | None = None,
) -> dict[str, Any]:
    """Add `items` (`[{"start", "end", "title", "scored": {"start", "end"}}]`) to the video's own timeline as labelled
    main-track clips (`kind` `short`: cropped to 9:16; `clip`: the whole frame) in one atomic batch based on
    `base_revision` (the project's, from `GET /project/timeline`), then mark them accepted.

    Returns the ops result (`revision` is the project's new one) plus `reviews` (the review document after) and
    `made` (`[{"key", "clip", "label", "start", "end"}]`). Errors: `ReviewError`; the ops errors
    (`OpsInvalidError`, `RevisionConflictError`) unchanged."""
    if kind not in KINDS:
        raise ReviewError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    asset = assets.get_asset(media, asset_id)
    cleaned = _clean_items(items, asset.duration)
    target = project.video_ops_target(store, media, video)
    existing = sum(1 for t in target.load().tracks for c in t.clips if c.label.startswith(("Short: ", "Highlight ")))
    doc = make_ops(media, asset_id, cleaned, kind, video=video, first_index=existing + 1)
    result = edit_ops.apply_ops(media, store, doc, base_revision=base_revision, target=target)
    added = [o for o in doc["ops"] if o["op"] == "add_clip"]
    made = []
    reviews = load(media, asset_id)
    for i, item in enumerate(cleaned):
        key = key_of(*item["scored"])
        reviews = set_review(
            media,
            asset_id,
            key,
            "accepted",
            start=item["scored"][0],
            end=item["scored"][1],
            adjusted_start=item["start"],
            adjusted_end=item["end"],
        )
        made.append(
            {
                "key": key,
                "clip": added[i]["id"],
                "label": added[i]["label"],
                "start": item["start"],
                "end": item["end"],
            }
        )
    return {**result, "reviews": reviews, "made": made}
