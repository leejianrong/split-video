"""Edit plans (SPL-49): one JSON document builds a whole timeline, atomically, on both stores."""

import copy
import importlib.util
import json
import re
import time
from pathlib import Path

import pytest
from fake_signals import make_store as make_signal_store
from fake_signals import plant
from jsonschema import Draft202012Validator

from split_video import assets, edit_plan, highlights, local_store, timeline
from split_video.edit_plan import PlanInvalidError
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import InMemoryMediaStore
from split_video.store import InMemoryProjectStore

REPO_ROOT = Path(__file__).resolve().parents[2]

FILES = {
    "a.mp4": (b"aaaa", MediaInfo(duration=300.0, width=640, height=360, has_audio=True, fps=25.0)),
    "b.mp4": (b"bbbb", MediaInfo(duration=120.0, width=640, height=360, has_audio=True, fps=25.0)),
    "c.mp4": (b"cccc", MediaInfo(duration=60.0, width=640, height=360, has_audio=False, fps=25.0)),
    "song.mp3": (b"ssss", MediaInfo(duration=200.0, width=None, height=None, has_audio=True, fps=None)),
}


class World:
    def __init__(self, kind: str, tmp_path) -> None:
        self.kind = kind
        if kind == "memory":
            self.media = InMemoryMediaStore()
            for name, (data, info) in FILES.items():
                self.media.add(name, data, info=info)
            self.store = InMemoryProjectStore()
        else:
            self.media = LocalMediaStore(tmp_path)
            for name, (data, _) in FILES.items():
                (tmp_path / name).write_bytes(data)
            self.media.probe = lambda media_id: FILES[media_id][1]
            self.store = LocalProjectStore(tmp_path)
        self.ids = {name.split(".")[0]: assets.register_path(self.media, name).id for name in FILES}


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def minimal(world, **extra):
    plan = {
        "schema_version": 1,
        "project": "reel",
        "tracks": [{"kind": "video", "clips": [{"asset": world.ids["a"], "in": 10, "out": 20}]}],
    }
    return {**plan, **extra}


def pairs(report):
    return {(e.path, e.code) for e in report.errors}


def codes(report):
    return [e.code for e in report.errors]


