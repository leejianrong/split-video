"""Edit-operation batches: a list of timeline edits applied atomically, with dry-run (SPL-50).

The second half of the agent edit contract (the first is `edit_plan.py`): where a plan replaces or creates a whole
project, a batch of OPERATIONS edits an existing one incrementally. Each operation maps 1:1 to a single edit of
`timeline_ops` / `timeline_service` (add_clip, trim_clip, ...). The ops run in order on an in-memory `Project`
(the pure `timeline_ops` functions), and only if every one succeeds is the result saved, with ONE
`ProjectStore.update_timeline` under the store's write lock and the caller's `base_revision`. A failed batch
changes nothing; a dry run never writes at all (not the project, not the asset table).

    validate_ops(media, store, ops_doc, ...) -> OpsReport     the same engine as a dry run, as a report
    apply_ops(media, store, ops_doc, *, dry_run=False, base_revision=None, continue_on_error=False) -> dict
    migrate_ops(doc)                                          older `schema_version`s to the current one
    schema() / schema_json()                                  the JSON Schema (draft 2020-12)

`docs/edit-ops.md` is the contract and `docs/schemas/edit-ops.v1.schema.json` its machine-readable form, generated
by `schema()` from the `OPS` table below, which the validator reads too. The error report has the plan's shape
(`edit_plan.PlanIssue`: `path`, `code`, `message`, `hint`; `ops.*` codes, with the `plan.*` ones reused where the
problem is the same, e.g. an unknown asset or a range past the end of the source).

Two phases. First every operation is checked for SHAPE (known op, known fields, types, ranges): this needs no
project, so all such problems are reported together. Then the operations are applied in order, and the first one
the project refuses stops the batch, because later ops may depend on what it would have done. A dry run with
`continue_on_error` instead goes on and collects every independent problem; an op that uses an id created by a
failed op is reported as `ops.skipped_dependency`.

Clips and tracks are addressed by id. A creating op (`add_clip`, `add_text_clip`, `add_track`) may be given an `id`,
and later ops in the same batch name it; without one the id is generated deterministically from the batch and the
op's position, and reported in the result.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from split_video import assets, timeline, timeline_ops, timeline_service
from split_video import store as store_module
from split_video.edit_plan import (
    _ID_RE,
    CLIP_FIELDS,
    MAX_ID_LENGTH,
    MAX_TEXT_LENGTH,
    SCHEMA_URI,
    Field,
    PlanIssue,
    PlanReport,
    _check_object,
    _Context,
    _generated_id,
    _object_schema,
    _pointer,
    canonical_hash,
)
from split_video.media_store import MediaStore
from split_video.store import ProjectStore
from split_video.timeline import TRACK_KINDS, Project, TimelineError

SCHEMA_VERSION = 1
SCHEMA_ID = "urn:split-video:edit-ops:1"
MAX_OPS = 1000


# --- the op table: the single source of truth for validation and for the published schema ------------------------------


def _clip_field(name: str, **changes: Any) -> Field:
    """A field of the plan's clip table (same meaning, range and default), as an op field."""
    return replace(next(f for f in CLIP_FIELDS if f.name == name), **changes)


def _id_field(kind: str) -> Field:
    return Field(
        "id",
        "string",
        f"Id for the new {kind}, so later ops in this batch can name it (letters, digits, `_`, `-`, `.`). "
        "Generated deterministically from the batch and this op's position when absent; the result reports it.",
        max_length=MAX_ID_LENGTH,
    )


def _ref(name: str, required: bool = True, extra: str = "") -> Field:
    kind = "clip" if name == "clip" else "track"
    return Field(
        name,
        "string",
        f"Id of the {kind} (from show_timeline, or the `id` of an earlier op in this batch){extra}.",
        required=required,
    )


OP_FIELD = "op"


def _op_field(name: str) -> Field:
    return Field(OP_FIELD, "string", "The operation.", required=True, const=name)


_TO_INDEX = Field(
    "to_index", "integer", "Zero-based position to move to (the list order of the track).", required=True, minimum=0
)
# In a `set_*` op an absent field means "leave it", so the table's defaults for new clips do not apply.
_PROPERTY_FIELDS = tuple(
    _clip_field(n, default=None)
    for n in ("gain", "muted", "x", "y", "scale", "opacity", "brightness", "contrast", "saturation")
)
_TEXT_FIELDS = (
    Field("text", "string", "The title's words (not blank).", max_length=MAX_TEXT_LENGTH),
    _clip_field("font_size", default=None),
    _clip_field("color", default=None),
)


@dataclass(frozen=True)
class OpSpec:
    name: str
    summary: str
    fields: tuple[Field, ...]  # includes the `op` field
    creates: str | None = None  # "clip" or "track": the kind of id a creating op allocates
    any_of: tuple[str, ...] = ()  # at least one of these fields must be given (the op would change nothing)
    needs_media_source: bool = False  # one of `asset` / `source` is required

    @property
    def refs(self) -> tuple[str, ...]:
        """The fields that name an existing clip or track."""
        return tuple(f.name for f in self.fields if f.name in ("clip", "track") and f.name != "id")


def _spec(name: str, summary: str, *fields: Field, **kw: Any) -> OpSpec:
    return OpSpec(name, summary, (_op_field(name), *fields), **kw)


