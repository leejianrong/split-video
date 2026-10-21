"""The error catalogue (SPL-55): every code is registered and documented, the doc is generated from the registry, the
common mistakes raised by the real engine (in-memory stores) get the right code and a concrete fix, and the payload
is the same dict every surface renders."""

import json
import re
from pathlib import Path

import pytest

from split_video import captions, edit_ops, edit_plan, error_catalogue, presets, project, timeline_service
from split_video import timeline_ops as ops
from split_video.error_catalogue import REGISTRY, describe, error_dict
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, MediaInfo, ProbeError
from split_video.frames import FrameAudioOnlyError, FrameTimeError
from split_video.jobs import JobFailedError, UnknownJobError
from split_video.media_store import InMemoryMediaStore, MediaNotFoundError, MediaOutsideRootError
from split_video.ocr import OcrNotFoundError, OcrUnavailableError
from split_video.render_queue import RenderQueueError, RenderRequestError, UnknownBatchError
from split_video.search import SearchNotFoundError, SearchQueryError
from split_video.store import InMemoryProjectStore, SavedSegment
from split_video.timefmt import parse_time
from split_video.timeline import (
    AssetNotFoundError,
    OutputExistsError,
    OutsideRootError,
    ProjectExistsError,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
)
from split_video.transcribe import TranscriptionError, TranscriptionUnavailableError
from split_video.transcript_search import TranscriptNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[2]
VIDEO = MediaInfo(duration=30.0, width=320, height=240, has_audio=True)


def raised(fn, *args, **kwargs) -> BaseException:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("did not raise")


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("a.mp4", b"a-bytes", info=VIDEO)
    return store


@pytest.fixture
def store(media):
    store = InMemoryProjectStore()
    timeline_service.create_project(store, "reel")
    timeline_service.add_clip(store, media, "reel", "a.mp4", out_point=10)
    return store


# --- the registry and its documentation ---------------------------------------------------------------------------------


def test_every_code_is_namespaced_and_has_a_meaning_a_fix_and_valid_statuses():
    assert len(REGISTRY) >= 40
    for code, spec in REGISTRY.items():
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", code), code
        assert spec.code == code and spec.meaning.endswith(".") and len(spec.fix) > 40, code
        assert 400 <= spec.status <= 599 and spec.exit_code in (1, 3, 4), code
        # A fix names something to do: a tool or command in backticks, a choice list, or a value to change.
        assert (
            "`" in spec.fix
            or "{choices}" in spec.fix
            or re.search(r"\b(use|give|make|pick|send|fix)\b", spec.fix, re.IGNORECASE)
        )


def test_the_catalogue_doc_is_generated_from_the_registry():
    doc = (REPO_ROOT / "docs" / "errors.md").read_text()
    assert doc == error_catalogue.render_markdown(), "docs/errors.md is out of date; run `make errors`"
    missing = [code for code in REGISTRY if f"| `{code}` |" not in doc]
    assert not missing, f"codes missing from docs/errors.md: {missing}"


def test_every_code_used_in_the_source_is_registered():
    """A code written in a `REGISTRY` lookup or an `error_code` attribute anywhere must exist."""
    src = (REPO_ROOT / "src" / "split_video").rglob("*.py")
    used = set()
    for path in src:
        used |= set(re.findall(r'error_code = "([a-z_]+\.[a-z_]+)"', path.read_text()))
    assert used <= set(REGISTRY)