def thirty_clip_plan(ids):
    """30 clips on 4 tracks: 18 on the main track (a crossfade every 4th), 6 overlays, 3 titles, 3 audio clips."""
    sources = [ids["a"], ids["b"], ids["c"]]
    main = []
    for j in range(18):
        clip = {"asset": sources[j % 3], "in": (j // 3) * 8, "out": (j // 3) * 8 + 8}
        if j % 4 == 3:
            clip.update(transition="crossfade", transition_duration=0.5)
        main.append(clip)
    overlay = [
        {
            "asset": ids["a"],
            "in": 100 + k * 5,
            "out": 105 + k * 5,
            "start": k * 20,
            "scale": 0.3,
            "x": 0.8,
            "opacity": 0.9,
        }
        for k in range(6)
    ]
    titles = [{"kind": "text", "text": f"Song {k}", "out": 3, "start": k * 30, "color": "#ffcc00"} for k in range(3)]
    audio = [{"asset": ids["song"], "in": k * 10, "out": k * 10 + 10, "gain": 0.8} for k in range(3)]
    return {
        "schema_version": 1,
        "project": "reel",
        "meta": {"note": "thirty clips"},
        "tracks": [
            {"kind": "video", "clips": main},
            {"kind": "video", "magnetic": False, "clips": overlay},
            {"kind": "video", "magnetic": False, "clips": titles},
            {"kind": "audio", "magnetic": False, "clips": audio},
        ],
    }


class SpyStore:
    """Counts every write the engine makes to the store."""

    def __init__(self, store) -> None:
        self._store = store
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if name in ("save_timeline", "create_timeline", "update_timeline", "save_split", "update_split"):

            def wrapped(*args, **kwargs):
                self.calls.append(name)
                return attr(*args, **kwargs)

            return wrapped
        return attr


# --- done when: a plan with 30 clips applies in one call ---------------------------------------------------------------


def test_a_30_clip_plan_applies_in_one_save_and_round_trips(world):
    plan = thirty_clip_plan(world.ids)
    spy = SpyStore(world.store)
    result = edit_plan.apply_plan(world.media, spy, plan)
    assert spy.calls == ["save_timeline"]  # ONE write for the whole timeline
    assert result["project"] == "reel" and result["action"] == "created" and result["dry_run"] is False
    assert result["revision"] == 1 and result["clips"] == 30
    assert result["meta"] == {"note": "thirty clips"}
    assert [t["kind"] for t in result["tracks"]] == ["video", "video", "video", "audio"]
    assert [t["clips"] for t in result["tracks"]] == [18, 6, 3, 3]
    assert result["duration"] == pytest.approx(18 * 8 - 4 * 0.5)  # four crossfades
    assert result["plan_hash"] == edit_plan.canonical_hash(plan) and len(result["plan_hash"]) == 64
    assert result["warnings"] == []

    loaded = world.store.load_existing_timeline("reel")
    assert loaded.revision == 1 and loaded == edit_plan.build_timeline(world.media, plan)
    ids = [c.id for t in loaded.tracks for c in t.clips] + [t.id for t in loaded.tracks]
    assert len(ids) == len(set(ids)) == 34 and all(ids)
    for track, planned in zip(loaded.tracks, plan["tracks"], strict=True):
        assert (track.kind, track.magnetic) == (planned["kind"], planned.get("magnetic", True))
        assert len(track.clips) == len(planned["clips"])
        for clip, want in zip(track.clips, planned["clips"], strict=True):
            assert (clip.in_point, clip.out_point) == (want.get("in", 0), want["out"])
            assert clip.asset == want.get("asset", "") and clip.kind == want.get("kind", "media")
            assert clip.gain == want.get("gain", 1.0) and clip.opacity == want.get("opacity", 1.0)
            assert clip.transition == want.get("transition", "cut") and clip.text == want.get("text", "")
    main, overlay, titles, audio = loaded.tracks
    assert [(c.transition, c.transition_duration) for c in main.clips if c.transition != "cut"] == [
        ("crossfade", 0.5)
    ] * 4
    assert main.clips[4].start == pytest.approx(4 * 8 - 0.5)  # the clip after the first crossfade
    assert [c.start for c in overlay.clips] == [0, 20, 40, 60, 80, 100]
    assert [c.start for c in audio.clips] == [0, 10, 20]  # no start: right after the previous clip
    assert titles.clips[1].text == "Song 1" and titles.clips[1].y == 0.85 and titles.clips[1].color == "#ffcc00"
    assert loaded.tracks[0].clips[0].source == "a.mp4"  # the asset's path is stored as the hint


def test_asset_wins_over_source_and_source_alone_resolves(world):
    plan = minimal(world)
    plan["tracks"][0]["clips"] = [
        {"asset": world.ids["a"], "source": "a.mp4", "out": 5},
        {"source": "b.mp4", "in": 1, "out": 3},
        {"asset": world.ids["b"], "source": "a.mp4", "out": 3},  # disagree: the asset wins, with a warning
    ]
    report = edit_plan.validate_plan(world.media, plan)
    assert report.ok, report.errors
    assert [(w.path, w.code) for w in report.warnings] == [("/tracks/0/clips/2/source", "plan.clip.source_mismatch")]
    project = edit_plan.build_timeline(world.media, plan)
    assert [c.asset for c in project.tracks[0].clips] == [world.ids["a"], world.ids["b"], world.ids["b"]]
    assert project.tracks[0].clips[2].out_point == 3


def test_a_300_clip_plan_validates_and_applies_quickly(world):
    ids = world.ids
    main = [{"asset": ids["a"], "in": (k * 2) % 250, "out": (k * 2) % 250 + 4} for k in range(150)]
    overlay = [{"asset": ids["b"], "in": k % 100, "out": k % 100 + 2, "start": k * 3} for k in range(100)]
    titles = [{"kind": "text", "text": f"t{k}", "out": 2, "start": k * 5} for k in range(50)]
    plan = {
        "schema_version": 1,
        "project": "big",
        "tracks": [
            {"kind": "video", "clips": main},
            {"kind": "video", "magnetic": False, "clips": overlay},
            {"kind": "video", "magnetic": False, "clips": titles},
        ],
    }
    started = time.perf_counter()
    report = edit_plan.validate_plan(world.media, plan, world.store)
    result = edit_plan.apply_plan(world.media, world.store, plan)
    elapsed = time.perf_counter() - started
    assert report.ok and result["clips"] == 300
    assert elapsed < 2.0, f"validate + apply took {elapsed:.2f}s"  # about 0.1 s on the dev machine


# --- done when: an invalid plan reports every error and saves nothing --------------------------------------------------


def broken_plan(ids):
    a, song, c = ids["a"], ids["song"], ids["c"]
    return {
        "schema_version": 1,
        "project": "reel",
        "mode": "create",
        "base_revision": 3,
        "extra": True,
        "tracks": [
            {
                "kind": "video",
                "clips": [
                    {"asset": a, "out": 6, "transition": "crossfade", "transition_duration": 1},
                    {"asset": a, "in": 10, "out": 400},
                    {"asset": "a_0000000000000000", "out": 5},
                    {"asset": song, "out": 5},
                    {"source": "missing.mp4"},
                    {"asset": a, "in": 5, "out": 5},
                    {"asset": a, "out": 5, "start": 3},
                    {"asset": a, "out": 5, "bogus": 1},
                    {"asset": a, "out": 5, "gain": 99},
                    {"kind": "text", "text": "hi", "out": 3},
                    {"asset": a, "out": 4, "id": "dup"},
                    {"asset": a, "out": 4, "id": "dup"},
                    {"asset": a, "out": 2, "transition": "crossfade", "transition_duration": 5},
                    {"asset": "not-an-id", "out": 2},
                    {"source": "../escape.mp4", "out": 2},
                    {"asset": a, "out": 2, "text": "no text on media"},
                ],
            },
            {
                "kind": "video",
                "magnetic": False,
                "clips": [
                    {"asset": a, "start": 0, "out": 10},
                    {"asset": a, "start": 5, "out": 15},
                    {"kind": "text", "out": 3},
                    {"kind": "text", "text": "x", "out": 2, "color": "red"},
                    {"asset": a, "out": 5, "start": 30, "transition": "crossfade", "transition_duration": 1},
                    {"kind": "text", "text": "x"},
                ],
            },
            {"kind": "audio", "clips": [{"asset": c, "out": 5}]},
            {"kind": "movie", "clips": []},
        ],
    }


EXPECTED_BROKEN = {
    ("/extra", "plan.field.unknown"),
    ("/base_revision", "plan.mode.field_not_applicable"),
    ("/tracks/0/clips/0/transition", "plan.clip.crossfade_first"),
    ("/tracks/0/clips/1/out", "plan.clip.out_of_range"),
    ("/tracks/0/clips/2/asset", "plan.asset.unknown"),
    ("/tracks/0/clips/3", "plan.clip.audio_only_on_video_track"),
    ("/tracks/0/clips/4/source", "plan.source.unknown"),
    ("/tracks/0/clips/5/out", "plan.clip.in_after_out"),
    ("/tracks/0/clips/6/start", "plan.clip.start_on_magnetic_track"),
    ("/tracks/0/clips/7/bogus", "plan.field.unknown"),
    ("/tracks/0/clips/8/gain", "plan.field.range"),
    ("/tracks/0/clips/9", "plan.clip.text_on_main_track"),
    ("/tracks/0/clips/11/id", "plan.id.duplicate"),
    ("/tracks/0/clips/12/transition_duration", "plan.clip.transition_too_long"),
    ("/tracks/0/clips/13/asset", "plan.asset.invalid_id"),
    ("/tracks/0/clips/14/source", "plan.source.outside_root"),
    ("/tracks/0/clips/15/text", "plan.clip.field_not_applicable"),
    ("/tracks/1/clips/1/start", "plan.clip.overlap"),
    ("/tracks/1/clips/2/text", "plan.clip.text_required"),
    ("/tracks/1/clips/3/color", "plan.field.pattern"),
    ("/tracks/1/clips/4/transition", "plan.clip.crossfade_on_free_track"),
    ("/tracks/1/clips/5/out", "plan.field.required"),
    ("/tracks/2/clips/0", "plan.clip.no_audio_for_audio_track"),
    ("/tracks/3/kind", "plan.field.enum"),
}


def test_an_invalid_plan_reports_every_problem_at_once_and_saves_nothing(world):
    before_table = world.media.load_asset_table()
    spy = SpyStore(world.store)
    plan = broken_plan(world.ids)
    original = copy.deepcopy(plan)
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, spy, plan)
    report = caught.value.report
    assert not report.ok and plan == original  # the input is not touched
    assert EXPECTED_BROKEN <= pairs(report), EXPECTED_BROKEN - pairs(report)
    assert len({e.code for e in report.errors}) >= 10
    assert all(e.message and e.path.startswith("/") or e.path == "" for e in report.errors)
    assert spy.calls == [] and world.store.list_timelines() == []
    assert world.media.load_asset_table() == before_table
    compact = json.loads(str(caught.value))
    assert compact["error"] == "plan_invalid" and len(compact["errors"]) == len(report.errors)


def test_every_error_has_a_hint_path_and_stable_code(world):
    report = edit_plan.validate_plan(world.media, broken_plan(world.ids))
    for e in report.errors:
        assert e.code.startswith("plan.") and e.message
    assert next(e for e in report.errors if e.code == "plan.source.unknown").hint.startswith("run scan_library")
    assert "allowed fields" in next(e for e in report.errors if e.path == "/extra").hint


def test_an_invalid_replace_leaves_the_existing_project_untouched(world):
    edit_plan.apply_plan(world.media, world.store, minimal(world))
    before = world.store.load_existing_timeline("reel")
    bad = minimal(world, mode="replace", base_revision=1)
    bad["tracks"][0]["clips"].append({"asset": world.ids["a"], "in": 5, "out": 500})
    with pytest.raises(PlanInvalidError):
        edit_plan.apply_plan(world.media, world.store, bad)
    after = world.store.load_existing_timeline("reel")
    assert after == before and after.revision == before.revision == 1


def test_a_failed_save_leaves_the_previous_project_intact(world, monkeypatch):
    edit_plan.apply_plan(world.media, world.store, minimal(world))
    before = world.store.load_existing_timeline("reel")
    replace_plan = thirty_clip_plan(world.ids)
    replace_plan.update(mode="replace", base_revision=1)
    create_plan = {**thirty_clip_plan(world.ids), "project": "another"}
    store = world.store
    if world.kind == "memory":

        class Failing(SpyStore):
            def save_timeline(self, *args, **kwargs):
                raise RuntimeError("disk full")

        store = Failing(world.store)
    else:

        def boom(path, payload):
            raise OSError("disk full")

        monkeypatch.setattr(local_store, "_write_json_atomically", boom)
    for plan in (replace_plan, create_plan):
        with pytest.raises((RuntimeError, OSError), match="disk full"):
            edit_plan.apply_plan(world.media, store, plan)
    assert world.store.load_existing_timeline("reel") == before
    assert world.store.load_existing_timeline("reel").revision == 1
    assert world.store.list_timelines() == ["reel"]  # nothing was created for the failed create


# --- mode and revision rules --------------------------------------------------------------------------------------------


def test_create_fails_if_the_project_exists(world):
    edit_plan.apply_plan(world.media, world.store, minimal(world))
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, world.store, minimal(world))
    assert pairs(caught.value.report) == {("/project", "plan.project.exists")}
    assert world.store.load_existing_timeline("reel").revision == 1