OPS: dict[str, OpSpec] = {
    s.name: s
    for s in (
        _spec(
            "add_clip",
            "Add a clip of an asset to a track (`timeline_service.add_clip`).",
            _id_field("clip"),
            _ref("track", required=False, extra="; default: the main video track"),
            Field("asset", "string", "Asset id (`a_` + 16 hex digits, see list_assets). Preferred over `source`."),
            Field("source", "string", "A path relative to the workspace root that is already an asset."),
            Field(
                "index",
                "integer",
                "Magnetic track: zero-based position to insert at (default: the end). Not allowed on other tracks.",
                minimum=0,
            ),
            _clip_field("start", description="Other tracks: where the clip begins (default: after the last clip)."),
            _clip_field("in", description="Seconds into the source where the clip starts."),
            _clip_field("out", description="Seconds into the source where it ends (default: the end of the asset)."),
            creates="clip",
            needs_media_source=True,
        ),
        _spec(
            "trim_clip",
            "Change which part of its source a clip uses (`timeline_service.trim_clip`).",
            _ref("clip"),
            _clip_field("in", description="New in point, seconds into the source.", default=None),
            _clip_field("out", description="New out point, seconds into the source.", default=None),
            any_of=("in", "out"),
        ),
        _spec(
            "move_clip",
            "Move a clip within its magnetic track (`timeline_service.move_clip`).",
            _ref("clip"),
            _TO_INDEX,
        ),
        _spec("remove_clip", "Remove a clip (`timeline_service.remove_clip`).", _ref("clip")),
        _spec(
            "set_transition",
            "Set how a clip joins the one before it (`timeline_service.set_transition`).",
            _ref("clip"),
            _clip_field("transition", required=True, default=None),
            Field(
                "duration",
                "number",
                "Crossfade seconds; required above 0 with `crossfade`, ignored for `cut`.",
                minimum=0.0,
                default=0.0,
            ),
        ),
        _spec(
            "set_clip_properties",
            "Change gain, mute, overlay placement or colour of a clip (`timeline_service.set_clip_properties`).",
            _ref("clip"),
            *_PROPERTY_FIELDS,
            any_of=tuple(f.name for f in _PROPERTY_FIELDS),
        ),
        _spec(
            "add_text_clip",
            "Add a title to an overlay video track (`timeline_service.add_text_clip`).",
            _id_field("clip"),
            _ref("track"),
            Field("text", "string", "The title's words (not blank).", required=True, max_length=MAX_TEXT_LENGTH),
            Field(
                "duration",
                "number",
                f"How long the title shows, seconds (at least {timeline_ops.MIN_CLIP:g}).",
                required=True,
                minimum=timeline_ops.MIN_CLIP,
            ),
            _clip_field("start", description="Where the title begins on the timeline.", default=0.0),
            _clip_field("font_size"),
            _clip_field("color"),
            _clip_field("x"),
            _clip_field("y", default=0.85),
            creates="clip",
        ),
        _spec(
            "set_text_properties",
            "Change the words, size or colour of a text clip (`timeline_service.set_text_properties`).",
            _ref("clip"),
            *_TEXT_FIELDS,
            any_of=("text", "font_size", "color"),
        ),
        _spec(
            "add_track",
            "Append an empty track on top of the stack (`timeline_service.add_track`).",
            _id_field("track"),
            Field("kind", "string", "`video` or `audio`.", required=True, enum=TRACK_KINDS),
            Field(
                "magnetic",
                "boolean",
                "Keep clips back to back (like the main track). Default false: clips are placed with `start`.",
                default=False,
            ),
            creates="track",
        ),
        _spec("remove_track", "Remove a track and its clips (`timeline_service.remove_track`).", _ref("track")),
        _spec(
            "move_track",
            "Reorder tracks; later tracks draw on top (`timeline_service.move_track`).",
            _ref("track"),
            _TO_INDEX,
        ),
        _spec(
            "place_clip",
            "Put a clip at a start time on a free track, optionally on another track (`timeline_service.place_clip`).",
            _ref("clip"),
            _clip_field("start", description="New start on the timeline, seconds (default: unchanged)."),
            _ref("track", required=False, extra=" to move the clip to (same kind; default: its own track)"),
        ),
    )
}

TOP_FIELDS: tuple[Field, ...] = (
    Field(
        "schema_version",
        "integer",
        "Version of this ops format. Additive optional fields keep the version; a breaking change bumps it.",
        required=True,
        const=SCHEMA_VERSION,
    ),
    Field("project", "string", "Name of the existing timeline project to edit.", required=True),
    Field(
        "base_revision",
        "integer",
        "The revision of the project these ops were written against; the batch fails with a 409 / revision "
        "conflict, writing nothing, if it has moved on. null or absent: apply to the latest version.",
        minimum=0,
        nullable=True,
    ),
    Field(
        "continue_on_error",
        "boolean",
        "Dry run only: keep going after a failing op and report every independent problem (ops that use an id from "
        "a failed op are `ops.skipped_dependency`). By default the first failing op stops the batch.",
        default=False,
    ),
    Field("ops", "array", "The operations, applied in order.", required=True),
    Field("meta", "object", "Free-form notes for the caller; echoed in the response, never stored or validated."),
)


