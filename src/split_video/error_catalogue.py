"""The error catalogue: one stable code, message and suggested fix for every way a request can fail.

The engine keeps raising its own exceptions (`ProjectError`, `TimelineError`, `FrameError`, ...). `describe(exc)` turns
any of them into an `ErrorInfo`, the one payload every front door renders the same way:

    {"code": "clip.overlap", "message": "...", "fix": "...", "details": {...}}

- HTTP: the response body of the failing route carries `code`, `message`, `fix` and `details` next to the `detail`
  string it always had (`editor/errors.py`).
- CLI: `Error:` and `Fix:` lines on stderr and, with `--json` (or `--json-errors`), this payload on stdout, with a
  non-zero exit code (`cli_errors.py`).
- MCP: a tool error (`isError`) whose text is this payload as JSON (`mcp_server.py`).
- Render queue / jobs: a failed job's `error` is `{code, message, fix}`.

`REGISTRY` maps each code to its `ErrorSpec` (meaning, fix template, HTTP status, CLI exit code); docs/errors.md is
generated from it (`scripts/export_error_catalogue.py`, `make errors`) and a test fails when the two drift or a code
is undocumented. The per-issue `plan.*` and `ops.*` codes of edit plans and edit-op batches (docs/edit-plan.md,
docs/edit-ops.md) are not repeated here: a failing plan or batch is the single code `plan.invalid` / `ops.invalid`
whose body keeps its `errors` list, each with its own `code`, `path`, `message` and `hint`.

Every fix names a concrete next action: a tool or command to call, a value to change, or the valid choices when there
is an enumerable set. Nothing here touches ffmpeg or the disk.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    meaning: str  # one sentence, for the catalogue
    fix: str  # template; `{name}` placeholders are filled from `details`
    status: int = 422  # the HTTP status the API answers with
    exit_code: int = 1  # the CLI exit code
    # Fix templates for a context the default one does not fit (`describe` picks one by what failed, e.g.
    # "highlights" for a `suggest_highlights` preset); docs/errors.md lists them after the default.
    variants: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    fix: str
    details: dict[str, Any] = field(default_factory=dict)
    # Keys older clients already read from this error (`error`, `errors`, `current_revision`, ...): merged at the top
    # level by `to_dict`, so the structured payload is purely additive.
    legacy: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> ErrorSpec:
        return REGISTRY[self.code]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {**self.legacy, "code": self.code, "message": self.message, "fix": self.fix}
        if self.details:
            out["details"] = self.details
        return out


REGISTRY: dict[str, ErrorSpec] = {}


def _register(
    code: str,
    meaning: str,
    fix: str,
    status: int = 422,
    exit_code: int = 1,
    variants: dict[str, str] | None = None,
) -> None:
    assert code not in REGISTRY, code
    REGISTRY[code] = ErrorSpec(code, meaning, fix, status, exit_code, variants or {})


# --- times and ranges ---------------------------------------------------------------------------------------------

_register(
    "time.invalid",
    "A time could not be read as a number of seconds or a clock time.",
    "Give the time as seconds (83.5) or as a clock time (1:23.5 or 1:02:03); {value} is neither.",
)
_register(
    "time.negative",
    "A time, start or in point is below 0.",
    "Use 0 or a later time: the earliest point of any media is 0 s.",
)
_register(
    "time.out_of_range",
    "A time lies outside the media or the editable span.",
    "Use a time within {range}. The media's duration is in `list_assets` (CLI `split-video library list DIR`, "
    "HTTP GET /api/v1/assets).",
)
_register(
    "time.range_inverted",
    "A range ends at or before the point where it starts.",
    "Make the end later than the start (start < end, both in seconds) and send the range again.",
)

# --- split projects: segments and splits --------------------------------------------------------------------------

_register(
    "segments.overlap",
    "Two segments of a split project overlap.",
    "Make each segment start where the previous one ends (no overlap), or remove one with `remove_split`; "
    "`list_segments` (CLI `split-video segments VIDEO`) shows the current boundaries.",
)
_register(
    "segments.too_close",
    "A split would sit on top of an existing split.",
    "Choose a time at least a second away from every existing split, or move the existing one with `move_split`; "
    "`list_segments` shows where they are.",
)
_register(
    "segments.not_found",
    "A segment or split number does not exist.",
    "Use a number from 1 up to the count `list_segments` (CLI `split-video segments VIDEO`) reports; split N is the "
    "cut between segments N and N+1.",
)
_register(
    "segments.invalid",
    "The list of segments breaks a rule (empty, unsorted, duplicate export names).",
    "Send a non-empty list sorted by start time, with unique export names; `list_segments` shows a valid one.",
)

_register(
    "segments.main_track_edited",
    "The main track holds more than the recording's own clips (another recording or a title), so splits cannot be "
    "rebuilt from segments.",
    "Edit this timeline with the compose tools (`show_timeline`, `apply_ops`, CLI `split-video compose`), or remove "
    "the clips that were added; splitting only works while the main track holds the recording itself.",
)

# --- timelines: clips, tracks, projects ---------------------------------------------------------------------------

_register(
    "clip.overlap",
    "A clip would overlap another clip on a track that places clips by start time.",
    "Place the clip at or after the end of the previous clip on that track, or on "
    "another track; `show_timeline` (CLI `split-video compose show NAME`) lists every clip's start and end.",
)
_register(
    "clip.too_short",
    "A clip would be shorter than the minimum clip length.",
    "Make out_point at least a tenth of a second after in_point (a clip must be at least 0.1 s long).",
)
_register(
    "clip.not_found",
    "No clip or track with that number or id exists.",
    "Use a clip number from 1 (or an id) that `show_timeline` (CLI `split-video compose show NAME`) lists; "
    "numbers are per track.",
    status=404,
)
_register(
    "timeline.rule_violated",
    "The edit breaks one of the timeline's editing rules (crossfades, tracks, crop, text, ...).",
    "Read the message for the rule, inspect the tracks with `show_timeline` (CLI `split-video compose show NAME`), "
    "then change the offending value; `dry_run` on `apply_ops` checks an edit without writing.",
)
_register(
    "timeline.invalid",
    "An edit to a timeline was refused (bad argument for the timeline).",
    "Read the message, then correct the argument; `show_timeline` (CLI `split-video compose show NAME`) shows the "
    "timeline as it is.",
)
_register(
    "timeline.not_found",
    "No timeline project with that name exists.",
    "Use a name `list_timelines` (CLI `split-video compose list`, HTTP GET /api/v1/compose/projects) returns, or "
    "create it with `create_timeline` (or `apply_plan` in mode create).",
    status=404,
)
_register(
    "timeline.exists",
    "A timeline project with that name already exists.",
    "Pick another name, or replace the project with `apply_plan` in mode replace and `base_revision` set to its "
    "current revision (`show_timeline`).",
    status=409,
)
_register(
    "timeline.name_invalid",
    "The project name is not usable.",
    "Use a plain name of letters, digits, dashes and underscores, with no slashes or extension.",
)

# --- assets and media ---------------------------------------------------------------------------------------------

_register(
    "asset.not_found",
    "No asset with that id is in the workspace's asset table.",
    "List the ids with `list_assets` (CLI `split-video library list DIR`, HTTP GET /api/v1/assets); a new file needs "
    "`scan_library` (CLI `split-video library scan DIR`) first. Ids are `a_` plus 16 hex digits.",
    status=404,
)
_register(
    "source.not_found",
    "The media file does not exist under the workspace root.",
    "Use a path relative to the root that `list_videos` (CLI `split-video library list DIR`) shows, or run "
    "`scan_library` if the file was just added.",
    status=404,
)
_register(
    "source.outside_root",
    "A path resolves outside the workspace root.",
    "Use a path inside the root (no `..`, no absolute path outside it), or start the server or CLI with `--root` "
    "set to the folder that holds the file.",
    status=400,
)
_register(
    "media.unreadable",
    "ffprobe could not read the file as media.",
    "Check the file plays (`ffprobe FILE`) and is not still being written; re-export or re-download it if it is "
    "damaged.",
)
_register(
    "media.audio_only",
    "The operation needs a picture and the asset is audio-only.",
    "Pick an asset with a video stream (`list_assets` shows has_video), or use an audio-only operation.",
)

# --- imports --------------------------------------------------------------------------------------------------------

_register(
    "upload.invalid_name",
    "An uploaded file has no usable name (empty, hidden, or too long).",
    "Rename the file to a plain name such as `gig.mp4`, then import it again.",
    status=400,
)
_register(
    "upload.unsupported_type",
    "An uploaded file does not have a supported video or audio extension.",
    "Use a video or audio file with one of these extensions: {supported}.",
    status=415,
)
_register(
    "upload.empty",
    "An upload carried no bytes.",
    "Make sure the file is not empty (it may still be recording or copying), then import it again.",
    status=400,
)

# --- revisions ----------------------------------------------------------------------------------------------------

_register(
    "revision.conflict",
    "The project changed since the revision the write was based on; nothing was written.",
    "Reload the project ({reload}), re-apply your change on top of it, and retry with base_revision="
    "{current_revision} (CLI `--base-revision {current_revision}`, HTTP `If-Match: {current_revision}`).",
    status=409,
    exit_code=3,
)

# --- render options -----------------------------------------------------------------------------------------------

_register(
    "preset.unknown",
    "The export or highlight preset name is not one of the known presets.",
    "Use one of: {choices}. `list_export_presets` (CLI `split-video presets`, HTTP GET /api/v1/presets) describes them.",
    variants={
        "highlights": "Use one of: {choices} (the `preset` of `suggest_highlights`, CLI `split-video highlights "
        "--preset`); docs/highlights.md says what each weighs. These are not the export presets of "
        "`list_export_presets`.",
    },
)
_register(
    "fit.unknown",
    "The fit is not one of the known framing modes.",
    "Use one of: {choices}. `letterbox` keeps the whole frame with bars, `crop` fills the frame.",
)
_register(
    "caption.style_unknown",
    "The caption style name is not one of the known styles.",
    "Use one of: {choices}. `list_caption_styles` (CLI `split-video caption-styles`, HTTP GET /api/v1/caption-styles) describes them.",
)
_register(
    "caption.invalid",
    "A caption option (range, position, colour, tracks) is not usable.",
    "Fix the option named in the message: positions are top, middle or bottom; colours look like #RRGGBB; a range "
    "is {start, end} in timeline seconds with start < end; tracks are 1-based numbers `show_timeline` lists.",
)
_register(
    "render.output_exists",
    "The render's output file already exists.",
    "Pass overwrite=true (CLI `--overwrite`) to replace it, or choose another `output` name.",
    status=409,
)
_register(
    "render.no_clips",
    "The timeline has no clips to render.",
    "Add clips first with `add_clip_to_timeline` (CLI `split-video compose add-clip NAME FILE`) or build the "
    "timeline with `apply_plan`.",
)
_register(
    "render.request_invalid",
    "A render request, or a batch of render requests, is malformed.",
    "Send a non-empty list of at most 50 render objects, each with a `project` (a name `list_timelines` returns) and "
    "its own `output` name; the other options are those of `render_timeline`.",
)
_register(
    "export.invalid",
    "The export request is unusable: an unknown mode, or the argument its mode needs is missing.",
    "Use mode segments (with `video`), timeline (with `project`, or `video` for a recording's own timeline) or "
    "highlights (with `timelines`, the names of the accepted highlights' timelines).",
)
_register(
    "render.size_invalid",
    "The output size is not usable.",
    "Give both width and height (or a preset), as even whole numbers within limits, e.g. `1080x1920`.",
)

# --- external programs and optional extras ------------------------------------------------------------------------

_register(
    "ffmpeg.missing",
    "ffmpeg or ffprobe was not found on PATH.",
    "Install ffmpeg (`sudo apt install ffmpeg` or `brew install ffmpeg`) so `ffmpeg -version` works, or use the "
    "Docker image, which bundles it.",
    status=501,
)
_register(
    "ffmpeg.failed",
    "ffmpeg exited with an error while reading or writing media.",
    "Read the ffmpeg output in the message; check the source plays (`ffprobe FILE`) and the disk has room, then "
    "retry. Do not retry blindly: the same input fails the same way.",
    status=500,
)
_register(
    "extra.transcription_missing",
    "Transcription needs the optional faster-whisper package, which is not installed.",
    "Install it with `uv sync --extra transcribe` (a source checkout) or `pip install 'split-video[transcribe]'`, "
    "or use the Docker image built with `--build-arg WITH_TRANSCRIPTION=1`.",
    status=501,
    exit_code=4,
)
_register(
    "extra.ocr_missing",
    "On-screen text needs the Tesseract OCR program, which is not installed.",
    "Install it with `sudo apt install tesseract-ocr tesseract-ocr-eng` or `brew install tesseract`, or use the "
    "Docker image built with `--build-arg WITH_OCR=1`.",
    status=501,
    exit_code=4,
)
_register(
    "model.missing",
    "The audio classification model file is not installed.",
    "Run `make fetch-model` (source checkout) or use the Docker image, which fetches the model at build time.",
    status=501,
)

# --- derived data that has not been made yet ----------------------------------------------------------------------

_register(
    "transcript.missing",
    "The asset has no transcript yet (never made, or the file changed since).",
    "Make one with `transcribe_asset` (CLI `split-video transcribe TARGET`, HTTP POST /api/v1/assets/{{id}}/transcribe), "
    "then ask again.",
    status=404,
)
_register(
    "ocr.missing",
    "The asset has no on-screen text yet (never made, or the file changed since).",
    "Make it with `ocr_asset` (CLI `split-video ocr TARGET`, HTTP POST /api/v1/assets/{{id}}/ocr), then ask again.",
    status=404,
)
_register(
    "analysis.missing",
    "The asset has nothing to search or score yet (no transcript, on-screen text or scene signals).",
    "Run the step the message names first: `transcribe_asset`, `ocr_asset` or `analyze_scenes` (each also a CLI "
    "command and an HTTP POST under /api/v1/assets/{{id}}/).",
    status=404,
)
_register(
    "job.unknown",
    "No job or render batch with that id is known to this server.",
    "List what exists with `render_queue_status` (CLI `split-video compose render-queue`, HTTP GET /api/v1/jobs); ids do "
    "not survive a server restart.",
    status=404,
)
_register(
    "job.failed",
    "The background job failed.",
    "Read the message for the cause, fix it, and submit the job again.",
    status=500,
)

# --- edit plans and op batches ------------------------------------------------------------------------------------

_register(
    "plan.invalid",
    "The edit plan has problems; nothing was written.",
    "Fix every entry of `errors` (each has a `path` into the plan, a `code` and a `hint`) and send the plan again; "
    "`dry_run` validates without writing. The codes are in docs/edit-plan.md.",
)
_register(
    "ops.invalid",
    "The edit-op batch has problems; nothing was written.",
    "Fix every entry of `errors` (each has a `path` like /ops/2/clip, a `code` and a `hint`) and send the batch "
    "again; `dry_run` checks it without writing. The codes are in docs/edit-ops.md.",
)

# --- everything else ----------------------------------------------------------------------------------------------

_register(
    "request.invalid",
    "An argument is missing, of the wrong type or outside its allowed values.",
    "Compare the arguments with the tool's input schema (MCP), `--help` (CLI) or openapi.json (HTTP) and send "
    "corrected values.",
)
_register(
    "error.internal",
    "An unexpected failure inside the engine.",
    "Retry once. If it fails again, report it: send the message and the request that caused it, since it is a bug "
    "rather than a mistake in the request.",
    status=500,
)


def choices_of(message: str) -> str:
    """The valid values the engine put in `message` ("valid presets: a, b"), or a pointer to where they are listed."""
    m = re.search(r"valid [a-z]+(?: are|:) (?P<c>.+)$", message)
    return m.group("c").strip() if m else "the values the list tool for this option returns"


# --- classification -----------------------------------------------------------------------------------------------

_Details = dict[str, Any]
_Rule = tuple["re.Pattern[str]", str, "Callable[[re.Match[str]], _Details] | None"]


def _num(text: str) -> float:
    return float(text)


def _out_of_range(m: re.Match[str]) -> _Details:
    groups = m.groupdict()
    if groups.get("lo") is not None:
        lo, hi = groups["lo"], groups["hi"]
        return {"range": f"{lo} s to {hi} s", "min_seconds": _num(lo), "max_seconds": _num(hi)}
    if groups.get("limit") is not None:
        return {"range": f"0 s to {groups['limit']} s", "min_seconds": 0.0, "max_seconds": _num(groups["limit"])}
    return {"range": "0 s to the media's duration"}


# Message patterns for the failures that share a code across many modules. Tried in order against any ValueError whose
# class has no code of its own; the first match wins.
_TIME_RULES: list[_Rule] = [
    (re.compile(r"not a time: (?P<value>.+)$"), "time.invalid", lambda m: {"value": m.group("value")}),
    (
        re.compile(r"must be after start|end must be after|needs 0 <= start < end|end \(.*\) must be after"),
        "time.range_inverted",
        None,
    ),
    (
        re.compile(
            r"(?:past|beyond) the end of the (?:asset|source)[^\d]*(?P<limit>[\d.]+) ?s"
            r"|(?:outside|beyond) the editable span \((?P<lo>[\d.]+)s to (?P<hi>[\d.]+)s\)"
            r"|(?:past|beyond) the end"
        ),
        "time.out_of_range",
        _out_of_range,
    ),
    (
        re.compile(r"\b(?:time|start|in point|seconds)\b[^.]{0,30}(?:negative|0 or more)|can't start before 0"),
        "time.negative",
        None,
    ),
]

_OTHER_RULES: list[_Rule] = [
    (re.compile(r"segments must not overlap"), "segments.overlap", None),
    (re.compile(r"too close to the existing split"), "segments.too_close", None),
    (re.compile(r"^(?:split|segment) \d+ doesn't exist"), "segments.not_found", None),
    (
        re.compile(r"segments list must not be empty|segments must be sorted|export names must be unique"),
        "segments.invalid",
        None,
    ),
    (re.compile(r"would overlap another clip on the track \(until (?P<until>[\d.]+)s\)"), "clip.overlap", None),
    (re.compile(r"a clip must be at least"), "clip.too_short", None),
    (re.compile(r"invalid project name"), "timeline.name_invalid", None),
    (re.compile(r"^no (?:clip|track) "), "clip.not_found", None),
    (re.compile(r"unknown preset"), "preset.unknown", lambda m: {}),
    (re.compile(r"unknown fit"), "fit.unknown", lambda m: {}),
    (re.compile(r"unknown caption style"), "caption.style_unknown", lambda m: {}),
    (re.compile(r"already exists$"), "render.output_exists", None),
    (re.compile(r"has no clips to render|no clips to render"), "render.no_clips", None),
    (re.compile(r"has no (?:picture|video stream)|is audio-only"), "media.audio_only", None),
    (
        re.compile(r"size must look like|give both width and height|must be an even whole number"),
        "render.size_invalid",
        None,
    ),
]


_cached: dict[str, tuple[type, ...]] = {}


def _roles() -> dict[str, tuple[type, ...]]:
    """The engine's exception classes by role, imported on first use (this module sits below all of them)."""
    if _cached:
        return _cached
    from split_video import (
        captions,
        edit_ops,
        edit_plan,
        highlights,
        jobs,
        ocr,
        presets,
        render_queue,
        search,
        timeline,
        transcribe,
        transcript_search,
    )
    from split_video import frames as frames_module
    from split_video import media_store as ms
    from split_video import project as project_module
    from split_video.editor.classify import ModelNotFoundError
    from split_video.errors import RevisionConflictError
    from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError

    _cached.update(
        {
            "conflict": (RevisionConflictError,),
            "plan": (edit_plan.PlanInvalidError,),
            "ops": (edit_ops.OpsInvalidError,),
            "transcription_missing": (transcribe.TranscriptionUnavailableError,),
            "ocr_missing": (ocr.OcrUnavailableError,),
            "model_missing": (ModelNotFoundError,),
            "ffmpeg_missing": (FfmpegNotFoundError,),
            "ffmpeg_failed": (ExtractError, transcribe.TranscriptionError, ocr.OcrError),
            "unreadable": (ProbeError,),
            "outside_root": (timeline.OutsideRootError, ms.MediaOutsideRootError),
            "source_missing": (timeline.SourceNotFoundError, ms.MediaNotFoundError),
            "asset_missing": (timeline.AssetNotFoundError,),
            "timeline_missing": (timeline.ProjectNotFoundError,),
            "timeline_exists": (timeline.ProjectExistsError,),
            "output_exists": (timeline.OutputExistsError, project_module.ExportConflictError),
            "transcript_missing": (transcript_search.TranscriptNotFoundError,),
            "ocr_data_missing": (ocr.OcrNotFoundError,),
            "analysis_missing": (search.SearchNotFoundError, highlights.NoSignalsError),
            "unknown_job": (jobs.UnknownJobError, render_queue.UnknownBatchError),
            "job_failed": (jobs.JobFailedError,),
            "render_request": (render_queue.RenderQueueError, render_queue.RenderRequestError),
            "preset": (presets.PresetError,),
            "highlights": (highlights.HighlightsError,),
            "caption": (captions.CaptionError,),
            "audio_only": (frames_module.FrameAudioOnlyError,),
            "timeline": (timeline.TimelineError,),
        }
    )
    return _cached