def test_replace_needs_a_base_revision_or_the_loud_flag(world):
    edit_plan.apply_plan(world.media, world.store, minimal(world))
    new = minimal(world, mode="replace")
    new["tracks"][0]["clips"][0]["out"] = 30
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, world.store, new)
    assert pairs(caught.value.report) == {("/base_revision", "plan.replace.needs_base_revision")}

    with pytest.raises(RevisionConflictError) as conflict:  # stale: the 409 path, nothing written
        edit_plan.apply_plan(world.media, world.store, {**new, "base_revision": 0})
    assert (conflict.value.current, conflict.value.base) == (1, 0)
    assert world.store.load_existing_timeline("reel").tracks[0].clips[0].out_point == 20

    result = edit_plan.apply_plan(world.media, world.store, {**new, "base_revision": 1})
    assert (result["action"], result["revision"]) == ("replaced", 2)
    assert [w["code"] for w in result["warnings"]] == ["plan.project.replaces_existing"]
    assert world.store.load_existing_timeline("reel").tracks[0].clips[0].out_point == 30

    again = edit_plan.apply_plan(world.media, world.store, {**new, "replace_unconditionally": True})
    assert again["revision"] == 3

    with pytest.raises(PlanInvalidError) as both:
        edit_plan.apply_plan(world.media, world.store, {**new, "base_revision": 3, "replace_unconditionally": True})
    assert pairs(both.value.report) == {("/replace_unconditionally", "plan.replace.ambiguous")}