# One exception per code, to prove each is reachable and carries a fix with no unfilled placeholder.
def _samples() -> dict[str, BaseException]:
    segments = [SavedSegment(0.0, 10.0), SavedSegment(10.0, 20.0)]
    return {
        "time.invalid": raised(parse_time, "abc"),
        "time.negative": raised(parse_time, "-3"),
        "time.out_of_range": raised(project.add_split, segments, 99.0),
        "time.range_inverted": raised(project.validate_pairs, [(5.0, 3.0)]),
        "segments.overlap": raised(project.validate_pairs, [(0.0, 5.0), (4.0, 9.0)]),
        "segments.too_close": raised(project.add_split, segments, 10.0),
        "segments.not_found": raised(project.remove_split, segments, 7),
        "segments.invalid": raised(project.validate_pairs, []),
        "clip.overlap": ops.RuleViolation("overlap", "clip would overlap another clip on the track (until 4s)"),
        "clip.too_short": ops.RuleViolation("too_short", "a clip must be at least 0.1s long"),
        "clip.not_found": TimelineError("no clip 9; the track has 2"),
        "timeline.rule_violated": ops.RuleViolation("crossfade_first", "the first clip has nothing to crossfade from"),
        "timeline.invalid": TimelineError("text can't be empty"),
        "timeline.not_found": ProjectNotFoundError("no project named 'x'"),
        "timeline.exists": ProjectExistsError("project 'x' already exists"),
        "timeline.name_invalid": TimelineError("invalid project name 'a/b'"),
        "asset.not_found": AssetNotFoundError("unknown asset 'a_0000000000000000'"),
        "source.not_found": SourceNotFoundError("file not found: x.mp4"),
        "source.outside_root": OutsideRootError("'../x.mp4' is outside the project directory"),
        "media.unreadable": ProbeError("could not read 'x.mp4'"),
        "media.audio_only": FrameAudioOnlyError("asset a_1 is audio-only: it has no picture to extract"),
        "revision.conflict": RevisionConflictError("reel", 3, 1, "timeline project"),
        "preset.unknown": raised(presets.get, "zzz"),
        "fit.unknown": raised(presets.check_fit, "zzz"),
        "caption.style_unknown": raised(captions.get, "zzz"),
        "caption.invalid": raised(captions.check_color, "red"),
        "render.output_exists": OutputExistsError("out.mp4 already exists"),
        "render.no_clips": TimelineError("the timeline has no clips to render"),
        "render.request_invalid": RenderRequestError("each render request needs a project"),
        "render.size_invalid": TimelineError("size must look like 1080x1920, not '5'"),
        "ffmpeg.missing": FfmpegNotFoundError("'ffmpeg' not found on PATH; please install ffmpeg."),
        "ffmpeg.failed": ExtractError("ffmpeg failed rendering 'x.mp4'"),
        "extra.transcription_missing": TranscriptionUnavailableError(),
        "extra.ocr_missing": OcrUnavailableError(),
        "model.missing": _model_missing(),
        "transcript.missing": TranscriptNotFoundError("asset a_1 has no transcript yet"),
        "ocr.missing": OcrNotFoundError("asset a_1 has no on-screen text"),
        "analysis.missing": SearchNotFoundError("asset a_1 has nothing to search"),
        "job.unknown": UnknownJobError("unknown render job 'x'"),
        "job.failed": JobFailedError("boom"),
        "plan.invalid": _plan_invalid(),
        "ops.invalid": _ops_invalid(),
        "request.invalid": SearchQueryError("cursor is not one returned by this tool"),
        "error.internal": RuntimeError("boom"),
    }


def _model_missing():
    from split_video.editor.classify import ModelNotFoundError

    return ModelNotFoundError("no model")


def _plan_invalid():
    report = edit_plan.PlanReport()
    report.error("/tracks/0/clips/0/out", "plan.clip.out_of_range", "out is past the end", "use at most 30")
    return edit_plan.PlanInvalidError(report)


def _ops_invalid():
    report = edit_ops.OpsReport()
    report.error("/ops/1/clip", "ops.clip.not_found", "no such clip", "call show_timeline for the ids")
    return edit_ops.OpsInvalidError(report)


def test_every_registered_code_is_reachable_and_its_fix_is_filled_in():
    samples = _samples()
    assert set(samples) == set(REGISTRY), "add a sample for every registered code (and none for a missing one)"
    for code, exc in samples.items():
        info = describe(exc)
        assert info.code == code, (code, str(exc), info.code)
        assert info.message and info.fix and not re.search(r"<[a-z_]+>|\{\{", info.fix), (code, info.fix)


def test_the_payload_has_code_message_fix_and_optional_details():
    d = error_dict(RevisionConflictError("reel", 3, 1, "timeline project"))
    assert list(d)[:4] == ["error", "current_revision", "base_revision", "code"][:0] or {
        "code",
        "message",
        "fix",
    } <= set(d)
    assert d["code"] == "revision.conflict" and d["details"]["current_revision"] == 3
    assert "base_revision=3" in d["fix"] and "split-video compose show reel" in d["fix"]
    # A legacy key stays at the top level, so old clients keep working.
    assert d["error"] == "revision_conflict" and d["current_revision"] == 3 and d["base_revision"] == 1
    plain = error_dict(RuntimeError("boom"))
    assert set(plain) == {"code", "message", "fix"} and json.loads(json.dumps(plain)) == plain


# --- the common mistakes, raised by the real engine on in-memory stores -----------------------------------------------------


def test_overlapping_clips_on_a_free_track_say_where_the_gap_is(store, media):
    overlay = timeline_service.add_track(store, "reel", "video", media).tracks[-1]
    timeline_service.add_clip(store, media, "reel", "a.mp4", track_id=overlay.id, start=2.0, out_point=5)
    exc = raised(timeline_service.add_clip, store, media, "reel", "a.mp4", track_id=overlay.id, start=4.0, out_point=5)
    info = describe(exc)
    assert info.code == "clip.overlap" and "until 7s" in info.message
    assert "show_timeline" in info.fix and "previous clip" in info.fix


def test_overlapping_segments_and_splits(media):
    info = describe(raised(project.validate_pairs, [(0.0, 5.0), (4.0, 9.0)]))
    assert info.code == "segments.overlap" and "remove_split" in info.fix and "list_segments" in info.fix
    two = [SavedSegment(0.0, 10.0), SavedSegment(10.0, 20.0)]
    assert describe(raised(project.add_split, two, 10.0)).code == "segments.too_close"


