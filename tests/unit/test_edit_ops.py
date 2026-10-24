"""Edit-operation batches (SPL-50): a list of timeline edits applied atomically, with a dry run that never writes."""

import copy
import importlib.util
import json
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from test_edit_plan import FILES, SpyStore, World

from split_video import edit_ops, edit_plan, timeline_service
from split_video.edit_ops import OpsInvalidError
from split_video.errors import RevisionConflictError
from split_video.timeline import ProjectNotFoundError, TimelineError

REPO_ROOT = Path(__file__).resolve().parents[2]
NAME = "reel"


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    w = World(request.param, tmp_path)
    return w


def doc(ops, **extra):
    return {"schema_version": 1, "project": NAME, "ops": ops, **extra}


def seed(w):
    """A project with three clips on the main track (a 0-30, b 0-30, c 0-30) and nothing else."""
    timeline_service.create_project(w.store, NAME)
    for key in ("a", "b", "c"):
        timeline_service.add_clip(w.store, w.media, NAME, asset=w.ids[key], in_point=0, out_point=30)
    return w.store.load_timeline(NAME)


def codes(report):
    return [e.code for e in report.errors]


def failure(w, ops_doc, **kw):
    with pytest.raises(OpsInvalidError) as caught:
        edit_ops.apply_ops(w.media, w.store, ops_doc, **kw)
    return caught.value.report


def stored_bytes(w):
    """Everything a dry run must not touch: the project and the asset table, as stored."""
    table = json.dumps(w.media.load_asset_table(), sort_keys=True)
    if w.kind == "local":
        files = {
            p.name: p.read_bytes()
            for p in sorted(Path(w.media.root).iterdir())
            if p.is_file() and (p.suffix == ".json" or p.name.startswith("."))
        }
        return files, table
    return (w.store.load_timeline(NAME), w.store.load_timeline(NAME).revision), table


# --- done when: a failed batch leaves the project unchanged ------------------------------------------------------------


def ten_ops(w, bad_at=6, bad=None):
    a = w.ids["a"]
    ops = [
        {"op": "add_clip", "id": "n1", "asset": a, "in": 40, "out": 50},
        {"op": "trim_clip", "clip": "n1", "out": 45},
        {"op": "add_track", "id": "t2", "kind": "video"},
        {"op": "add_text_clip", "id": "x1", "track": "t2", "text": "Hello", "duration": 2},
        {"op": "set_text_properties", "clip": "x1", "color": "#ff0000"},
        {"op": "set_clip_properties", "clip": "n1", "gain": 0.5, "muted": True},
        {"op": "move_clip", "clip": "n1", "to_index": 0},
        {"op": "place_clip", "clip": "x1", "start": 5},
        {"op": "remove_clip", "clip": "n1"},
        {"op": "move_track", "track": "t2", "to_index": 0},
    ]
    ops[bad_at] = bad or {"op": "trim_clip", "clip": "ghost", "out": 5}
    return ops


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "trim_clip", "clip": "ghost", "out": 5},  # the project refuses it
        {"op": "trim_clip", "clip": "n1", "out": 5000},  # past the end of the source
        {"op": "set_clip_properties", "clip": "n1", "gain": 99},  # not valid in shape
    ],
)
def test_failed_batch_leaves_the_project_unchanged(world, bad):
    before_project = seed(world)
    before = stored_bytes(world)
    spy = SpyStore(world.store)
    report = failure(SimpleNamespace(media=world.media, store=spy), doc(ten_ops(world, bad_at=6, bad=bad)))
    assert report.errors and report.errors[0].path.startswith("/ops/6")
    assert report.ok is False
    # The store was asked for at most one update (state failures) and wrote nothing: bytes, revision and content agree.
    assert world.store.load_timeline(NAME) == before_project
    assert world.store.load_timeline(NAME).revision == 3
    assert stored_bytes(world) == before


def test_state_failure_reports_the_failing_index_and_how_far_it_got(world):
    seed(world)
    report = failure(world, doc(ten_ops(world)))
    assert [(e.path, e.code) for e in report.errors] == [("/ops/6/clip", "ops.clip.not_found")]
    assert report.applied == 6
    assert [r["ok"] for r in report.results] == [True] * 6 + [False]
    assert report.results[-1]["code"] == "ops.clip.not_found" and report.results[-1]["index"] == 6
    assert report.results[0]["ids"] == {"clip": "n1", "track": world.store.load_timeline(NAME).tracks[0].id}
    assert "never" not in report.errors[0].hint and report.errors[0].hint


def test_the_first_failing_op_stops_the_batch_by_default(world):
    seed(world)
    ops = [
        {"op": "trim_clip", "clip": "ghost1", "out": 5},
        {"op": "trim_clip", "clip": "ghost2", "out": 5},
    ]
    report = failure(world, doc(ops))
    assert [e.path for e in report.errors] == ["/ops/0/clip"] and len(report.results) == 1


def test_shape_problems_are_all_reported_together(world):
    seed(world)
    ops = [
        {"op": "trim_clip", "clip": "x", "out": -1},
        {"op": "teleport"},
        {"op": "remove_clip", "clip": "x", "colour": "red"},
        "nope",
        {"clip": "x"},
    ]
    report = failure(world, doc(ops))
    got = {(e.path, e.code) for e in report.errors}
    assert got == {
        ("/ops/0/out", "ops.field.range"),
        ("/ops/1/op", "ops.op.unknown"),
        ("/ops/2/colour", "ops.field.unknown"),
        ("/ops/3", "ops.field.type"),
        ("/ops/4/op", "ops.field.required"),
    }
    assert report.applied == 0 and report.results == []


# --- done when: a dry run never writes ---------------------------------------------------------------------------------


