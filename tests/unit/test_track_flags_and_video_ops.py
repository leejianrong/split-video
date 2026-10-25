"""Lane toggles (SPL-65): lock, hide and mute on a track, and edit ops on a recording's own timeline, on both stores."""

import json
from dataclasses import replace

import pytest
from test_edit_plan import World

from split_video import edit_ops, project, timeline, timeline_ops, timeline_service
from split_video.edit_ops import OpsInvalidError
from split_video.errors import RevisionConflictError
from split_video.store import SavedSegment

NAME = "reel"


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def doc(ops, **extra):
    return {"schema_version": 1, "project": NAME, "ops": ops, **extra}


def seeded(w):
    timeline_service.create_project(w.store, NAME)
    for key in ("a", "b"):
        timeline_service.add_clip(w.store, w.media, NAME, asset=w.ids[key], in_point=0, out_point=30)
    edit_ops.apply_ops(
        w.media,
        w.store,
        doc(
            [
                {"op": "add_track", "id": "ov", "kind": "video"},
                {"op": "add_text_clip", "id": "tx", "track": "ov", "text": "hi", "duration": 2},
                {"op": "add_track", "id": "au", "kind": "audio"},
                {"op": "add_clip", "id": "mu", "asset": w.ids["song"], "track": "au", "in": 0, "out": 10},
            ]
        ),
    )
    return w.store.load_timeline(NAME)


def flags(w, track, **given):
    return edit_ops.apply_ops(w.media, w.store, doc([{"op": "set_track_flags", "track": track, **given}]))


def test_flags_default_off_are_saved_only_when_set_and_round_trip(world):
    seeded(world)
    on_disk = timeline.to_dict(world.store.load_timeline(NAME))
    assert all("locked" not in t and "hidden" not in t and "muted" not in t for t in on_disk["tracks"])
    result = flags(world, "ov", locked=True, hidden=True)
    assert result["revision"] == world.store.load_timeline(NAME).revision
    track = next(t for t in world.store.load_timeline(NAME).tracks if t.id == "ov")
    assert (track.locked, track.hidden, track.muted) == (True, True, False)
    as_json = timeline.to_dict(world.store.load_timeline(NAME))
    assert next(t for t in as_json["tracks"] if t["id"] == "ov")["locked"] is True
    again = timeline.from_dict(json.loads(json.dumps(as_json)))
    assert next(t for t in again.tracks if t.id == "ov").hidden is True
    flags(world, "ov", locked=False)  # a locked track can be unlocked, and only the given flag changes
    track = next(t for t in world.store.load_timeline(NAME).tracks if t.id == "ov")
    assert (track.locked, track.hidden) == (False, True)


def test_a_locked_track_refuses_every_clip_edit_with_a_stable_code(world):
    seeded(world)
    main = world.store.load_timeline(NAME).tracks[0]
    flags(world, main.id, locked=True)
    first, second = main.clips[0].id, main.clips[1].id
    before = world.store.load_timeline(NAME)
    refused = [
        ({"op": "trim_clip", "clip": first, "out": 20}, "/ops/0/clip"),
        ({"op": "move_clip", "clip": first, "to_index": 1}, "/ops/0/clip"),
        ({"op": "remove_clip", "clip": second}, "/ops/0/clip"),
        ({"op": "set_clip_properties", "clip": first, "gain": 0.5}, "/ops/0/clip"),
        ({"op": "add_clip", "asset": world.ids["b"], "track": main.id}, "/ops/0/track"),
        ({"op": "remove_track", "track": main.id}, "/ops/0/track"),
    ]
    for op, path in refused:
        with pytest.raises(OpsInvalidError) as caught:
            edit_ops.apply_ops(world.media, world.store, doc([op]))
        assert [(e.path, e.code) for e in caught.value.report.errors] == [(path, "ops.track.locked")], op
    assert world.store.load_timeline(NAME) == before
    assert world.store.load_timeline(NAME).revision == before.revision
    flags(world, main.id, locked=False)
    edit_ops.apply_ops(world.media, world.store, doc([{"op": "trim_clip", "clip": first, "out": 20}]))


def test_locked_track_also_refuses_the_single_edit_twins_with_the_catalogue_code(world):
    from split_video.error_catalogue import describe

    seeded(world)
    main = world.store.load_timeline(NAME).tracks[0]
    timeline_service.set_track_flags(world.store, NAME, main.id, locked=True)
    with pytest.raises(timeline.TimelineError) as caught:
        timeline_service.trim_clip(world.store, world.media, NAME, main.clips[0].id, out_point=10)
    assert describe(caught.value).code == "track.locked"


def test_the_main_track_cannot_be_hidden_and_audio_tracks_are_muted_not_hidden(world):
    seeded(world)
    main = world.store.load_timeline(NAME).tracks[0]
    for op, code, path in (
        ({"op": "set_track_flags", "track": main.id, "hidden": True}, "ops.track.main_hidden", "/ops/0/hidden"),
        ({"op": "set_track_flags", "track": "au", "hidden": True}, "ops.track.hidden_audio", "/ops/0/hidden"),
    ):
        with pytest.raises(OpsInvalidError) as caught:
            edit_ops.apply_ops(world.media, world.store, doc([op]))
        assert [(e.path, e.code) for e in caught.value.report.errors] == [(path, code)]
    with pytest.raises(OpsInvalidError) as caught:  # no flag at all is a shape problem
        edit_ops.apply_ops(world.media, world.store, doc([{"op": "set_track_flags", "track": "ov"}]))
    assert [e.code for e in caught.value.report.errors] == ["ops.op.no_changes"]