def test_the_request_revision_can_replace_the_one_in_the_plan_but_not_contradict_it(world):
    edit_plan.apply_plan(world.media, world.store, minimal(world))
    new = minimal(world, mode="replace")
    assert edit_plan.apply_plan(world.media, world.store, new, base_revision=1)["revision"] == 2
    with pytest.raises(RevisionConflictError):
        edit_plan.apply_plan(world.media, world.store, new, base_revision=1)
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, world.store, {**new, "base_revision": 2}, base_revision=5)
    assert ("/base_revision", "plan.base_revision.conflict") in pairs(caught.value.report)


def test_replace_of_a_missing_project_and_create_only_fields(world):
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, world.store, minimal(world, mode="replace", base_revision=1))
    assert ("/project", "plan.project.not_found") in pairs(caught.value.report)
    with pytest.raises(PlanInvalidError) as caught:
        edit_plan.apply_plan(world.media, world.store, minimal(world, replace_unconditionally=True))
    assert pairs(caught.value.report) == {("/replace_unconditionally", "plan.mode.field_not_applicable")}


def test_dry_run_writes_nothing_and_predicts_the_result(world):
    spy = SpyStore(world.store)
    result = edit_plan.apply_plan(world.media, spy, thirty_clip_plan(world.ids), dry_run=True)
    assert spy.calls == [] and world.store.list_timelines() == []
    assert result["dry_run"] is True and result["revision"] == 1 and result["clips"] == 30
    real = edit_plan.apply_plan(world.media, spy, thirty_clip_plan(world.ids))
    assert {**result, "dry_run": False} == real  # the same document, as promised
    replace = {**thirty_clip_plan(world.ids), "mode": "replace", "base_revision": 1}
    assert edit_plan.apply_plan(world.media, spy, replace, dry_run=True)["revision"] == 2
    with pytest.raises(RevisionConflictError):
        edit_plan.apply_plan(world.media, spy, {**replace, "base_revision": 0}, dry_run=True)
    with pytest.raises(PlanInvalidError):
        edit_plan.apply_plan(world.media, spy, minimal(world, tracks=[]), dry_run=True)
    assert spy.calls == ["save_timeline"] and world.store.load_existing_timeline("reel").revision == 1


