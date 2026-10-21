"""Small fixes found by the end-to-end check (SPL-112): hints, per-context fixes, output rounding, lock files, log
redaction, and the scan's skipped rows."""

import json
import logging

import pytest
from test_edit_plan import World

from split_video import edit_plan, highlights, library, presets, timeline_service, transcribe
from split_video.error_catalogue import REGISTRY, describe
from split_video.local_media_store import LocalMediaStore


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def plan_of(world, *clips, **extra):
    return {
        "schema_version": 1,
        "project": "reel",
        "tracks": [{"kind": "video", "clips": list(clips)}],
        **extra,
    }


def issues(world, plan):
    report = edit_plan.validate_plan(world.media, plan)
    return {e.code: e for e in report.errors}, report


# --- (b) preset.unknown has a fix that fits where it was raised ---------------------------------------------------------


def test_an_unknown_highlight_preset_points_at_the_highlight_presets_not_the_export_ones():
    info = describe(
        _raised(highlights.HighlightsError, "unknown preset 'zzz': valid presets are default, gig, stream, lecture")
    )
    assert info.code == "preset.unknown"
    assert "default, gig, stream, lecture" in info.fix and "suggest_highlights" in info.fix
    assert "list_export_presets" in info.fix  # only to say these are not those
    assert info.fix.startswith("Use one of: default, gig, stream, lecture")
    assert "docs/highlights.md" in info.fix


def test_an_unknown_export_preset_still_points_at_list_export_presets():
    info = describe(_raised(presets.get, "zzz"))
    assert info.code == "preset.unknown"
    assert "youtube-1080p" in info.fix and "`list_export_presets`" in info.fix
    assert "suggest_highlights" not in info.fix


def test_the_catalogue_doc_lists_the_highlight_variant():
    assert "highlights" in REGISTRY["preset.unknown"].variants
    assert "For a highlights preset" in (_repo() / "docs" / "errors.md").read_text()


# --- (c) hints --------------------------------------------------------------------------------------------------------


def test_range_pattern_and_too_short_errors_say_what_to_do(world):
    ids = world.ids
    errors, _ = issues(world, plan_of(world, {"asset": ids["a"], "out": 0.05}))
    assert "0.1s" in errors["plan.clip.too_short"].hint and "'out'" in errors["plan.clip.too_short"].hint
    errors, _ = issues(world, plan_of(world, {"asset": ids["a"], "out": 5, "gain": 99}))
    assert errors["plan.field.range"].hint.startswith("send a value ")
    assert "between" in errors["plan.field.range"].hint or "at most" in errors["plan.field.range"].hint
    errors, _ = issues(world, plan_of(world, {"kind": "text", "text": "hi", "out": 2, "start": 0, "color": "red"}))
    assert errors["plan.field.pattern"].hint == "write it as #rrggbb, for example #ffcc00"


def test_replacing_an_existing_project_warns_with_a_hint(world):
    plan = plan_of(world, {"asset": world.ids["a"], "in": 0, "out": 5})
    edit_plan.apply_plan(world.media, world.store, plan)
    replace = {**plan, "mode": "replace", "base_revision": 1}
    report = edit_plan.validate_plan(world.media, replace, store=world.store)
    warning = next(w for w in report.warnings if w.code == "plan.project.replaces_existing")
    assert "show_timeline" in warning.hint and "dry_run" in warning.hint


def test_a_bad_colour_on_a_media_clip_is_one_error_not_two(world):
    errors, report = issues(world, plan_of(world, {"asset": world.ids["a"], "out": 5, "color": "red"}))
    assert [(e.path, e.code) for e in report.errors] == [("/tracks/0/clips/0/color", "plan.clip.field_not_applicable")]
    assert errors["plan.clip.field_not_applicable"].hint == 'text clips are kind "text"'


def test_a_bad_colour_on_a_text_clip_is_still_a_pattern_error(world):
    plan = plan_of(world, {"asset": world.ids["a"], "out": 5})
    plan["tracks"].append(
        {"kind": "video", "magnetic": False, "clips": [{"kind": "text", "text": "hi", "out": 2, "color": "red"}]}
    )
    _, report = issues(world, plan)
    assert [e.code for e in report.errors] == ["plan.field.pattern"]


# --- (d) output rounding, storage untouched ---------------------------------------------------------------------------


def test_outputs_are_rounded_to_a_millisecond_but_the_stored_file_keeps_full_precision(tmp_path):
    w = World("local", tmp_path)
    clips = [{"asset": w.ids["a"], "in": 0.1 * i + 0.00049, "out": 0.1 * i + 0.00049 + 3.3} for i in range(1, 8)]
    plan = plan_of(w, *clips)
    result = edit_plan.apply_plan(w.media, w.store, plan)
    stored = json.loads((tmp_path / "reel.split-video-timeline.json").read_text())
    raw_in = stored["tracks"][0]["clips"][2]["in"]
    shown = timeline_service.describe(w.media, "reel", w.store.load_timeline("reel"))
    clip = shown["tracks"][0]["clips"][2]
    assert raw_in != round(raw_in, 3), "the fixture must have sub-millisecond noise"
    assert clip["in"] == round(raw_in, 3)
    for key in ("in", "out", "start", "end", "duration"):
        assert clip[key] == round(clip[key], 3), key
    assert shown["duration"] == round(shown["duration"], 3) == result["duration"]
    assert all(t["end"] == round(t["end"], 3) for t in result["tracks"])
    # Nothing stored changed: the file still holds the raw floats, and the plan hash is of the plan as sent.
    assert stored["tracks"][0]["clips"][2]["in"] == plan["tracks"][0]["clips"][2]["in"]
    assert result["plan_hash"] == edit_plan.canonical_hash(plan)