def _op_schema(spec: OpSpec) -> dict[str, Any]:
    body = _object_schema(spec.fields)
    body["description"] = spec.summary
    conditions: list[dict[str, Any]] = []
    if spec.any_of:
        conditions.append({"anyOf": [{"required": [name]} for name in spec.any_of]})
    if spec.needs_media_source:
        conditions.append({"anyOf": [{"required": ["asset"]}, {"required": ["source"]}]})
    if conditions:
        body["allOf"] = conditions
    return body


def schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a version 1 ops document, built from the `OPS` table."""
    top = _object_schema(TOP_FIELDS)
    top["properties"]["ops"] = {
        **top["properties"]["ops"],
        "minItems": 1,
        "maxItems": MAX_OPS,
        "items": {"oneOf": [{"$ref": f"#/$defs/{name}"} for name in OPS]},
    }
    return {
        "$schema": SCHEMA_URI,
        "$id": SCHEMA_ID,
        "title": "split-video edit operations, schema_version 1",
        "description": (
            "A batch of timeline edits applied in order and atomically to an existing project: all of them or "
            "none. Strict: unknown ops and fields are rejected. Constraints this schema cannot express (clip and "
            "track ids exist, assets exist, in/out within the asset, no overlaps, crossfade room) are checked by "
            f"the server and reported with stable codes; see docs/edit-ops.md. At most {MAX_OPS} ops."
        ),
        **top,
        "$defs": {name: _op_schema(spec) for name, spec in OPS.items()},
    }


def schema_json() -> str:
    """`schema()` as the exact text committed to docs/schemas/edit-ops.v1.schema.json."""
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"


# --- the report ----------------------------------------------------------------------------------------------------


@dataclass
class OpsReport(PlanReport):
    """`PlanReport` for a batch: the same `errors` / `warnings` (`PlanIssue`), `ops_hash` instead of `plan_hash`, plus
    how far the batch got. `results` has one entry per op that was tried."""

    applied: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    timeline: dict[str, Any] | None = None  # dry run: the project after the ops that applied

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "ops_hash": self.plan_hash,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "results": self.results,
        }
        if self.timeline is not None:
            out["timeline"] = self.timeline
        return out


class OpsInvalidError(TimelineError):
    """The batch has problems; nothing was written. `report` lists them and what did apply in memory."""

    def __init__(self, report: OpsReport) -> None:
        self.report = report
        super().__init__(compact(report))


def invalid_body(report: OpsReport) -> dict[str, Any]:
    """The JSON an invalid batch is answered with on every surface (HTTP 422 body, CLI stdout, MCP error text)."""
    count = len(report.errors)
    detail = f"the batch has {count} problem{'s' if count != 1 else ''}; nothing was written"
    return {"error": "ops_invalid", "detail": detail, **report.to_dict()}


def compact(report: OpsReport) -> str:
    """The report as compact JSON (what an MCP tool error carries)."""
    return json.dumps(invalid_body(report), separators=(",", ":"))


# --- versions ----------------------------------------------------------------------------------------------------


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class OpsVersionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def migrate_ops(doc: dict[str, Any]) -> dict[str, Any]:
    """`doc` brought up to `SCHEMA_VERSION` (a new dict); the same policy as `edit_plan.migrate_plan`."""
    version = doc.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise OpsVersionError("ops.schema_version.invalid", "schema_version must be an integer")
    if version > SCHEMA_VERSION:
        raise OpsVersionError(
            "ops.schema_version.unsupported",
            f"schema_version {version} is newer than this server supports ({SCHEMA_VERSION})",
        )
    doc = copy.deepcopy(doc)
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            oldest = min([*MIGRATIONS, SCHEMA_VERSION])
            raise OpsVersionError(
                "ops.schema_version.unsupported",
                f"schema_version {version} is no longer supported (the oldest accepted is {oldest})",
            )
        doc = step(doc)
        version += 1
        doc["schema_version"] = version
    return doc


# --- checking the shape of the batch ---------------------------------------------------------------------------------


@dataclass
class _Op:
    index: int
    raw: Any
    spec: OpSpec | None = None
    ok: bool = True  # the op's shape is fine (its own problems are in the report)

    @property
    def path(self) -> str:
        return _pointer("ops", self.index)

    def creates_id(self) -> str | None:
        value = self.raw.get("id") if isinstance(self.raw, dict) else None
        return value if self.spec is not None and self.spec.creates and isinstance(value, str) else None


def _check_ops(ops: list[Any], report: OpsReport) -> list[_Op]:
    """The shape of every op, all problems at once: a known op name, known fields, types and ranges, ids."""
    names = ", ".join(OPS)
    parsed: list[_Op] = []
    claimed: dict[tuple[str, str], str] = {}
    for i, raw in enumerate(ops):
        op = _Op(i, raw)
        parsed.append(op)
        before = len(report.errors)
        if not isinstance(raw, dict):
            report.error(
                op.path, "ops.field.type", "an op must be an object", f'send {{"op": "<name>", ...}}; ops: {names}'
            )
        else:
            name = raw.get("op")
            if name is None:
                report.error(op.path + "/op", "ops.field.required", "missing required field 'op'", f"ops: {names}")
            elif not isinstance(name, str) or name not in OPS:
                report.error(
                    op.path + "/op",
                    "ops.op.unknown",
                    f"unknown op {name!r}",
                    f"the ops are: {names}; see GET /api/v1/schemas/edit-ops",
                )
            else:
                op.spec = OPS[name]
                _check_fields(op, report, claimed)
        op.ok = len(report.errors) == before
    return parsed


def _check_fields(op: _Op, report: OpsReport, claimed: dict[tuple[str, str], str]) -> None:
    spec, raw = op.spec, op.raw
    assert spec is not None
    good = _check_object(raw, spec.fields, op.path, report, ns="ops")
    if spec.any_of and not any(name in raw for name in spec.any_of):
        report.error(
            op.path,
            "ops.op.no_changes",
            f"{spec.name} needs at least one of: {', '.join(spec.any_of)}",
            "an op that changes nothing is a mistake; add the field to change",
        )
    if spec.name in ("add_text_clip", "set_text_properties") and "text" in good and not good["text"].strip():
        report.error(op.path + "/text", "plan.clip.text_required", "text can't be empty", "give the title some words")
    new_id = good.get("id")
    if isinstance(new_id, str) and spec.creates:
        if not _ID_RE.match(new_id):
            report.error(
                op.path + "/id",
                "ops.id.invalid",
                f"id {new_id!r} may only contain letters, digits, '_', '-' and '.'",
                "",
            )
        elif (spec.creates, new_id) in claimed:
            report.error(
                op.path + "/id",
                "ops.id.duplicate",
                f"id {new_id!r} is already used by the op at {claimed[(spec.creates, new_id)]}",
                "ids must be unique among the clips (and among the tracks) of the batch; drop `id` to have one generated",
            )
        else:
            claimed[(spec.creates, new_id)] = op.path


# --- applying one op -------------------------------------------------------------------------------------------------


class _Failure(Exception):
    """An op the project refuses: `issues` are what to report (their paths are complete)."""

    def __init__(self, issues: list[PlanIssue]) -> None:
        super().__init__(issues[0].message)
        self.issues = issues


def _fail(op: _Op, code: str, message: str, hint: str = "", field_name: str | None = None) -> _Failure:
    path = f"{op.path}/{field_name}" if field_name else op.path
    return _Failure([PlanIssue(path, code, message, hint)])


# `RuleViolation.reason` -> (code, field, hint). Where a reason applies to a field that differs per op, `field` is None
# and `_rule_failure` picks it.
_RULES: dict[str, tuple[str, str | None, str]] = {
    "in_negative": ("plan.clip.in_negative", "in", ""),
    "out_past_end": ("plan.clip.out_of_range", "out", "the asset is shorter than that"),
    "too_short": ("plan.clip.too_short", None, ""),
    "crossfade_first": (
        "plan.clip.crossfade_first",
        "transition",
        'the first clip has nothing to fade from; use "cut"',
    ),
    "crossfade_duration": ("plan.clip.transition_duration", "duration", "set duration above 0"),
    "crossfade_room": (
        "plan.clip.transition_too_long",
        "duration",
        "shorten the crossfade or lengthen the neighbouring clips",
    ),
    "start_negative": ("ops.clip.start_negative", "start", "start at 0 or later"),
    "overlap": ("plan.clip.overlap", "start", "move it later, put it on another track, or shorten the earlier clip"),
    "magnetic_start": (
        "plan.clip.start_on_magnetic_track",
        "start",
        "this track keeps its clips back to back: use `index`, or pick a free track",
    ),
    "free_index": ("ops.clip.index_on_free_track", "index", "this track places clips with `start`"),
    "index_range": ("ops.index.out_of_range", None, "indexes are zero-based positions in the current list"),
    "move_on_free": ("ops.clip.move_on_free_track", "clip", "clips on a free track are positioned with place_clip"),
    "crossfade_free_track": (
        "plan.clip.crossfade_on_free_track",
        "clip",
        "crossfades are only available on the main (magnetic) track",
    ),
    "text_target": (
        "plan.clip.text_on_main_track",
        "track",
        'put titles on a video track with "magnetic": false (add_track makes one)',
    ),
    "not_text_clip": ("ops.clip.not_text", "clip", "only clips created by add_text_clip have text properties"),
    "last_video_track": ("ops.track.last_video", "track", "a project keeps at least one video track"),
    "kind_mismatch": ("ops.clip.track_kind_mismatch", "track", "a clip can only move to a track of its own kind"),
    "main_track_dest": (
        "ops.clip.dest_is_magnetic",
        "track",
        "the main track keeps clips back to back; use move_clip or add_clip",
    ),
}


def _rule_failure(op: _Op, exc: TimelineError) -> _Failure:
    reason = getattr(exc, "reason", None)
    if reason not in _RULES:
        return _fail(op, "ops.rule.violated", str(exc), "")
    code, field_name, hint = _RULES[reason]
    if field_name is None:
        field_name = {"index_range": "index" if op.spec and op.spec.name == "add_clip" else "to_index"}.get(
            reason, "duration" if op.spec and op.spec.name == "add_text_clip" else "out"
        )
    return _fail(op, code, str(exc), hint, field_name)


@dataclass
class _Run:
    """What the ops of one batch share while they are applied."""

    ctx: _Context
    book: assets.AssetBook
    report: OpsReport
    batch_hash: str
    taken: set[str]  # every id in the project and every explicit id of the batch: generated ids avoid them


def _track(run: _Run, project: Project, op: _Op, name: str = "track") -> timeline.Track:
    track_id = op.raw[name]
    try:
        return timeline_ops.find_track(project, track_id)
    except TimelineError:
        raise _fail(
            op,
            "ops.track.not_found",
            f"no track {track_id!r}",
            "track ids come from show_timeline, or from the `id` of an add_track earlier in this batch",
            name,
        ) from None


def _clip(project: Project, op: _Op) -> tuple[timeline.Track, int]:
    clip_id = op.raw["clip"]
    try:
        return timeline_ops.find_clip(project, clip_id)
    except TimelineError:
        raise _fail(
            op,
            "ops.clip.not_found",
            f"no clip {clip_id!r}",
            "clip ids come from show_timeline, or from the `id` of an add_clip / add_text_clip earlier in this batch",
            "clip",
        ) from None


def _new_id(run: _Run, project: Project, op: _Op, kind: str) -> str:
    """The id a creating op gives its clip or track: the one it names, or a generated one. A named id that the
    project already uses is `ops.id.duplicate`."""
    given = op.raw.get("id")
    existing = {c.id for t in project.tracks for c in t.clips} if kind == "clip" else {t.id for t in project.tracks}
    if given is not None:
        if given in existing:
            raise _fail(
                op,
                "ops.id.duplicate",
                f"the project already has a {kind} with id {given!r}",
                "pick another id, or drop `id` to have one generated",
                "id",
            )
        return given
    return _generated_id(run.batch_hash, kind, op.index, taken=run.taken)


def _rename(project: Project, kind: str, old: str, new: str) -> Project:
    if old == new:
        return project
    if kind == "track":
        return replace(project, tracks=tuple(replace(t, id=new) if t.id == old else t for t in project.tracks))
    tracks = tuple(
        replace(t, clips=tuple(replace(c, id=new) if c.id == old else c for c in t.clips)) for t in project.tracks
    )
    return replace(project, tracks=tracks)


def _clip_asset(run: _Run, clip: timeline.Clip, op: _Op) -> float:
    """How long a media clip's source is, read from the asset table (never relinking: a dry run must not write)."""
    found = assets.peek(run.book, clip.asset, clip.source)
    if found is None:
        raise _fail(
            op,
            "ops.clip.source_missing",
            f"the source of clip {clip.id!r} is not a known asset",
            "scan_library registers files; the clip's asset must be in list_assets",
            "clip",
        )
    return found.asset.duration