def test_dry_run_never_writes(world):
    seed(world)
    spy = SpyStore(world.store)
    before = stored_bytes(world)
    ops = ten_ops(world, bad_at=6, bad={"op": "move_clip", "clip": "n1", "to_index": 0})
    result = edit_ops.apply_ops(world.media, spy, doc(ops), dry_run=True)
    assert spy.calls == [], "a dry run must not call save / create / update on the store"
    assert stored_bytes(world) == before
    assert result["dry_run"] is True and result["revision"] == 4 and result["applied"] == 10
    # ... and not when it fails either
    bad = copy.deepcopy(ops)
    bad[5] = {"op": "trim_clip", "clip": "nope", "out": 1}
    with pytest.raises(OpsInvalidError):
        edit_ops.apply_ops(world.media, spy, doc(bad), dry_run=True, continue_on_error=True)
    assert spy.calls == [] and stored_bytes(world) == before


def test_dry_run_leaves_the_asset_table_alone_even_for_unregistered_files(world):
    seed(world)
    extra = "late.mp4"
    if world.kind == "memory":
        world.media.add(extra, b"late", info=FILES["a.mp4"][1])
    else:
        (Path(world.media.root) / extra).write_bytes(b"late")
    before = stored_bytes(world)
    ops = [{"op": "add_clip", "source": extra, "out": 5}]  # on disk but not an asset: the lookup must not register it
    report = failure(world, doc(ops), dry_run=True)
    assert codes(report) == ["plan.source.unknown"]
    assert stored_bytes(world) == before


def test_dry_run_result_is_the_real_result_plus_the_project(world):
    seed(world)
    ops = ten_ops(world, bad_at=6, bad={"op": "set_clip_properties", "clip": "n1", "x": 0.9})
    dry = edit_ops.apply_ops(world.media, world.store, doc(ops, meta={"by": "t"}), dry_run=True)
    real = edit_ops.apply_ops(world.media, world.store, doc(ops, meta={"by": "t"}))
    view = dry.pop("timeline")
    assert view["revision"] == 4 == real["revision"] and view["name"] == NAME
    assert {**dry, "dry_run": False} == real
    assert real["meta"] == {"by": "t"} and "timeline" not in real
    stored = timeline_service.describe(world.media, NAME, world.store.load_timeline(NAME))
    assert view == stored  # the predicted project is the project that was stored, ids included


def test_dry_run_reports_the_predicted_revision_and_conflicts(world):
    seed(world)
    ops = [{"op": "remove_clip", "clip": world.store.load_timeline(NAME).tracks[0].clips[0].id}]
    assert edit_ops.apply_ops(world.media, world.store, doc(ops), dry_run=True)["revision"] == 4
    with pytest.raises(RevisionConflictError):
        edit_ops.apply_ops(world.media, world.store, doc(ops), dry_run=True, base_revision=2)
    assert edit_ops.apply_ops(world.media, world.store, doc(ops), dry_run=True, base_revision=3)["revision"] == 4


# --- stale revisions ----------------------------------------------------------------------------------------------------


def test_stale_base_revision_writes_nothing(world):
    seed(world)
    before = stored_bytes(world)
    ops = [{"op": "add_track", "kind": "audio"}]
    for kwargs, ops_doc in (({"base_revision": 1}, doc(ops)), ({}, doc(ops, base_revision=2))):
        with pytest.raises(RevisionConflictError) as caught:
            edit_ops.apply_ops(world.media, world.store, ops_doc, **kwargs)
        assert caught.value.current == 3
    assert stored_bytes(world) == before
    ok = edit_ops.apply_ops(world.media, world.store, doc(ops), base_revision=3)
    assert ok["revision"] == 4
    ok = edit_ops.apply_ops(world.media, world.store, doc(ops, base_revision=4))
    assert ok["revision"] == 5


def test_base_revision_given_twice_must_agree(world):
    seed(world)
    report = failure(world, doc([{"op": "add_track", "kind": "audio"}], base_revision=3), base_revision=2)
    assert codes(report) == ["ops.base_revision.conflict"]


def test_missing_project(world):
    with pytest.raises(ProjectNotFoundError):
        edit_ops.apply_ops(world.media, world.store, doc([{"op": "add_track", "kind": "audio"}]))
    with pytest.raises(ProjectNotFoundError):
        edit_ops.apply_ops(world.media, world.store, doc([{"op": "add_track", "kind": "audio"}]), dry_run=True)


# --- one save, and the same as editing one by one ------------------------------------------------------------------------


def twenty_ops(ids):
    a, b, c = ids["a"], ids["b"], ids["c"]
    return [
        {"op": "add_clip", "id": "m4", "asset": a, "in": 100, "out": 112},
        {"op": "trim_clip", "clip": "m4", "in": 101, "out": 111},
        {"op": "move_clip", "clip": "m4", "to_index": 1},
        {"op": "set_transition", "clip": "m4", "transition": "crossfade", "duration": 1.5},
        {"op": "add_clip", "id": "m5", "asset": b, "in": 5, "out": 25, "index": 0},
        {"op": "set_transition", "clip": "m5", "transition": "cut"},
        {"op": "add_track", "id": "ov", "kind": "video"},
        {"op": "add_track", "id": "mu", "kind": "audio"},
        {"op": "add_text_clip", "id": "t1", "track": "ov", "text": "Intro", "duration": 3, "color": "#ffcc00"},
        {"op": "add_text_clip", "id": "t2", "track": "ov", "text": "Outro", "duration": 2, "start": 40, "y": 0.5},
        {"op": "set_text_properties", "clip": "t1", "text": "Intro!", "font_size": 0.1},
        {"op": "add_clip", "id": "o1", "asset": c, "track": "ov", "in": 0, "out": 8, "start": 10},
        {"op": "set_clip_properties", "clip": "o1", "scale": 0.3, "x": 0.8, "y": 0.2, "opacity": 0.9, "muted": True},
        {"op": "place_clip", "clip": "o1", "start": 20},
        {"op": "add_clip", "id": "s1", "asset": ids["song"], "track": "mu", "in": 0, "out": 60},
        {"op": "set_clip_properties", "clip": "s1", "gain": 0.4},
        {"op": "add_track", "id": "ov2", "kind": "video"},
        {"op": "place_clip", "clip": "o1", "track": "ov2", "start": 3},
        {"op": "move_track", "track": "ov2", "to_index": 1},
        {"op": "remove_clip", "clip": "t2"},
    ]