def test_for_render_leaves_out_hidden_tracks_and_silences_muted_ones(world):
    project_ = seeded(world)
    plain = timeline_ops.for_render(project_)
    assert plain is project_  # no flags: untouched
    flagged = timeline_ops.set_track_flags(project_, "ov", hidden=True)
    flagged = timeline_ops.set_track_flags(flagged, "au", muted=True)
    flagged = timeline_ops.set_track_flags(flagged, project_.tracks[0].id, muted=True)
    seen = timeline_ops.for_render(flagged)
    assert [t.id for t in seen.tracks] == [project_.tracks[0].id, "au"]
    assert all(c.muted for t in seen.tracks for c in t.clips)
    assert not any(c.muted for t in flagged.tracks for c in t.clips)  # the stored project is not changed


def test_render_plan_honours_the_flags(world):
    from split_video import render

    project_ = seeded(world)
    infos = {c.source: world.media.probe(c.source) for t in project_.tracks for c in t.clips if c.kind == "media"}

    def audio_inputs(p):
        plan = render.build_plan(timeline_ops.for_render(p), infos, 640, 360, font="/f.ttf")
        return " ".join(plan.args) if hasattr(plan, "args") else str(plan)

    muted = timeline_ops.set_track_flags(project_, "au", muted=True)
    assert audio_inputs(muted) != audio_inputs(project_)
    hidden = timeline_ops.set_track_flags(project_, "ov", hidden=True)
    assert "drawtext" in audio_inputs(project_) and "drawtext" not in audio_inputs(hidden)


# --- ops on a recording's own timeline --------------------------------------------------------------------------------


def split_video_into_three(w):
    """The recording a.mp4 (300 s) split in three segments: the video's own timeline project."""
    segments = [SavedSegment(0, 100, label="Opening"), SavedSegment(100, 200, label="Song 1"), SavedSegment(200, 300)]
    project.save_segments(w.store, "a.mp4", segments, media=w.media)
    return project.video_timeline(w.store, w.media, "a.mp4")


def test_trim_and_move_on_the_video_timeline_are_engine_ops_with_revisions(world):
    before = split_video_into_three(world)
    target = project.video_ops_target(world.store, world.media, "a.mp4")
    main = before.tracks[0]
    rev = project.current_revision(world.store, "a.mp4")
    assert rev >= 1

    out = edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "ops": [{"op": "trim_clip", "clip": main.clips[1].id, "in": 110, "out": 190}]},
        base_revision=rev,
        target=target,
    )
    assert out["revision"] == rev + 1 and out["applied"] == 1 and out["project"] == "a.mp4"
    after = project.video_timeline(world.store, world.media, "a.mp4")
    assert [(c.in_point, c.out_point) for c in after.tracks[0].clips] == [(0, 100), (110, 190), (200, 300)]
    assert [c.start for c in after.tracks[0].clips] == [0, 100, 180]  # the track is magnetic: later clips close up
    assert [c.label for c in after.tracks[0].clips] == ["Opening", "Song 1", ""]
    assert project.current_revision(world.store, "a.mp4") == rev + 1

    moved = edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "ops": [{"op": "move_clip", "clip": main.clips[2].id, "to_index": 0}]},
        base_revision=rev + 1,
        target=target,
    )
    assert moved["revision"] == rev + 2
    ids = [c.id for c in project.video_timeline(world.store, world.media, "a.mp4").tracks[0].clips]
    assert ids == [main.clips[2].id, main.clips[0].id, main.clips[1].id]

    with pytest.raises(RevisionConflictError) as caught:  # a stale writer
        edit_ops.apply_ops(
            world.media,
            world.store,
            {"schema_version": 1, "ops": [{"op": "trim_clip", "clip": main.clips[0].id, "out": 50}]},
            base_revision=rev,
            target=target,
        )
    assert caught.value.current == rev + 2
    assert project.current_revision(world.store, "a.mp4") == rev + 2


def test_the_first_op_on_an_unsaved_recording_saves_it_as_one_clip(world):
    target = project.video_ops_target(world.store, world.media, "a.mp4")
    assert project.current_revision(world.store, "a.mp4") == 0
    result = edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "ops": [{"op": "trim_clip", "clip": target.load().tracks[0].clips[0].id, "out": 50}]},
        base_revision=0,
        target=target,
    )
    assert result["revision"] == 1
    clips = project.video_timeline(world.store, world.media, "a.mp4").tracks[0].clips
    assert [(c.in_point, c.out_point) for c in clips] == [(0, 50)]


def test_dry_run_on_the_video_timeline_writes_nothing_and_flags_lock_the_splits(world):
    split_video_into_three(world)
    target = project.video_ops_target(world.store, world.media, "a.mp4")
    rev = project.current_revision(world.store, "a.mp4")
    first = target.load().tracks[0].clips[0].id
    dry = edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "ops": [{"op": "trim_clip", "clip": first, "out": 40}]},
        dry_run=True,
        target=target,
    )
    assert dry["dry_run"] and dry["revision"] == rev + 1 and dry["timeline"]["tracks"][0]["clips"][0]["out"] == 40
    assert project.current_revision(world.store, "a.mp4") == rev

    main_id = target.load().tracks[0].id
    edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "ops": [{"op": "set_track_flags", "track": main_id, "locked": True}]},
        target=target,
    )
    saved = project.video_timeline(world.store, world.media, "a.mp4")
    assert saved.tracks[0].locked  # the flag survives the split view's rebuild of the main track
    with pytest.raises(timeline_ops.RuleViolation) as caught:  # and a locked main track refuses a re-split
        project.save_segments(world.store, "a.mp4", [SavedSegment(0, 300)], media=world.media)
    assert caught.value.reason == "track_locked"
    assert replace(saved, revision=0) == replace(project.video_timeline(world.store, world.media, "a.mp4"), revision=0)