def _is(exc: BaseException, role: str) -> bool:
    return isinstance(exc, _roles()[role])


def _match(rules: list[_Rule], message: str) -> tuple[str, _Details] | None:
    for pattern, code, extract in rules:
        m = pattern.search(message)
        if m:
            return code, (extract(m) if extract else {})
    return None


# Exception role -> code, for the failures whose class alone says what went wrong.
_BY_ROLE = (
    ("plan", "plan.invalid"),
    ("ops", "ops.invalid"),
    ("transcription_missing", "extra.transcription_missing"),
    ("ocr_missing", "extra.ocr_missing"),
    ("model_missing", "model.missing"),
    ("ffmpeg_missing", "ffmpeg.missing"),
    ("unreadable", "media.unreadable"),
    ("outside_root", "source.outside_root"),
    ("source_missing", "source.not_found"),
    ("asset_missing", "asset.not_found"),
    ("timeline_missing", "timeline.not_found"),
    ("timeline_exists", "timeline.exists"),
    ("output_exists", "render.output_exists"),
    ("transcript_missing", "transcript.missing"),
    ("ocr_data_missing", "ocr.missing"),
    ("analysis_missing", "analysis.missing"),
    ("unknown_job", "job.unknown"),
    ("job_failed", "job.failed"),
    ("audio_only", "media.audio_only"),
    ("ffmpeg_failed", "ffmpeg.failed"),
)