def _check_in_out(op: _Op, in_point: float, out_point: float) -> None:
    if out_point <= in_point:
        raise _fail(
            op,
            "plan.clip.in_after_out",
            f"out ({out_point:g}s) must be after in ({in_point:g}s)",
            "swap them or fix the values",
            "out",
        )


def _op_add_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    raw = op.raw
    if "track" in raw:
        track = _track(run, project, op)
    else:
        track = next((t for t in project.tracks if t.kind == "video"), None)
        if track is None:
            raise _fail(op, "ops.track.not_found", "the project has no video track", "", "track")
    scratch = PlanReport()  # the asset lookup is the plan's: its problems come back here to be reported as this op's
    run.ctx.report = scratch
    try:
        found = run.ctx.find_asset(raw, op.path)
    finally:
        run.ctx.report = run.report
    run.report.warnings.extend(scratch.warnings)
    if found is None:
        raise _Failure(scratch.errors)
    row, source = found
    if track.kind == "video" and not row.has_video:
        raise _fail(
            op,
            "plan.clip.audio_only_on_video_track",
            f"{row.path} has no video stream, so it can't go on a video track",
            "put audio-only assets on an audio track",
        )
    if track.kind == "audio" and not row.has_audio:
        raise _fail(
            op,
            "plan.clip.no_audio_for_audio_track",
            f"{row.path} has no audio stream, so it can't go on an audio track",
            "",
        )
    in_point = float(raw.get("in", 0.0))
    out_point = float(raw["out"]) if "out" in raw else row.duration
    _check_in_out(op, in_point, out_point)
    clip_id = _new_id(run, project, op, "clip")
    try:
        new, clip = timeline_ops.add_clip(
            project,
            track.id,
            source,
            row.duration,
            in_point=in_point,
            out_point=out_point,
            index=raw.get("index"),
            start=None if raw.get("start") is None else float(raw["start"]),
            asset=row.id,
        )
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    new = _rename(new, "clip", clip.id, clip_id)
    return (
        new,
        f"added clip {clip_id} ({row.id} {in_point:g}s..{out_point:g}s) to track {track.id}",
        {
            "clip": clip_id,
            "track": track.id,
        },
    )