def shape(project):
    """A project without its (random) ids: what two edit paths must agree on."""
    return [
        (
            t.kind,
            t.magnetic,
            [
                (
                    c.kind,
                    c.asset,
                    round(c.in_point, 6),
                    round(c.out_point, 6),
                    round(c.start, 6),
                    c.transition,
                    round(c.transition_duration, 6),
                    c.gain,
                    c.muted,
                    c.x,
                    c.y,
                    c.scale,
                    c.opacity,
                    c.brightness,
                    c.contrast,
                    c.saturation,
                    c.text,
                    c.font_size,
                    c.color,
                )
                for c in t.clips
            ],
        )
        for t in project.tracks
    ]


class SingleEdits:
    """The same operations through the existing single-edit service functions (the API the CLI, MCP and editor use)."""

    def __init__(self, w) -> None:
        self.w = w
        self.ids: dict[str, str] = {}  # the batch's id -> the id this path got

    def ref(self, name):
        return self.ids.get(name, name)

    def _ids(self):
        project = self.w.store.load_timeline(NAME)
        return {c.id for t in project.tracks for c in t.clips}, {t.id for t in project.tracks}

    def run(self, op):
        w, svc, name = self.w, timeline_service, NAME
        store, media = w.store, w.media
        kind = op["op"]
        before_clips, before_tracks = self._ids()
        r = self.ref
        if kind == "add_clip":
            svc.add_clip(
                store, media, name, asset=op["asset"], track_id=r(op["track"]) if "track" in op else None,
                index=op.get("index"), start=op.get("start"), in_point=op.get("in", 0.0), out_point=op.get("out"),
            )  # fmt: skip
        elif kind == "trim_clip":
            svc.trim_clip(store, media, name, r(op["clip"]), in_point=op.get("in"), out_point=op.get("out"))
        elif kind == "move_clip":
            svc.move_clip(store, name, r(op["clip"]), op["to_index"], media)
        elif kind == "remove_clip":
            svc.remove_clip(store, name, r(op["clip"]), media)
        elif kind == "set_transition":
            svc.set_transition(store, name, r(op["clip"]), op["transition"], op.get("duration", 0.0), media)
        elif kind == "set_clip_properties":
            props = {k: v for k, v in op.items() if k not in ("op", "clip", "id")}
            svc.set_clip_properties(store, name, r(op["clip"]), media=media, **props)
        elif kind == "add_text_clip":
            options = {"start": op.get("start", 0.0), "font_size": op.get("font_size", 0.08)}
            options.update(color=op.get("color", "#ffffff"), x=op.get("x", 0.5), y=op.get("y", 0.85))
            svc.add_text_clip(store, name, r(op["track"]), op["text"], op["duration"], media=media, **options)
        elif kind == "set_text_properties":
            svc.set_text_properties(
                store, name, r(op["clip"]), media=media, text=op.get("text"), font_size=op.get("font_size"),
                color=op.get("color"),
            )  # fmt: skip
        elif kind == "add_track":
            svc.add_track(store, name, op["kind"], media)
        elif kind == "remove_track":
            svc.remove_track(store, name, r(op["track"]), media)
        elif kind == "move_track":
            svc.move_track(store, name, r(op["track"]), op["to_index"], media)
        elif kind == "place_clip":
            svc.place_clip(
                store, name, r(op["clip"]), start=op.get("start"),
                track_id=r(op["track"]) if "track" in op else None, media=media,
            )  # fmt: skip
        after_clips, after_tracks = self._ids()
        if "id" in op:
            new = (after_clips - before_clips) | (after_tracks - before_tracks)
            assert len(new) == 1
            self.ids[op["id"]] = new.pop()


def test_twenty_ops_apply_in_one_save_and_equal_the_same_edits_one_by_one(world, tmp_path):
    seed(world)
    (tmp_path / "twin").mkdir()
    twin = World(world.kind, tmp_path / "twin")
    seed(twin)
    ops = twenty_ops(world.ids)
    assert len(ops) == 20
    spy = SpyStore(world.store)
    result = edit_ops.apply_ops(world.media, spy, doc(ops))
    assert spy.calls == ["update_timeline"], "one batch is one save"
    assert result["applied"] == 20 and result["revision"] == 4 and len(result["results"]) == 20
    single = SingleEdits(twin)
    for op in twenty_ops(twin.ids):
        single.run(op)
    assert twin.store.load_timeline(NAME).revision == 3 + 20  # one revision per single edit
    assert shape(world.store.load_timeline(NAME)) == shape(twin.store.load_timeline(NAME))
    assert world.store.load_timeline(NAME).duration == pytest.approx(twin.store.load_timeline(NAME).duration)
    # The caller-chosen ids are the real ones.
    ids = {c.id for t in world.store.load_timeline(NAME).tracks for c in t.clips}
    assert {"m4", "m5", "t1", "o1", "s1"} <= ids and "t2" not in ids