# `RuleViolation.reason` -> code; any other reason is `timeline.rule_violated`.
_BY_REASON = {
    "in_negative": "time.negative",
    "start_negative": "time.negative",
    "out_past_end": "time.out_of_range",
    "too_short": "clip.too_short",
    "overlap": "clip.overlap",
}


def classify(exc: BaseException) -> tuple[str, _Details]:
    """The registry code for `exc` and the details worth keeping. Never raises; unknown failures are `error.internal`."""
    message = str(exc)
    own = getattr(exc, "error_code", None)
    if own in REGISTRY:  # an exception may name its own code
        return own, dict(getattr(exc, "details", None) or {})
    if _is(exc, "conflict"):
        return "revision.conflict", {
            "project": exc.project,  # type: ignore[attr-defined]
            "kind": exc.kind,  # type: ignore[attr-defined]
            "current_revision": exc.current,  # type: ignore[attr-defined]
            "base_revision": exc.base,  # type: ignore[attr-defined]
        }
    for role, code in _BY_ROLE:
        if _is(exc, role):
            return code, {}
    reason = getattr(exc, "reason", None)  # a `RuleViolation` knows which rule it broke
    if isinstance(reason, str):
        code = _BY_REASON.get(reason, "timeline.rule_violated")
        hit = _match(_TIME_RULES + _OTHER_RULES, message)
        return code, hit[1] if hit and hit[0] == code else {}
    if isinstance(exc, ValueError):
        hit = _match(_TIME_RULES, message) or _match(_OTHER_RULES, message)
        if hit:
            return hit
    if _is(exc, "preset"):
        return ("preset.unknown" if message.startswith("unknown preset") else "fit.unknown"), {}
    if _is(exc, "caption"):
        return "caption.invalid", {}
    if _is(exc, "render_request"):
        return "render.request_invalid", {}
    if _is(exc, "timeline"):
        return "timeline.invalid", {}
    if isinstance(exc, ValueError):
        return "request.invalid", {}
    return "error.internal", {}