def _op_trim_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, i = _clip(project, op)
    clip = track.clips[i]
    duration = float("inf") if clip.kind == "text" else _clip_asset(run, clip, op)
    new_in = float(op.raw["in"]) if "in" in op.raw else clip.in_point
    new_out = float(op.raw["out"]) if "out" in op.raw else clip.out_point
    _check_in_out(op, new_in, new_out)
    try:
        new = timeline_ops.trim_clip(
            project,
            clip.id,
            duration,
            in_point=new_in if "in" in op.raw else None,
            out_point=new_out if "out" in op.raw else None,
        )
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"trimmed clip {clip.id} to {new_in:g}s..{new_out:g}s", {"clip": clip.id, "track": track.id}


def _op_move_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    try:
        new = timeline_ops.move_clip(project, op.raw["clip"], op.raw["to_index"])
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return (
        new,
        f"moved clip {op.raw['clip']} to position {op.raw['to_index']}",
        {
            "clip": op.raw["clip"],
            "track": track.id,
        },
    )


def _op_remove_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    return (
        timeline_ops.remove_clip(project, op.raw["clip"]),
        f"removed clip {op.raw['clip']}",
        {
            "clip": op.raw["clip"],
            "track": track.id,
        },
    )


def _op_set_transition(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    transition = op.raw["transition"]
    duration = float(op.raw.get("duration", 0.0))
    if transition == "cut" and duration > 0:
        run.report.warn(
            op.path + "/duration",
            "plan.clip.transition_duration_ignored",
            "duration has no effect on a cut",
            "",
        )
    try:
        new = timeline_ops.set_transition(project, op.raw["clip"], transition, duration)
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    label = "cut" if transition == "cut" else f"crossfade {duration:g}s"
    return new, f"set clip {op.raw['clip']} to {label}", {"clip": op.raw["clip"], "track": track.id}


def _op_set_clip_properties(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    changes = {f.name: op.raw[f.name] for f in _PROPERTY_FIELDS if f.name in op.raw}
    try:
        new = timeline_ops.set_clip_properties(project, op.raw["clip"], **changes)
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"set {', '.join(changes)} on clip {op.raw['clip']}", {"clip": op.raw["clip"], "track": track.id}


def _op_add_text_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    raw = op.raw
    track = _track(run, project, op)
    if track.kind == "audio":
        raise _fail(
            op, "plan.clip.text_on_audio_track", "a text clip can't go on an audio track", "use a video track", "track"
        )
    if track.magnetic:
        raise _fail(
            op,
            "plan.clip.text_on_main_track",
            "text goes on an overlay video track, not the main (magnetic) track",
            'put titles on a video track with "magnetic": false (add_track makes one)',
            "track",
        )
    clip_id = _new_id(run, project, op, "clip")
    options = {k: float(raw[k]) for k in ("start", "font_size", "x", "y") if k in raw}
    if "color" in raw:
        options["color"] = raw["color"]  # type: ignore[assignment]
    try:
        new, clip = timeline_ops.add_text_clip(project, track.id, raw["text"], float(raw["duration"]), **options)
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    new = _rename(new, "clip", clip.id, clip_id)
    return new, f"added title {clip_id} to track {track.id}", {"clip": clip_id, "track": track.id}


def _op_set_text_properties(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    changes = {k: op.raw[k] for k in ("text", "font_size", "color") if k in op.raw}
    try:
        new = timeline_ops.set_text_properties(project, op.raw["clip"], **changes)
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"set {', '.join(changes)} on title {op.raw['clip']}", {"clip": op.raw["clip"], "track": track.id}


def _op_add_track(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track_id = _new_id(run, project, op, "track")
    try:
        new, track = timeline_ops.add_track(project, op.raw["kind"], magnetic=bool(op.raw.get("magnetic", False)))
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    new = _rename(new, "track", track.id, track_id)
    return new, f"added {op.raw['kind']} track {track_id}", {"track": track_id}


def _op_remove_track(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track = _track(run, project, op)
    try:
        new = timeline_ops.remove_track(project, track.id)
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"removed track {track.id} with {len(track.clips)} clips", {"track": track.id}


def _op_move_track(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track = _track(run, project, op)
    try:
        new = timeline_ops.move_track(project, track.id, op.raw["to_index"])
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"moved track {track.id} to position {op.raw['to_index']}", {"track": track.id}


def _op_place_clip(run: _Run, project: Project, op: _Op) -> tuple[Project, str, dict[str, str]]:
    track, _ = _clip(project, op)
    dest_id = track.id
    if "track" in op.raw:
        dest_id = _track(run, project, op).id
    try:
        new = timeline_ops.place_clip(
            project,
            op.raw["clip"],
            start=None if op.raw.get("start") is None else float(op.raw["start"]),
            track_id=op.raw.get("track"),
        )
    except TimelineError as exc:
        raise _rule_failure(op, exc) from None
    return new, f"placed clip {op.raw['clip']} on track {dest_id}", {"clip": op.raw["clip"], "track": dest_id}


_HANDLERS: dict[str, Callable[[_Run, Project, _Op], tuple[Project, str, dict[str, str]]]] = {
    "add_clip": _op_add_clip,
    "trim_clip": _op_trim_clip,
    "move_clip": _op_move_clip,
    "remove_clip": _op_remove_clip,
    "set_transition": _op_set_transition,
    "set_clip_properties": _op_set_clip_properties,
    "add_text_clip": _op_add_text_clip,
    "set_text_properties": _op_set_text_properties,
    "add_track": _op_add_track,
    "remove_track": _op_remove_track,
    "move_track": _op_move_track,
    "place_clip": _op_place_clip,
}
assert set(_HANDLERS) == set(OPS)


def _run_ops(
    media: MediaStore,
    project: Project,
    ops: list[_Op],
    report: OpsReport,
    batch_hash: str,
    continue_on_error: bool,
) -> Project:
    """Apply `ops` in order to `project` (in memory; nothing is written). Fills `report.results`, `applied` and the
    errors, and returns the project as it stands after the ops that applied."""
    poisoned: set[tuple[str, str]] = set()  # ids a failed or skipped creating op would have made
    taken = {t.id for t in project.tracks} | {c.id for t in project.tracks for c in t.clips}
    taken |= {i for op in ops if (i := op.creates_id())}
    with assets.open_book(media) as book:  # read only: `assets.peek` and the table lookups never mark it dirty
        run = _Run(_Context(media, book, report), book, report, batch_hash, taken)
        for op in ops:
            spec = op.spec
            failed: list[PlanIssue] | None = None
            if not op.ok or spec is None:
                if not continue_on_error:
                    break  # the shape problems are all in the report already
                failed = []  # reported by the shape check
            else:
                blocked = next(
                    (
                        (kind, op.raw[name])
                        for name, kind in (("clip", "clip"), ("track", "track"))
                        if name in spec.refs and (kind, op.raw.get(name)) in poisoned
                    ),
                    None,
                )
                if blocked is not None:
                    failed = [
                        PlanIssue(
                            op.path,
                            "ops.skipped_dependency",
                            f"skipped: it uses {blocked[0]} {blocked[1]!r}, which an earlier failed op was to create",
                            "fix the earlier op first",
                        )
                    ]
                else:
                    try:
                        project, summary, ids = _HANDLERS[spec.name](run, project, op)
                    except _Failure as exc:
                        failed = exc.issues
                    except TimelineError as exc:  # a rule the table above does not know
                        failed = [PlanIssue(op.path, "ops.rule.violated", str(exc), "")]
                    else:
                        report.applied += 1
                        report.results.append({"index": op.index, "ok": True, "summary": summary, "ids": ids})
                        continue
            if failed:
                report.errors.extend(failed)
            first = failed[0] if failed else next((e for e in report.errors if _under(e.path, op.path)), None)
            report.results.append(
                {
                    "index": op.index,
                    "ok": False,
                    "summary": first.message if first else "invalid",
                    "ids": {},
                    "code": first.code if first else "",
                }
            )
            made = op.creates_id() if op.raw and isinstance(op.raw, dict) else None
            if made and spec is not None and spec.creates:
                poisoned.add((spec.creates, made))
            if not continue_on_error:
                break
    return project


# --- the API -----------------------------------------------------------------------------------------------------


def _prepare(
    ops_doc: Any, base_revision: int | None, continue_on_error: bool, dry_run: bool
) -> tuple[OpsReport, dict[str, Any], list[_Op], int | None, bool]:
    """The static half: version, top-level fields and the shape of every op. Returns the report, the document as a
    version 1 one, the parsed ops, the effective base revision and the effective `continue_on_error`."""
    report = OpsReport(dry_run=dry_run)
    if not isinstance(ops_doc, dict):
        report.error(
            "",
            "ops.document.not_object",
            "the ops document must be a JSON object",
            'send {"schema_version": 1, "project": ..., "ops": [...]}',
        )
        return report, {}, [], None, False
    if ops_doc.get("schema_version") is None:
        report.error(
            "/schema_version", "ops.schema_version.missing", "missing required field 'schema_version'",
            f'add "schema_version": {SCHEMA_VERSION}',
        )  # fmt: skip
        doc = {**ops_doc, "schema_version": SCHEMA_VERSION}
    else:
        try:
            doc = migrate_ops(ops_doc)
        except OpsVersionError as exc:
            hint = (
                "send an integer"
                if exc.code.endswith("invalid")
                else f"write the batch as schema_version {SCHEMA_VERSION}; GET /api/v1/schemas/edit-ops has the format"
            )
            report.error("/schema_version", exc.code, str(exc), hint)
            return report, {}, [], None, False
        except Exception as exc:  # noqa: BLE001 - a migration is code too: report it, don't crash the call
            report.error(
                "/schema_version", "ops.schema_version.migration_failed", f"migrating the ops failed: {exc}", ""
            )
            return report, {}, [], None, False
    try:
        report.plan_hash = canonical_hash(doc)
    except (TypeError, ValueError):
        report.error(
            "", "ops.document.not_json", "the ops document is not plain JSON data",
            "send only objects, arrays, strings, numbers, booleans and null",
        )  # fmt: skip
        return report, {}, [], None, False

    top = _check_object(doc, TOP_FIELDS, "", report, ns="ops")
    name = top.get("project")
    if name is not None:
        try:
            timeline.validate_name(name)
        except TimelineError:
            report.error(
                "/project", "ops.project.name_invalid", f"invalid project name {name!r}",
                "use a plain name: no slashes, not starting with a dot",
            )  # fmt: skip
    in_doc = top.get("base_revision")
    if base_revision is not None and in_doc is not None and base_revision != in_doc:
        report.error(
            "/base_revision", "ops.base_revision.conflict",
            f"the document says base_revision {in_doc} but the request says {base_revision}",
            "give the revision once, or make them agree",
        )  # fmt: skip
    effective_base = base_revision if base_revision is not None else in_doc
    effective_continue = bool(top.get("continue_on_error", False)) or continue_on_error
    if effective_continue and not dry_run:
        report.error(
            "/continue_on_error", "ops.continue_on_error.needs_dry_run",
            "continue_on_error only works with a dry run: a batch with a problem is never written",
            "use dry_run to collect every problem, fix them, then apply without it",
        )  # fmt: skip
    ops_raw = top.get("ops")
    parsed: list[_Op] = []
    if ops_raw is not None:
        if not ops_raw:
            report.error("/ops", "ops.ops.empty", "a batch needs at least one op", "add an op")
        elif len(ops_raw) > MAX_OPS:
            report.error(
                "/ops", "ops.ops.too_many", f"{len(ops_raw)} ops; at most {MAX_OPS} are allowed",
                "split the batch in several",
            )  # fmt: skip
        else:
            parsed = _check_ops(ops_raw, report)
    return report, doc, parsed, effective_base, effective_continue


def _summary(
    name: str, project: Project, revision: int, report: OpsReport, doc: dict[str, Any], dry_run: bool
) -> dict[str, Any]:
    return {
        "project": name,
        "revision": revision,
        "action": "edited",
        "dry_run": dry_run,
        "applied": report.applied,
        "ops": len(doc["ops"]),
        "clips": sum(len(t.clips) for t in project.tracks),
        "duration": project.duration,
        "tracks": [{"id": t.id, "kind": t.kind, "clips": len(t.clips), "end": t.end} for t in project.tracks],
        "results": report.results,
        "warnings": [w.to_dict() for w in report.warnings],
        "ops_hash": report.plan_hash,
        **({"meta": doc["meta"]} if "meta" in doc else {}),
    }


def _under(path: str, op_path: str) -> bool:
    return path == op_path or path.startswith(op_path + "/")


def _static_failed(report: OpsReport, parsed: list[_Op], continue_on_error: bool) -> bool:
    """Whether the shape check alone rules out going on: any problem outside the ops themselves, or an op with a
    problem unless `continue_on_error` wants the rest tried."""
    bad = [op.path for op in parsed if not op.ok]
    outside = [e for e in report.errors if not any(_under(e.path, b) for b in bad)]
    return bool(outside) or (bool(bad) and not continue_on_error)


def _dry_run(
    media: MediaStore, store: ProjectStore, ops_doc: Any, base_revision: int | None, continue_on_error: bool
) -> tuple[OpsReport, dict[str, Any], Project | None, int]:
    """A dry run: the report (with `timeline`), the document, the project after the ops that applied (None if the
    shape check failed first) and the predicted revision. Reads only."""
    report, doc, parsed, base, cont = _prepare(ops_doc, base_revision, continue_on_error, True)
    if not doc or _static_failed(report, parsed, cont):
        return report, doc, None, 0
    name = doc["project"]
    loaded = store.load_existing_timeline(name)
    store_module.check_base_revision("timeline project", name, loaded.revision, base)
    project = _run_ops(media, loaded, parsed, report, report.plan_hash or "", cont)
    revision = loaded.revision + 1
    report.timeline = _view(media, name, project, revision)
    return report, doc, project, revision


def validate_ops(
    media: MediaStore,
    store: ProjectStore,
    ops_doc: Any,
    *,
    base_revision: int | None = None,
    continue_on_error: bool = False,
) -> OpsReport:
    """The report a dry run gives, valid or not: every shape problem, and what applying the ops to the stored
    project would do. Writes nothing. `ProjectNotFoundError` / `RevisionConflictError` as for `apply_ops`."""
    return _dry_run(media, store, ops_doc, base_revision, continue_on_error)[0]


def _view(media: MediaStore, name: str, project: Project, revision: int) -> dict[str, Any]:
    return timeline_service.describe(media, name, replace(project, revision=revision), read_only=True)


def apply_ops(
    media: MediaStore,
    store: ProjectStore,
    ops_doc: Any,
    *,
    dry_run: bool = False,
    base_revision: int | None = None,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Apply a batch of operations to an existing timeline project, atomically: every op or none, saved with ONE
    `update_timeline` (under the store's write lock, so no other edit slips in between), or, with `dry_run`, only
    report what would happen and write nothing at all.

    `base_revision` (an If-Match header, `--base-revision`, the MCP argument; or in the document, and the two must
    agree) is the revision the batch was written against: `RevisionConflictError` if the project has moved on,
    nothing written. `ProjectNotFoundError` if there is no such project. `OpsInvalidError` (carrying the full
    report) if any op is invalid or the project refuses it. The first op the project refuses stops the batch,
    unless `continue_on_error` (dry run only) goes on and collects every independent problem.

    A dry run's `revision` is a prediction (the current one plus one), not a reservation."""
    if dry_run:
        report, doc, project, revision = _dry_run(media, store, ops_doc, base_revision, continue_on_error)
        if project is None or not report.ok:
            raise OpsInvalidError(report)
        return {**_summary(doc["project"], project, revision, report, doc, True), "timeline": report.timeline}
    report, doc, parsed, base, cont = _prepare(ops_doc, base_revision, continue_on_error, False)
    if not doc or _static_failed(report, parsed, cont):
        raise OpsInvalidError(report)
    name = doc["project"]
    batch_hash = report.plan_hash or ""

    def change(stored: Project) -> Project:
        linked = assets.link_project(media, stored)  # like every single edit: a version 1 project is saved as 2
        done = _run_ops(media, linked, parsed, report, batch_hash, False)
        if not report.ok:
            raise OpsInvalidError(report)  # nothing is written: update_timeline saves only what `change` returns
        return done

    saved = store.update_timeline(name, change, base)
    return _summary(name, saved, saved.revision, report, doc, False)