def random_op(rng, project, ids, n):
    """A plausible op for the current project, sometimes deliberately wrong."""
    clips = [(t, c) for t in project.tracks for c in t.clips]
    tracks = list(project.tracks)
    pick = rng.choice
    kind = pick(
        ["add_clip", "add_clip", "trim_clip", "move_clip", "remove_clip", "set_transition", "set_clip_properties",
         "add_text_clip", "set_text_properties", "add_track", "remove_track", "move_track", "place_clip"]
    )  # fmt: skip
    asset = pick(["a", "b", "c", "song"])
    new_id = f"r{n}"
    if kind == "add_clip":
        op = {"op": "add_clip", "id": new_id, "asset": ids[asset], "in": rng.randint(0, 20), "out": rng.randint(5, 70)}
        t = pick(tracks)
        if rng.random() < 0.6:
            op["track"] = t.id
            if t.magnetic and rng.random() < 0.5:
                op["index"] = rng.randint(0, len(t.clips) + 1)
            elif not t.magnetic and rng.random() < 0.7:
                op["start"] = round(rng.uniform(0, 80), 2)
        return op
    if kind == "add_track":
        return {"op": "add_track", "id": new_id, "kind": pick(["video", "audio"])}
    if kind == "add_text_clip":
        return {"op": kind, "id": new_id, "track": pick(tracks).id, "text": pick(["hi", " "]), "duration": rng.choice([0.05, 1, 4]),
                "start": round(rng.uniform(0, 50), 1)}  # fmt: skip
    if kind in ("remove_track", "move_track"):
        op = {"op": kind, "track": pick(tracks).id}
        if kind == "move_track":
            op["to_index"] = rng.randint(0, len(tracks))
        return op
    if not clips:
        return {"op": "add_track", "id": new_id, "kind": "video"}
    track, clip = pick(clips)
    if kind == "trim_clip":
        op = {"op": kind, "clip": clip.id}
        if rng.random() < 0.7:
            op["in"] = round(rng.uniform(0, 40), 1)
        if rng.random() < 0.7 or "in" not in op:
            op["out"] = round(rng.uniform(0.5, 80), 1)
        return op
    if kind == "move_clip":
        return {"op": kind, "clip": clip.id, "to_index": rng.randint(0, len(track.clips))}
    if kind == "remove_clip":
        return {"op": kind, "clip": clip.id}
    if kind == "set_transition":
        return {
            "op": kind,
            "clip": clip.id,
            "transition": pick(["cut", "crossfade"]),
            "duration": rng.choice([0, 0.5, 2, 40]),
        }
    if kind == "set_clip_properties":
        return {"op": kind, "clip": clip.id, pick(["gain", "x", "opacity", "scale"]): rng.choice([0.5, 1, 2, 9])}
    if kind == "set_text_properties":
        return {"op": kind, "clip": clip.id, "color": pick(["#00ff00", "#123456"]), "font_size": pick([0.1, 0.2])}
    op = {"op": "place_clip", "clip": clip.id, "start": round(rng.uniform(0, 90), 1)}
    if rng.random() < 0.5:
        op["track"] = pick(tracks).id
    return op


@pytest.mark.parametrize("seed_value", range(6))
def test_random_op_sequences_behave_like_single_edits(seed_value, tmp_path):
    """Property-style: for random op sequences, applying each op as a one-op batch and through the single-edit
    functions succeeds and fails together and leaves the same project; the whole sequence as ONE batch ends in the
    same project again."""
    rng = random.Random(seed_value)
    one = World("memory", tmp_path)
    two = World("memory", tmp_path)
    batch_world = World("memory", tmp_path)
    for w in (one, two):
        seed(w)
    batch_world.store.save_timeline(NAME, one.store.load_timeline(NAME))  # the same ids as `one`
    single = SingleEdits(two)
    mine, theirs_ = one.store.load_timeline(NAME), two.store.load_timeline(NAME)
    for t1, t2 in zip(mine.tracks, theirs_.tracks, strict=True):  # the seeded ids are random: pair them by position
        single.ids[t1.id] = t2.id
        single.ids.update({c1.id: c2.id for c1, c2 in zip(t1.clips, t2.clips, strict=True)})
    accepted = []
    outcomes = {"ok": 0, "refused": 0}
    for n in range(60):
        op = random_op(rng, one.store.load_timeline(NAME), one.ids, n)
        before = one.store.load_timeline(NAME)
        try:
            edit_ops.apply_ops(one.media, one.store, doc([op]))
            ours = True
        except OpsInvalidError:
            ours = False
            assert one.store.load_timeline(NAME) == before  # a refused op changes nothing
        try:
            single.run(op)  # the same content, so the same asset ids in both worlds
            theirs = True
        except (TimelineError, ValueError):
            theirs = False
        # `place_clip` / `move_*` refusals can differ only in wording, never in outcome
        assert ours == theirs, (n, op)
        outcomes["ok" if ours else "refused"] += 1
        if ours:
            accepted.append(op)
        assert shape(one.store.load_timeline(NAME)) == shape(two.store.load_timeline(NAME)), (n, op)
    assert outcomes["ok"] > 15 and outcomes["refused"] > 3, outcomes
    edit_ops.apply_ops(batch_world.media, batch_world.store, doc(accepted))
    assert shape(batch_world.store.load_timeline(NAME)) == shape(one.store.load_timeline(NAME))


# --- continue_on_error ----------------------------------------------------------------------------------------------------