def test_a_crop_is_shown_without_float_noise_and_stored_as_given(world):
    edit_plan.apply_plan(world.media, world.store, plan_of(world, {"asset": world.ids["a"], "out": 5}))
    clip_id = world.store.load_timeline("reel").tracks[0].clips[0].id
    box = {"x": 0.1 + 0.2, "y": 0.0, "w": 0.316406 + 1e-12, "h": 0.5625}
    timeline_service.set_clip_crop(world.store, "reel", clip_id, media=world.media, crop=box)
    stored = world.store.load_timeline("reel").tracks[0].clips[0].crop
    assert stored["x"] == box["x"] and stored["x"] != 0.3, "the stored crop is untouched"
    shown = timeline_service.describe(world.media, "reel", world.store.load_timeline("reel"))
    assert shown["tracks"][0]["clips"][0]["crop"] == {"x": 0.3, "y": 0.0, "w": 0.316406, "h": 0.5625}


def test_render_results_report_a_rounded_duration(world):
    edit_plan.apply_plan(
        world.media,
        world.store,
        plan_of(world, *[{"asset": world.ids["a"], "in": 0.1 * i, "out": 0.1 * i + 0.3} for i in range(1, 6)]),
    )
    project = world.store.load_timeline("reel")
    out = timeline_service.render_result("reel", "x.mp4", project, {})
    assert out["duration"] == round(out["duration"], 3)


# --- (a) the scan's skipped rows ----------------------------------------------------------------------------------------


def test_an_unreadable_file_is_skipped_with_a_short_reason_a_code_and_a_fix(tmp_path):
    (tmp_path / "broken.mp4").write_bytes(b"not really a video" * 50)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "also broken.mp4").write_bytes(b"x" * 100)
    report = library.scan(LocalMediaStore(tmp_path))
    assert {s["path"] for s in report.skipped} == {"broken.mp4", "sub/also broken.mp4"}
    for row in report.skipped:
        assert row["code"] == "media.unreadable" and "ffprobe" in row["fix"]
        assert str(tmp_path) not in row["reason"] and "/tmp" not in row["reason"] and "0x" not in row["reason"]
        assert 0 < len(row["reason"]) <= library.MAX_REASON and "\n" not in row["reason"]
    assert report.to_dict()["skipped"][0].keys() >= {"path", "reason", "code", "fix"}


def test_short_reason_drops_absolute_paths_and_addresses():
    exc = library.ProbeError(
        "could not read '/home/me/videos/a b.mp4': [mov,mp4 @ 0x5581] moov atom not found\n"
        "/home/me/videos/a b.mp4: Invalid data found when processing input"
    )
    assert library.short_reason(exc, "a b.mp4") == "could not read as media: Invalid data found when processing input"
    odd = library.ProbeError("could not read a duration from '/data/in/x.mp4'")
    assert "/data" not in library.short_reason(odd, "x.mp4")


# --- (e) URL redaction in logs ----------------------------------------------------------------------------------------


def test_url_query_strings_are_redacted():
    text = 'HTTP Request: GET https://cdn.example.com/m/model.bin?X-Amz-Signature=abc123&X-Amz-Date=1 "HTTP/1.1 200 OK"'
    out = transcribe.redact_urls(text)
    assert "abc123" not in out and "?<redacted>" in out and "https://cdn.example.com/m/model.bin" in out
    assert transcribe.redact_urls("see https://a.b/c and nothing else") == "see https://a.b/c and nothing else"


def test_the_http_libraries_log_without_signatures(caplog):
    transcribe.redact_logged_urls()
    transcribe.redact_logged_urls()  # idempotent
    assert len([f for f in logging.getLogger("httpx").filters if isinstance(f, transcribe._RedactUrls)]) == 1
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "HTTP/1.1 200 OK"', "GET", "https://h.co/f?X-Amz-Signature=SECRET"
        )
    assert "SECRET" not in caplog.text and "https://h.co/f?<redacted>" in caplog.text


def test_load_model_redacts_urls_in_its_error(monkeypatch, tmp_path):
    class Boom:
        class WhisperModel:
            def __init__(self, *a, **k):
                raise OSError("GET https://h.co/f?X-Amz-Signature=SECRET failed")

    monkeypatch.setattr(transcribe, "_import_faster_whisper", lambda models_dir=None: Boom)
    with pytest.raises(transcribe.TranscriptionError) as err:
        transcribe.load_model("base.en", str(tmp_path))
    assert "SECRET" not in str(err.value)


# --- helpers ----------------------------------------------------------------------------------------------------------


def _raised(fn_or_cls, *args):
    try:
        exc = fn_or_cls(*args)
        if isinstance(exc, BaseException):
            return exc
    except BaseException as e:  # noqa: BLE001
        return e
    raise AssertionError("did not raise")


def _repo():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]