def test_validate_plan_is_pure(world):
    spy = SpyStore(world.store)
    table = world.media.load_asset_table()
    report = edit_plan.validate_plan(world.media, broken_plan(world.ids), spy)
    assert not report.ok and spy.calls == []
    assert world.media.load_asset_table() == table and world.store.list_timelines() == []


# --- the contract: strictness, ids, versions -----------------------------------------------------------------------------


def test_unknown_fields_are_rejected_naming_the_field_and_the_allowed_ones(world):
    plan = minimal(world, colour="red")
    plan["tracks"][0]["clips"][0]["crop"] = {"x": 0}
    plan["tracks"][0]["wobble"] = 1
    report = edit_plan.validate_plan(world.media, plan)
    assert pairs(report) == {
        ("/colour", "plan.field.unknown"),
        ("/tracks/0/wobble", "plan.field.unknown"),
        ("/tracks/0/clips/0/crop", "plan.field.unknown"),
    }
    hint = next(e for e in report.errors if e.path.endswith("/crop")).hint
    assert "'crop'" in report.errors[2].message and "allowed fields here" in hint and "transition_duration" in hint


@pytest.mark.parametrize(
    ("doc", "code", "path"),
    [
        (5, "plan.document.not_object", ""),
        ([], "plan.document.not_object", ""),
        ({"project": "x", "tracks": []}, "plan.schema_version.missing", "/schema_version"),
        ({"schema_version": 2, "project": "x", "tracks": []}, "plan.schema_version.unsupported", "/schema_version"),
        ({"schema_version": "1", "project": "x", "tracks": []}, "plan.schema_version.invalid", "/schema_version"),
        ({"schema_version": True, "project": "x", "tracks": []}, "plan.schema_version.invalid", "/schema_version"),
        ({"schema_version": 0, "project": "x", "tracks": []}, "plan.schema_version.unsupported", "/schema_version"),
    ],
)
def test_schema_version_rules(world, doc, code, path):
    report = edit_plan.validate_plan(world.media, doc)
    assert (path, code) in pairs(report)
    if "unsupported" in code:
        assert codes(report) == [code]  # nothing else is judged against a format we don't know