def test_continue_on_error_collects_independent_errors_and_skips_dependent_ones(world):
    seed(world)
    ids = world.ids
    ops = [
        {"op": "add_clip", "id": "bad1", "asset": "a_0000000000000000"},  # 0: unknown asset
        {"op": "trim_clip", "clip": "bad1", "out": 5},  # 1: depends on 0
        {"op": "add_track", "id": "t9", "kind": "video"},  # 2: fine
        {"op": "add_text_clip", "id": "x9", "track": "t9", "text": " ", "duration": 2},  # 3: blank text (shape)
        {"op": "add_text_clip", "id": "x8", "track": "t9", "text": "ok", "duration": 2},  # 4: fine
        {"op": "add_clip", "id": "bad2", "asset": ids["a"], "in": 0, "out": 9999},  # 5: past the end
        {"op": "set_clip_properties", "clip": "bad2", "gain": 1},  # 6: depends on 5
        {"op": "remove_clip", "clip": "ghost"},  # 7: independent error
        {"op": "set_clip_properties", "clip": "x8", "gain": 2},  # 8: fine
    ]
    report = failure(world, doc(ops), dry_run=True, continue_on_error=True)
    assert [(e.path, e.code) for e in report.errors] == [
        ("/ops/3/text", "plan.clip.text_required"),
        ("/ops/0/asset", "plan.asset.unknown"),
        ("/ops/1", "ops.skipped_dependency"),
        ("/ops/5/out", "plan.clip.out_of_range"),
        ("/ops/6", "ops.skipped_dependency"),
        ("/ops/7/clip", "ops.clip.not_found"),
    ]
    assert report.applied == 3 and report.dry_run is True
    assert [r["ok"] for r in report.results] == [False, False, True, False, True, False, False, False, True]
    assert report.timeline is not None  # the project after the ops that did apply
    assert [t["id"] for t in report.timeline["tracks"]][-1] == "t9"
    # Without continue_on_error the first failure stops it.
    stop = failure(world, doc(ops[:3] + ops[4:]), dry_run=True)
    assert [e.path for e in stop.errors] == ["/ops/0/asset"]


def test_continue_on_error_needs_a_dry_run(world):
    seed(world)
    ops = [{"op": "add_track", "kind": "audio"}]
    assert codes(failure(world, doc(ops), continue_on_error=True)) == ["ops.continue_on_error.needs_dry_run"]
    assert codes(failure(world, doc(ops, continue_on_error=True))) == ["ops.continue_on_error.needs_dry_run"]
    assert edit_ops.apply_ops(world.media, world.store, doc(ops, continue_on_error=True), dry_run=True)["applied"] == 1


def test_validate_ops_returns_the_report_of_a_dry_run(world):
    seed(world)
    good = edit_ops.validate_ops(world.media, world.store, doc([{"op": "add_track", "kind": "audio"}]))
    assert good.ok and good.applied == 1 and good.timeline["revision"] == 4
    bad = edit_ops.validate_ops(world.media, world.store, doc([{"op": "remove_clip", "clip": "x"}]))
    assert not bad.ok and codes(bad) == ["ops.clip.not_found"]
    assert world.store.load_timeline(NAME).revision == 3


# --- ids -------------------------------------------------------------------------------------------------------------------


def test_ids_are_given_generated_deterministically_and_unique(world):
    seed(world)
    ops = [{"op": "add_track", "kind": "video"}]
    dry1 = edit_ops.apply_ops(world.media, world.store, doc([ops[0]]), dry_run=True)
    dry2 = edit_ops.apply_ops(world.media, world.store, doc([ops[0]]), dry_run=True)
    real = edit_ops.apply_ops(world.media, world.store, doc([ops[0]]))
    generated = real["results"][0]["ids"]["track"]
    assert re.fullmatch(r"[0-9a-f]{8}", generated)
    assert dry1["results"] == dry2["results"] == real["results"], "a dry run predicts the ids the real run gives"
    again = edit_ops.apply_ops(world.media, world.store, doc([ops[0]]))
    assert again["results"][0]["ids"]["track"] != generated  # same batch, new project state: no collision


def test_id_problems(world):
    seed(world)
    existing = world.store.load_timeline(NAME).tracks[0].clips[0].id
    report = failure(world, doc([{"op": "add_clip", "id": existing, "asset": world.ids["a"], "out": 5}]))
    assert [(e.path, e.code) for e in report.errors] == [("/ops/0/id", "ops.id.duplicate")]
    report = failure(
        world,
        doc(
            [
                {"op": "add_track", "id": "dup", "kind": "audio"},
                {"op": "add_track", "id": "dup", "kind": "audio"},
                {"op": "add_track", "id": "has space", "kind": "audio"},
                {"op": "add_clip", "id": "dup", "asset": world.ids["a"], "out": 5},  # a clip: its own namespace
            ]
        ),
    )
    assert [(e.path, e.code) for e in report.errors] == [
        ("/ops/1/id", "ops.id.duplicate"),
        ("/ops/2/id", "ops.id.invalid"),
    ]


# --- every op, and the codes of what the project refuses -------------------------------------------------------------------


def test_every_op_has_a_handler_a_doc_row_and_a_single_edit_twin():
    text = (REPO_ROOT / "docs" / "edit-ops.md").read_text()
    for name, spec in edit_ops.OPS.items():
        assert f"`{name}`" in text, name
        assert name in edit_ops._HANDLERS
        assert hasattr(timeline_service, name), f"{name} has no timeline_service twin"
        for f in spec.fields:
            assert f"`{f.name}`" in text, (name, f.name)
    assert list(edit_ops.OPS) == [
        "add_clip", "trim_clip", "move_clip", "remove_clip", "set_transition", "set_clip_properties",
        "add_text_clip", "set_text_properties", "add_track", "remove_track", "move_track", "place_clip",
    ]  # fmt: skip