class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return f"<{key}>"


def describe(exc: BaseException) -> ErrorInfo:
    """The structured payload for `exc`: stable code, its message, a suggested fix, details and legacy keys."""
    code, details = classify(exc)
    message = str(exc) or type(exc).__name__
    legacy: dict[str, Any] = {}
    extra_fix = ""
    if code in ("preset.unknown", "fit.unknown", "caption.style_unknown"):
        details = {**details, "choices": choices_of(message)}
    elif code == "plan.invalid":
        report = exc.report  # type: ignore[attr-defined]
        count = len(report.errors)
        message = f"the plan has {count} problem{'s' if count != 1 else ''}; nothing was written"
        legacy = {"error": "plan_invalid", "detail": message, **report.to_dict()}
    elif code == "ops.invalid":
        from split_video import edit_ops

        legacy = edit_ops.invalid_body(exc.report)  # type: ignore[attr-defined]
        message = legacy["detail"]
    elif code == "revision.conflict":
        legacy = {
            "error": "revision_conflict",
            "current_revision": details["current_revision"],
            "base_revision": details["base_revision"],
        }
    if code in ("plan.invalid", "ops.invalid"):
        first = next(iter(exc.report.errors), None)  # type: ignore[attr-defined]
        if first is not None and first.hint:
            extra_fix = f" First problem, {first.path or '/'} ({first.code}): {first.hint}"
    fill = _Fill(details)
    if code == "revision.conflict":
        if details["kind"] == "timeline project":
            fill["reload"] = f"`show_timeline`, CLI `split-video compose show {details['project']}`"
        else:
            fill["reload"] = f"`list_segments`, CLI `split-video segments {details['project']}`"
    template = REGISTRY[code].fix
    if code == "preset.unknown" and _is(exc, "highlights"):
        template = REGISTRY[code].variants["highlights"]
    fix = template.format_map(fill) + extra_fix
    return ErrorInfo(code, message, fix, details, legacy)