def test_older_versions_go_through_migrate_plan(world, monkeypatch):
    def v0_to_v1(doc):  # a made-up v0: `name` instead of `project`, `clips` straight on the plan
        doc = dict(doc)
        return {
            "schema_version": 1,
            "project": doc.pop("name"),
            "tracks": [{"kind": "video", "clips": doc.pop("clips")}],
        }

    monkeypatch.setitem(edit_plan.MIGRATIONS, 0, v0_to_v1)
    v0 = {"schema_version": 0, "name": "old", "clips": [{"asset": world.ids["a"], "out": 4}]}
    assert edit_plan.migrate_plan(v0)["project"] == "old"
    assert v0["name"] == "old"  # migrating copies
    result = edit_plan.apply_plan(world.media, world.store, v0)
    assert result["project"] == "old" and result["clips"] == 1
    assert world.store.load_existing_timeline("old").tracks[0].clips[0].out_point == 4
    # The hash is of the migrated plan, so v0 and v1 spellings of the same plan agree.
    v1 = {"schema_version": 1, "project": "old", "tracks": [{"kind": "video", "clips": v0["clips"]}]}
    assert result["plan_hash"] == edit_plan.canonical_hash(v1)

    monkeypatch.setitem(edit_plan.MIGRATIONS, 0, lambda doc: 1 / 0)
    report = edit_plan.validate_plan(world.media, v0)
    assert codes(report) == ["plan.schema_version.migration_failed"]


def test_ids_are_deterministic_and_explicit_ids_win(world):
    plan = thirty_clip_plan(world.ids)
    first = edit_plan.build_timeline(world.media, plan)
    again = edit_plan.build_timeline(world.media, {**plan, "meta": {"note": "other note"}})
    assert first == again  # `meta` is not part of the plan's identity
    assert edit_plan.canonical_hash(plan) == edit_plan.canonical_hash({**plan, "meta": 1})
    changed = copy.deepcopy(plan)
    changed["tracks"][0]["clips"][0]["out"] = 7
    other = edit_plan.build_timeline(world.media, changed)
    assert {c.id for c in other.tracks[0].clips}.isdisjoint({c.id for c in first.tracks[0].clips})
    named = copy.deepcopy(plan)
    named["tracks"][0]["id"] = "main"
    named["tracks"][0]["clips"][0]["id"] = "intro"
    project = edit_plan.build_timeline(world.media, named)
    assert project.tracks[0].id == "main" and project.tracks[0].clips[0].id == "intro"
    dup = copy.deepcopy(named)
    dup["tracks"][1]["id"] = "main"
    dup["tracks"][1]["clips"][0]["id"] = "intro"
    assert pairs(edit_plan.validate_plan(world.media, dup)) == {
        ("/tracks/1/id", "plan.id.duplicate"),
        ("/tracks/1/clips/0/id", "plan.id.duplicate"),
    }
    bad = copy.deepcopy(named)
    bad["tracks"][0]["clips"][1]["id"] = "has space"
    assert ("/tracks/0/clips/1/id", "plan.id.invalid") in pairs(edit_plan.validate_plan(world.media, bad))