def refusals(w):
    """(ops that set the scene, the op that fails, the failing code and path)."""
    a = w.ids["a"]
    main = w.store.load_timeline(NAME).tracks[0]
    first = main.clips[0].id
    scene = [
        {"op": "add_track", "id": "ov", "kind": "video"},
        {"op": "add_track", "id": "au", "kind": "audio"},
        {"op": "add_text_clip", "id": "tx", "track": "ov", "text": "hi", "duration": 2},
        {"op": "add_clip", "id": "ovc", "asset": a, "track": "ov", "in": 0, "out": 5, "start": 10},
    ]
    main_id = main.id
    return scene, [
        ({"op": "add_clip", "asset": "nope"}, "plan.asset.invalid_id", "/asset"),
        ({"op": "add_clip", "asset": "a_ffffffffffffffff"}, "plan.asset.unknown", "/asset"),
        ({"op": "add_clip", "source": "../x.mp4"}, "plan.source.outside_root", "/source"),
        ({"op": "add_clip", "asset": a, "track": "zzz"}, "ops.track.not_found", "/track"),
        ({"op": "add_clip", "asset": w.ids["song"], "track": main_id}, "plan.clip.audio_only_on_video_track", ""),
        ({"op": "add_clip", "asset": w.ids["c"], "track": "au"}, "plan.clip.no_audio_for_audio_track", ""),
        ({"op": "add_clip", "asset": a, "in": 10, "out": 5}, "plan.clip.in_after_out", "/out"),
        ({"op": "add_clip", "asset": a, "out": 301}, "plan.clip.out_of_range", "/out"),
        ({"op": "add_clip", "asset": a, "in": 1, "out": 1.05}, "plan.clip.too_short", "/out"),
        ({"op": "add_clip", "asset": a, "start": 3}, "plan.clip.start_on_magnetic_track", "/start"),
        ({"op": "add_clip", "asset": a, "track": "ov", "index": 0}, "ops.clip.index_on_free_track", "/index"),
        ({"op": "add_clip", "asset": a, "index": 99}, "ops.index.out_of_range", "/index"),
        ({"op": "add_clip", "asset": a, "track": "ov", "start": 11, "out": 3}, "plan.clip.overlap", "/start"),
        ({"op": "trim_clip", "clip": "ghost", "out": 3}, "ops.clip.not_found", "/clip"),
        ({"op": "trim_clip", "clip": first, "out": 400}, "plan.clip.out_of_range", "/out"),
        ({"op": "trim_clip", "clip": first, "in": 40}, "plan.clip.in_after_out", "/out"),
        ({"op": "move_clip", "clip": first, "to_index": 50}, "ops.index.out_of_range", "/to_index"),
        ({"op": "move_clip", "clip": "ovc", "to_index": 0}, "ops.clip.move_on_free_track", "/clip"),
        ({"op": "remove_clip", "clip": "ghost"}, "ops.clip.not_found", "/clip"),
        (
            {"op": "set_transition", "clip": first, "transition": "crossfade", "duration": 1},
            "plan.clip.crossfade_first",
            "/transition",
        ),
        (
            {"op": "set_transition", "clip": main.clips[1].id, "transition": "crossfade"},
            "plan.clip.transition_duration",
            "/duration",
        ),
        (
            {"op": "set_transition", "clip": main.clips[1].id, "transition": "crossfade", "duration": 60},
            "plan.clip.transition_too_long",
            "/duration",
        ),
        (
            {"op": "set_transition", "clip": "ovc", "transition": "crossfade", "duration": 1},
            "plan.clip.crossfade_on_free_track",
            "/clip",
        ),
        (
            {"op": "add_text_clip", "track": main_id, "text": "x", "duration": 1},
            "plan.clip.text_on_main_track",
            "/track",
        ),
        ({"op": "add_text_clip", "track": "au", "text": "x", "duration": 1}, "plan.clip.text_on_audio_track", "/track"),
        ({"op": "add_text_clip", "track": "ov", "text": "x", "duration": 1, "start": 1}, "plan.clip.overlap", "/start"),
        ({"op": "set_text_properties", "clip": first, "text": "x"}, "ops.clip.not_text", "/clip"),
        ({"op": "move_track", "track": "ov", "to_index": 9}, "ops.index.out_of_range", "/to_index"),
        ({"op": "place_clip", "clip": "ovc", "track": "au"}, "ops.clip.track_kind_mismatch", "/track"),
        ({"op": "place_clip", "clip": "ovc", "track": main_id}, "ops.clip.dest_is_magnetic", "/track"),
        ({"op": "place_clip", "clip": "ovc", "start": -1}, "ops.field.range", "/start"),
        ({"op": "place_clip", "clip": "tx", "start": 10}, "plan.clip.overlap", "/start"),
    ]


def test_every_refusal_has_its_code_and_path(world):
    seed(world)
    scene, cases = refusals(world)
    edit_ops.apply_ops(world.media, world.store, doc(scene))
    before = stored_bytes(world)
    seen = set()
    for op, code, suffix in cases:
        report = failure(world, doc([op]))
        assert [(e.path, e.code) for e in report.errors] == [("/ops/0" + suffix, code)], op
        assert all(e.message for e in report.errors)
        seen.add(code)
    assert stored_bytes(world) == before
    assert len(seen) >= 25


def test_the_last_video_track_stays(world):
    seed(world)
    main = world.store.load_timeline(NAME).tracks[0].id
    report = failure(world, doc([{"op": "remove_track", "track": main}]))
    assert [(e.path, e.code) for e in report.errors] == [("/ops/0/track", "ops.track.last_video")]
    done = edit_ops.apply_ops(
        world.media,
        world.store,
        doc([{"op": "add_track", "id": "v2", "kind": "video"}, {"op": "remove_track", "track": main}]),
    )
    assert [t["id"] for t in done["tracks"]] == ["v2"]


def test_set_properties_and_warnings(world):
    seed(world)
    first = world.store.load_timeline(NAME).tracks[0].clips[1].id
    result = edit_ops.apply_ops(
        world.media,
        world.store,
        doc(
            [
                {"op": "set_transition", "clip": first, "transition": "cut", "duration": 2},
                {"op": "set_clip_properties", "clip": first, "brightness": -0.5, "contrast": 2, "saturation": 0},
            ]
        ),
    )
    assert [w["code"] for w in result["warnings"]] == ["plan.clip.transition_duration_ignored"]
    clip = world.store.load_timeline(NAME).tracks[0].clips[1]
    assert (clip.brightness, clip.contrast, clip.saturation, clip.transition) == (-0.5, 2, 0, "cut")