def test_bad_times_get_distinct_codes_with_the_range_to_use(store, media):
    clip_id = store.load_existing_timeline("reel").tracks[0].clips[0].id
    beyond = describe(raised(timeline_service.trim_clip, store, media, "reel", clip_id, out_point=99.0))
    assert beyond.code == "time.out_of_range" and beyond.details["max_seconds"] == 30.0
    assert "0 s to 30 s" in beyond.fix and "list_assets" in beyond.fix
    negative = describe(raised(timeline_service.trim_clip, store, media, "reel", clip_id, in_point=-1.0))
    assert negative.code == "time.negative"
    inverted = describe(
        raised(timeline_service.trim_clip, store, media, "reel", clip_id, in_point=9.99, out_point=9.99)
    )
    assert inverted.code == "clip.too_short" and "0.1" in inverted.fix
    assert describe(raised(parse_time, "1:xx")).code == "time.invalid"
    assert describe(raised(parse_time, "-5")).code == "time.negative"
    assert describe(raised(project.add_split, [SavedSegment(0.0, 20.0)], 25.0)).details["max_seconds"] == 20.0
    assert (
        describe(FrameTimeError("time 50 s is past the end of the asset, which is 30 s long")).code
        == "time.out_of_range"
    )


def test_a_missing_asset_source_and_timeline_say_how_to_find_the_right_one(store, media):
    asset = describe(raised(timeline_service.add_clip, store, media, "reel", asset="a_0000000000000000"))
    assert asset.code == "asset.not_found" and "list_assets" in asset.fix and "scan_library" in asset.fix
    source = describe(raised(timeline_service.add_clip, store, media, "reel", "nope.mp4"))
    assert source.code == "source.not_found" and "list_videos" in source.fix
    outside = describe(raised(timeline_service.add_clip, store, media, "reel", "../etc/passwd"))
    assert outside.code == "source.outside_root" and "--root" in outside.fix
    timeline = describe(raised(timeline_service.load_existing, store, "ghost", media))
    assert timeline.code == "timeline.not_found" and "list_timelines" in timeline.fix
    exists = describe(raised(timeline_service.create_project, store, "reel"))
    assert exists.code == "timeline.exists" and "base_revision" in exists.fix


def test_a_stale_revision_says_which_revision_to_retry_with(store, media):
    exc = raised(timeline_service.add_track, store, "reel", "audio", media, base_revision=0)
    info = describe(exc)
    assert info.code == "revision.conflict" and info.spec.status == 409 and info.spec.exit_code == 3
    assert info.details == {"project": "reel", "kind": "timeline project", "current_revision": 1, "base_revision": 0}
    assert "base_revision=1" in info.fix and "If-Match: 1" in info.fix and "--base-revision 1" in info.fix


def test_unknown_preset_fit_and_caption_style_list_the_valid_choices(store, media):
    preset = describe(raised(timeline_service.plan_render, store, media, "reel", None, False, preset="zzz"))
    assert preset.code == "preset.unknown" and preset.details["choices"] == ", ".join(presets.names())
    assert all(name in preset.fix for name in presets.names())
    fit = describe(raised(timeline_service.plan_render, store, media, "reel", None, False, fit="stretch"))
    assert fit.code == "fit.unknown" and "letterbox, crop" in fit.fix
    style = describe(raised(captions.get, "no-such-style"))
    assert style.code == "caption.style_unknown" and all(n in style.fix for n in captions.names())
    assert describe(raised(captions.check_color, "red")).code == "caption.invalid"


def test_a_missing_optional_extra_gives_the_install_command_and_exit_code_4():
    transcription = describe(TranscriptionUnavailableError())
    assert transcription.code == "extra.transcription_missing" and transcription.spec.exit_code == 4
    assert "uv sync --extra transcribe" in transcription.fix and "WITH_TRANSCRIPTION=1" in transcription.fix
    ocr = describe(OcrUnavailableError())
    assert ocr.code == "extra.ocr_missing" and "apt install tesseract-ocr" in ocr.fix
    assert describe(TranscriptionError("no audio")).code == "ffmpeg.failed"


def test_a_bad_edit_plan_and_ops_batch_keep_their_issue_list_and_add_the_first_hint():
    plan = describe(_plan_invalid())
    assert plan.code == "plan.invalid" and plan.legacy["error"] == "plan_invalid"
    assert plan.legacy["errors"][0]["code"] == "plan.clip.out_of_range"
    assert "use at most 30" in plan.fix and "dry_run" in plan.fix
    ops_info = describe(_ops_invalid())
    assert (
        ops_info.code == "ops.invalid" and ops_info.legacy["error"] == "ops_invalid" and "show_timeline" in ops_info.fix
    )
    assert ops_info.to_dict()["errors"][0]["code"] == "ops.clip.not_found"


def test_media_store_errors_map_to_source_codes():
    assert describe(MediaNotFoundError("x.mp4")).code == "source.not_found"
    assert describe(MediaOutsideRootError("../x")).code == "source.outside_root"
    assert describe(UnknownBatchError("unknown render batch 'b'")).code == "job.unknown"
    assert (
        describe(RenderQueueError("renders is empty: give at least one render request")).code
        == "render.request_invalid"
    )