def test_field_checks(world):
    ids = world.ids

    def check(*clips, track=None, **top):
        plan = minimal(world, **top)
        plan["tracks"] = [track or {"kind": "video", "clips": list(clips)}]
        return pairs(edit_plan.validate_plan(world.media, plan))

    assert check({"asset": ids["a"], "out": 0.05}) == {("/tracks/0/clips/0/out", "plan.clip.too_short")}
    assert check({"asset": ids["a"], "in": -1, "out": 5}) == {("/tracks/0/clips/0/in", "plan.field.range")}
    assert check({"asset": ids["a"], "out": float("nan")}) == {("/tracks/0/clips/0/out", "plan.field.type")}
    assert check({"asset": ids["a"], "out": "5"}) == {("/tracks/0/clips/0/out", "plan.field.type")}
    assert check({"asset": ids["a"], "muted": 1}) == {("/tracks/0/clips/0/muted", "plan.field.type")}
    assert check({"asset": ids["a"], "kind": "gif"}) == {("/tracks/0/clips/0/kind", "plan.field.enum")}
    assert check({"asset": ids["a"], "transition": "wipe"}) == {("/tracks/0/clips/0/transition", "plan.field.enum")}
    assert check({"out": 3}) == {("/tracks/0/clips/0", "plan.clip.media_source_missing")}
    assert check(
        {"kind": "text", "text": "t", "out": 3, "asset": ids["a"]},
        track={
            "kind": "video",
            "magnetic": False,
            "clips": [{"kind": "text", "text": "t", "out": 3, "asset": ids["a"]}],
        },
    ) == {("/tracks/0/clips/0/asset", "plan.clip.field_not_applicable")}
    audio = {"kind": "audio", "clips": [{"kind": "text", "text": "t", "out": 3}]}
    assert check(track=audio) == {
        ("/tracks/0/clips/0", "plan.clip.text_on_audio_track"),
        ("/tracks", "plan.tracks.no_video"),
    }
    assert check(track={"kind": "audio", "clips": []}) == {("/tracks", "plan.tracks.no_video")}
    assert check(
        track={
            "kind": "video",
            "clips": [{"asset": ids["a"], "out": 5}, {"asset": ids["a"], "out": 2, "transition": "crossfade"}],
        }
    ) == {("/tracks/0/clips/1/transition_duration", "plan.clip.transition_duration")}
    assert check(project="a/b") == {("/project", "plan.project.name_invalid")}
    assert pairs(edit_plan.validate_plan(world.media, minimal(world, tracks=[]))) == {("/tracks", "plan.tracks.empty")}
    fine = edit_plan.validate_plan(world.media, minimal(world, mode="create", meta={"anything": [1, 2]}))
    assert fine.ok
    # An empty track is only a warning; a cut with a duration is ignored with a warning.
    plan = minimal(world)
    plan["tracks"].append({"kind": "video", "magnetic": False})
    plan["tracks"][0]["clips"][0].update(transition_duration=2)
    report = edit_plan.validate_plan(world.media, plan)
    assert report.ok and {w.code for w in report.warnings} == {
        "plan.track.empty",
        "plan.clip.transition_duration_ignored",
    }


def test_crossfade_room_is_checked_against_both_neighbours(world):
    a = world.ids["a"]
    clips = [
        {"asset": a, "out": 4},
        {"asset": a, "out": 4, "transition": "crossfade", "transition_duration": 3},
        {"asset": a, "out": 4, "transition": "crossfade", "transition_duration": 3},  # 1s left of the middle clip
    ]
    plan = minimal(world)
    plan["tracks"][0]["clips"] = clips
    assert pairs(edit_plan.validate_plan(world.media, plan)) == {
        ("/tracks/0/clips/2/transition_duration", "plan.clip.transition_too_long")
    }
    clips[2]["transition_duration"] = 1
    assert edit_plan.validate_plan(world.media, plan).ok
    project = edit_plan.build_timeline(world.media, plan)
    assert project.tracks[0].clips[2].start == pytest.approx((4 - 3 + 4) - 1)  # as `normalize_track` lays it out


def test_limits(world):
    a = world.ids["a"]
    one = {"asset": a, "out": 1}
    plan = minimal(world)
    plan["tracks"][0]["clips"] = [one] * (edit_plan.MAX_CLIPS + 1)
    started = time.perf_counter()
    report = edit_plan.validate_plan(world.media, plan)
    assert pairs(report) == {("/tracks", "plan.clips.too_many")} and time.perf_counter() - started < 1.0
    plan["tracks"][0]["clips"] = [one] * edit_plan.MAX_CLIPS
    assert edit_plan.validate_plan(world.media, plan).ok  # exactly the cap is fine
    plan["tracks"] = [{"kind": "video", "magnetic": False, "clips": []}] * (edit_plan.MAX_TRACKS + 1)
    assert pairs(edit_plan.validate_plan(world.media, plan)) == {("/tracks", "plan.tracks.too_many")}
    long_title = {"kind": "text", "text": "x" * 2000, "out": 2}
    titled = minimal(world, tracks=[{"kind": "video"}, {"kind": "video", "magnetic": False, "clips": [long_title]}])
    assert pairs(edit_plan.validate_plan(world.media, titled)) == {("/tracks/1/clips/0/text", "plan.field.length")}


# --- the published schema ------------------------------------------------------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location(
        "export_edit_plan_schema", REPO_ROOT / "scripts" / "export_edit_plan_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_schema_equals_the_generated_one():
    script = _script()
    committed = script.SCHEMA_PATH.read_text()
    assert script.SCHEMA_PATH == REPO_ROOT / "docs" / "schemas" / "edit-plan.v1.schema.json"
    assert committed == edit_plan.schema_json(), "stale schema; run `make schemas`"
    assert script.main(["--check"]) == 0  # what CI runs