def test_source_path_names_an_existing_asset(world):
    seed(world)
    result = edit_ops.apply_ops(
        world.media, world.store, doc([{"op": "add_clip", "source": "b.mp4", "out": 7, "id": "s"}])
    )
    clip = world.store.load_timeline(NAME).tracks[0].clips[-1]
    assert clip.asset == world.ids["b"] and clip.id == "s" and clip.out_point == 7
    assert result["results"][0]["summary"].startswith("added clip s")


# --- strictness -------------------------------------------------------------------------------------------------------------


def test_unknown_ops_and_fields_are_rejected_with_the_allowed_list(world):
    seed(world)
    report = failure(world, doc([{"op": "explode"}, {"op": "trim_clip", "clip": "c", "out": 3, "fade": 2}]))
    unknown_op, unknown_field = report.errors
    assert (unknown_op.path, unknown_op.code) == ("/ops/0/op", "ops.op.unknown")
    assert all(name in unknown_op.hint for name in edit_ops.OPS)
    assert (unknown_field.path, unknown_field.code) == ("/ops/1/fade", "ops.field.unknown")
    assert "clip, in, out" in unknown_field.hint.replace("op, ", "")


@pytest.mark.parametrize(
    ("bad", "code", "path"),
    [
        ({"schema_version": 1, "project": NAME, "ops": [], "extra": 1}, "ops.field.unknown", "/extra"),
        ({"schema_version": 1, "project": NAME, "ops": []}, "ops.ops.empty", "/ops"),
        ({"schema_version": 1, "project": NAME}, "ops.field.required", "/ops"),
        ({"schema_version": 1, "ops": [{"op": "add_track", "kind": "audio"}]}, "ops.field.required", "/project"),
        (
            {"schema_version": 1, "project": "a/b", "ops": [{"op": "add_track", "kind": "audio"}]},
            "ops.project.name_invalid",
            "/project",
        ),
        ({"schema_version": 1, "project": NAME, "ops": "x"}, "ops.field.type", "/ops"),
        (
            {"project": NAME, "ops": [{"op": "add_track", "kind": "audio"}]},
            "ops.schema_version.missing",
            "/schema_version",
        ),
        ({"schema_version": 2, "project": NAME, "ops": []}, "ops.schema_version.unsupported", "/schema_version"),
        ({"schema_version": "1", "project": NAME, "ops": []}, "ops.schema_version.invalid", "/schema_version"),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "add_track", "kind": "film"}]},
            "ops.field.enum",
            "/ops/0/kind",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "trim_clip", "clip": "x"}]},
            "ops.op.no_changes",
            "/ops/0",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "set_clip_properties", "clip": "x"}]},
            "ops.op.no_changes",
            "/ops/0",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "add_clip"}]},
            "plan.clip.media_source_missing",
            "/ops/0",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "move_clip", "clip": "x", "to_index": 1.5}]},
            "ops.field.type",
            "/ops/0/to_index",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "move_clip", "clip": "x", "to_index": True}]},
            "ops.field.type",
            "/ops/0/to_index",
        ),
        (
            {
                "schema_version": 1,
                "project": NAME,
                "ops": [{"op": "set_clip_properties", "clip": "x", "gain": float("nan")}],
            },
            "ops.field.type",
            "/ops/0/gain",
        ),
        (
            {
                "schema_version": 1,
                "project": NAME,
                "ops": [{"op": "add_text_clip", "track": "t", "text": "x", "duration": 0.01}],
            },
            "ops.field.range",
            "/ops/0/duration",
        ),
        (
            {"schema_version": 1, "project": NAME, "ops": [{"op": "add_text_clip", "track": "t", "text": "x"}]},
            "ops.field.required",
            "/ops/0/duration",
        ),
    ],
)
def test_document_strictness(world, bad, code, path):
    seed(world)
    report = failure(world, bad)
    assert (path, code) in {(e.path, e.code) for e in report.errors}, report.to_dict()
    assert world.store.load_timeline(NAME).revision == 3


def test_not_an_object_and_too_many_ops(world):
    seed(world)
    assert codes(failure(world, [1])) == ["ops.document.not_object"]
    many = [{"op": "add_track", "kind": "audio"}] * (edit_ops.MAX_OPS + 1)
    assert codes(failure(world, doc(many))) == ["ops.ops.too_many"]
    assert codes(
        failure(
            world, {"schema_version": 1, "project": NAME, "ops": [{"op": "add_track", "kind": "audio", "x": object()}]}
        )
    ) == ["ops.document.not_json"]


def test_migration_seam(monkeypatch, world):
    seed(world)
    monkeypatch.setitem(
        edit_ops.MIGRATIONS,
        0,
        lambda d: {"schema_version": 0, "project": d["project"], "ops": [{"op": "add_track", "kind": "audio"}]},
    )
    monkeypatch.setattr(edit_ops, "SCHEMA_VERSION", 1)
    assert edit_ops.migrate_ops({"schema_version": 0, "project": NAME})["ops"][0]["kind"] == "audio"
    done = edit_ops.apply_ops(world.media, world.store, {"schema_version": 0, "project": NAME})
    assert done["applied"] == 1
    monkeypatch.setitem(edit_ops.MIGRATIONS, 0, lambda d: 1 / 0)
    assert codes(failure(world, {"schema_version": 0, "project": NAME})) == ["ops.schema_version.migration_failed"]


# --- the published schema --------------------------------------------------------------------------------------------------