def argument_error(subject: str, problems: list[dict[str, str]]) -> ErrorInfo:
    """`request.invalid` for arguments that failed validation before the engine ran (an MCP tool's input schema, an
    HTTP query or body). `problems` are `{"argument", "problem"}` (plus optional `type`); the first one goes into the
    fix, all of them into `details["errors"]`."""
    first = problems[0] if problems else {"argument": "?", "problem": "invalid"}
    listed = "; ".join(f"{p['argument']}: {p['problem']}" for p in problems[:5])
    more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
    message = f"invalid arguments for {subject}: {listed}{more}"
    fix = REGISTRY["request.invalid"].fix + (f" First problem, `{first['argument']}`: {first['problem']}.")
    return ErrorInfo("request.invalid", message, fix, {"errors": problems})


def error_dict(exc: BaseException) -> dict[str, Any]:
    return describe(exc).to_dict()


# --- documentation ------------------------------------------------------------------------------------------------

DOC_HEADER = """\
# Error catalogue

<!-- Generated from src/split_video/error_catalogue.py by scripts/export_error_catalogue.py; edit the registry, then
run `make errors`. -->

Every failure of the CLI, the MCP server and the HTTP API carries the same four fields:

```json
{
  "code": "clip.overlap",
  "message": "clip would overlap another clip on the track (until 12s)",
  "fix": "Place the clip at or after the end of the previous clip ...",
  "details": {}
}
```

- `code` is stable and namespaced (`time.*`, `segments.*`, `clip.*`, `asset.*`, ...). Branch on it, not on `message`.
- `message` says what happened. `fix` says what to do next: a tool or command to call, a value to change, or the
  valid choices. `details` is present when there is structured data (a revision number, a range, the valid choices).
- **CLI**: `Error:` and `Fix:` lines on stderr and a non-zero exit code (`1`; `2` for a time argument that cannot be
  parsed; `3` for `revision.conflict`; `4` for a missing optional extra). With `--json` on the command, or `--json-errors` before the command name, the payload is
  also printed to stdout as JSON.
- **MCP**: the tool result has `isError: true`; its text is `Error executing tool NAME: ` followed by the payload as
  JSON (everything from the first `{`).
- **HTTP**: the response body is `{"detail": <message>, "code", "message", "fix", "details"}`; the status is the one in
  the table (routes keep the status they always had). For FastAPI's own argument check (`request.invalid`) `detail` stays
  FastAPI's list of problems, and `details.errors` has one `{argument, in, problem, type}` per problem.
- **Render queue and jobs**: a failed render's `error` is `{code, message, fix}`.
- A rejected edit plan or op batch is `plan.invalid` / `ops.invalid`; the body also keeps its `errors` list, each
  entry with its own `path`, `code` (`plan.*` / `ops.*`, see [edit-plan.md](edit-plan.md) and
  [edit-ops.md](edit-ops.md)) and `hint`.

| Code | HTTP | CLI exit | Meaning | Suggested fix |
| --- | --- | --- | --- | --- |
"""


def render_markdown() -> str:
    rows = []
    for spec in REGISTRY.values():
        fix = spec.fix
        for context, variant in spec.variants.items():
            fix += f" For a {context} preset: {variant}"
        fix = fix.replace("{{", "{").replace("}}", "}").replace("|", "\\|")
        rows.append(f"| `{spec.code}` | {spec.status} | {spec.exit_code} | {spec.meaning} | {fix} |")
    return DOC_HEADER + "\n".join(rows) + "\n"