def test_the_schema_is_a_valid_draft_2020_12_schema_that_agrees_with_the_validator(world):
    schema = edit_plan.schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator = Draft202012Validator(schema)
    good = [
        minimal(world),
        thirty_clip_plan(world.ids),
        {**minimal(world), "mode": "replace", "base_revision": None, "replace_unconditionally": True},
    ]
    for plan in good:
        assert list(validator.iter_errors(plan)) == []
        assert edit_plan.validate_plan(world.media, plan).ok
    # What the validator calls a field-level problem the schema rejects too (the rest needs the asset table).
    for mutate in (
        lambda p: p.update(extra=1),
        lambda p: p.update(schema_version=2),
        lambda p: p.pop("project"),
        lambda p: p["tracks"][0]["clips"][0].update(crop=1),
        lambda p: p["tracks"][0]["clips"][0].update(gain=9),
        lambda p: p["tracks"][0]["clips"][0].update(transition="wipe"),
        lambda p: p["tracks"][0]["clips"][0].pop("asset"),
        lambda p: p["tracks"][0].update(kind="movie"),
        lambda p: p["tracks"][0]["clips"].append({"kind": "text", "text": "x"}),
        lambda p: p["tracks"][0]["clips"].append({"kind": "text", "out": 3, "color": "red", "text": "x"}),
    ):
        plan = copy.deepcopy(minimal(world))
        mutate(plan)
        assert list(validator.iter_errors(plan)), plan
        assert not edit_plan.validate_plan(world.media, plan).ok


# --- glue from highlights ------------------------------------------------------------------------------------------------


def test_a_plan_built_from_suggest_highlights_output_applies():
    media = make_signal_store("memory", None, duration=300.0, path="talk.mp4")
    asset = plant(media, dense=True)
    found = highlights.suggest_highlights(media, asset.id, count=4, min_gap_seconds=10.0)
    assert len(found["candidates"]) >= 2
    store = InMemoryProjectStore()
    plan = edit_plan.highlights_to_plan(found, project="best-of")
    result = edit_plan.apply_plan(media, store, plan)
    clips = store.load_existing_timeline("best-of").tracks[0].clips
    starts = sorted(c["start"] for c in found["candidates"])
    assert [c.in_point for c in clips] == starts  # asset-relative seconds go straight in
    assert result["clips"] == len(found["candidates"]) and all(c.asset == asset.id for c in clips)

    ranked = edit_plan.highlights_to_plan(found, project="ranked", by_rank=True, crossfade=1.0)
    assert [c["in"] for c in ranked["tracks"][0]["clips"]] == [c["start"] for c in found["candidates"]]
    crossfaded = edit_plan.apply_plan(media, store, ranked)
    assert crossfaded["duration"] < sum(c["duration"] for c in found["candidates"])
    huge = edit_plan.highlights_to_plan(found, project="huge", crossfade=1000)  # capped so the plan stays valid
    assert edit_plan.validate_plan(media, huge).ok


def test_build_timeline_is_pure_and_raises_the_report(world):
    spy = SpyStore(world.store)
    project = edit_plan.build_timeline(world.media, minimal(world))
    assert isinstance(project, timeline.Project) and project.revision == 0
    with pytest.raises(PlanInvalidError):
        edit_plan.build_timeline(world.media, minimal(world, tracks=[]))
    assert spy.calls == [] and world.store.list_timelines() == []


def test_the_examples_in_the_docs_are_valid_against_the_schema():
    text = (REPO_ROOT / "docs" / "edit-plan.md").read_text()
    blocks = [b for b in text.split("```json\n")[1:]]
    plans = [json.loads(b.split("```")[0]) for b in blocks]
    plans = [p for p in plans if "schema_version" in p and "tracks" in p and "errors" not in p]
    assert len(plans) >= 4  # the format sketch, minimal, highlights reel, vertical short
    validator = Draft202012Validator(edit_plan.schema())
    for plan in plans:
        assert list(validator.iter_errors(plan)) == [], plan["project"]
    # Every code the engine can report is in the catalogue.
    source = (REPO_ROOT / "src" / "split_video" / "edit_plan.py").read_text()
    for code in sorted(set(re.findall(r'"(plan\.[a-z_.]+)"', source))):
        assert f"`{code}`" in text, code