def test_committed_schema_equals_the_generated_one():
    spec = importlib.util.spec_from_file_location(
        "export_edit_ops_schema", REPO_ROOT / "scripts" / "export_edit_ops_schema.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.SCHEMA_PATH == REPO_ROOT / "docs" / "schemas" / "edit-ops.v1.schema.json"
    assert script.SCHEMA_PATH.read_text() == edit_ops.schema_json(), "stale schema; run `make schemas`"
    assert script.main(["--check"]) == 0


def test_the_schema_accepts_what_the_engine_accepts_and_rejects_what_it_rejects_in_shape(world):
    validator = Draft202012Validator(edit_ops.schema())
    Draft202012Validator.check_schema(edit_ops.schema())
    good = doc(twenty_ops(world.ids))
    assert not list(validator.iter_errors(good))
    for bad, _code, _path in [
        (doc([{"op": "explode"}]), 0, 0),
        (doc([{"op": "trim_clip", "clip": "c"}]), 0, 0),
        (doc([{"op": "trim_clip", "clip": "c", "out": 3, "fade": 1}]), 0, 0),
        (doc([{"op": "add_clip"}]), 0, 0),
        (doc([{"op": "set_clip_properties", "clip": "c", "gain": 9}]), 0, 0),
        (doc([]), 0, 0),
        ({**doc([{"op": "add_track", "kind": "audio"}]), "schema_version": 2}, 0, 0),
    ]:
        assert list(validator.iter_errors(bad)), bad
    # the schema and the validator agree on the ops: every op is a definition with strict fields
    schema = edit_ops.schema()
    assert set(schema["$defs"]) == set(edit_ops.OPS)
    for name, spec in edit_ops.OPS.items():
        body = schema["$defs"][name]
        assert body["additionalProperties"] is False and body["properties"]["op"]["const"] == name
        assert set(body["properties"]) == {f.name for f in spec.fields}


def test_every_error_code_is_documented():
    text = (REPO_ROOT / "docs" / "edit-ops.md").read_text()
    source = (REPO_ROOT / "src" / "split_video" / "edit_ops.py").read_text()
    used = set(re.findall(r'"((?:ops|plan)\.[a-z_]+\.[a-z_]+)"', source))
    used |= {f"ops.field.{k}" for k in ("unknown", "required", "type", "value", "enum", "range", "pattern", "length")}
    used |= {"ops.project.mismatch"}
    missing = sorted(c for c in used if f"`{c}`" not in text)
    assert not missing, f"codes missing from docs/edit-ops.md: {missing}"


# --- plan and ops share their result and report types ----------------------------------------------------------------------


def test_plan_and_ops_dry_runs_share_shape_and_report_type(world):
    plan = {
        "schema_version": 1,
        "project": "p2",
        "tracks": [{"kind": "video", "clips": [{"asset": world.ids["a"], "in": 0, "out": 10}]}],
    }
    plan_dry = edit_plan.apply_plan(world.media, world.store, plan, dry_run=True)
    seed(world)
    ops_dry = edit_ops.apply_ops(world.media, world.store, doc([{"op": "add_track", "kind": "audio"}]), dry_run=True)
    shared = {"project", "revision", "action", "dry_run", "clips", "duration", "tracks", "warnings", "timeline"}
    assert shared <= set(plan_dry) and shared <= set(ops_dry)
    assert set(plan_dry["timeline"]) == set(ops_dry["timeline"]) == {"name", "revision", "duration", "tracks"}
    assert plan_dry["tracks"][0].keys() == ops_dry["tracks"][0].keys()
    assert issubclass(edit_ops.OpsReport, edit_plan.PlanReport) and issubclass(OpsInvalidError, TimelineError)
    bad_plan = edit_plan.validate_plan(world.media, {"schema_version": 1, "project": "x", "tracks": []})
    bad_ops = failure(world, doc([{"op": "teleport"}]))
    assert type(bad_plan.errors[0]) is type(bad_ops.errors[0])
    assert (
        bad_plan.errors[0].to_dict().keys() == bad_ops.errors[0].to_dict().keys() == {"path", "code", "message", "hint"}
    )


# --- volume and timing -----------------------------------------------------------------------------------------------------


def test_a_200_op_batch(world, capsys):
    seed(world)
    a, b = world.ids["a"], world.ids["b"]
    ops = [{"op": "add_track", "id": "ov", "kind": "video"}]
    for i in range(67):
        ops.append({"op": "add_clip", "id": f"m{i}", "asset": a if i % 2 else b, "in": i % 20, "out": i % 20 + 5})
        ops.append({"op": "set_clip_properties", "clip": f"m{i}", "gain": 0.5 + (i % 3) / 10})
        ops.append(
            {"op": "add_text_clip", "id": f"x{i}", "track": "ov", "text": f"T{i}", "duration": 1, "start": i * 2}
        )
    ops = ops[:200]
    assert len(ops) == 200
    t0 = time.perf_counter()
    dry = edit_ops.apply_ops(world.media, world.store, doc(ops), dry_run=True)
    dry_ms = (time.perf_counter() - t0) * 1000
    spy = SpyStore(world.store)
    t0 = time.perf_counter()
    real = edit_ops.apply_ops(world.media, spy, doc(ops))
    real_ms = (time.perf_counter() - t0) * 1000
    assert spy.calls == ["update_timeline"] and real["applied"] == 200 == dry["applied"]
    print(f"\n200-op batch ({world.kind}): dry run {dry_ms:.0f} ms, apply {real_ms:.0f} ms")
    assert real_ms < 5000


def test_the_run_never_changes_the_callers_document(world):
    seed(world)
    ops = doc(twenty_ops(world.ids), meta={"k": 1})
    snapshot = copy.deepcopy(ops)
    edit_ops.apply_ops(world.media, world.store, ops, dry_run=True)
    edit_ops.apply_ops(world.media, world.store, ops)
    assert ops == snapshot
