"""Undo and redo (SPL-75): a linear history per project, kept by the stores, applied as a forward write."""

import json

import pytest
from test_edit_plan import World, minimal

from split_video import edit_ops, edit_plan, history, project, timeline, timeline_service
from split_video.errors import RevisionConflictError
from split_video.history import HistoryEmptyError
from split_video.local_store import LocalProjectStore
from split_video.timeline import ProjectNotFoundError

NAME = "reel"
VIDEO = "a.mp4"


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def dump(w):
    return timeline.to_dict(w.store.load_timeline(NAME))


def ops(w, *batch):
    return edit_ops.apply_ops(w.media, w.store, {"schema_version": 1, "project": NAME, "ops": list(batch)})


def clip(w, n=0):
    """The n-th media clip."""
    return [c.id for t in w.store.load_timeline(NAME).tracks for c in t.clips if c.kind == "media"][n]


def empty_track(w):
    return next(t.id for t in w.store.load_timeline(NAME).tracks if not t.clips)


def text_clip(w):
    return next(c.id for t in w.store.load_timeline(NAME).tracks for c in t.clips if c.kind == "text")


def steps(w):
    """One edit of every kind, in an order where each is valid; (label the next undo shows, the edit)."""
    a = w.ids["a"]
    return [
        ("Add clip", lambda: ops(w, {"op": "add_clip", "asset": a, "in": 0, "out": 30})),
        ("Add clip", lambda: timeline_service.add_clip(w.store, w.media, NAME, asset=w.ids["b"], out_point=30)),
        ("Trim clip", lambda: ops(w, {"op": "trim_clip", "clip": clip(w), "out": 25})),
        ("Split clip", lambda: ops(w, {"op": "split_clip", "clip": clip(w), "at": 10})),
        ("Move clip", lambda: ops(w, {"op": "move_clip", "clip": clip(w), "to_index": 2})),
        ("Set transition", lambda: ops(w, {"op": "set_transition", "clip": clip(w, 1), "transition": "crossfade", "duration": 1})),
        ("Set clip properties", lambda: ops(w, {"op": "set_clip_properties", "clip": clip(w), "gain": 0.5, "muted": True})),
        ("Add track", lambda: ops(w, {"op": "add_track", "kind": "video"})),
        ("Add text clip", lambda: ops(w, {"op": "add_text_clip", "track": empty_track(w), "text": "Hi", "duration": 2})),
        ("Set text properties", lambda: ops(w, {"op": "set_text_properties", "clip": text_clip(w), "text": "Hello"})),
        ("Place clip", lambda: ops(w, {"op": "place_clip", "clip": text_clip(w), "start": 4})),
        ("Set track flags", lambda: ops(w, {"op": "set_track_flags", "track": w.store.load_timeline(NAME).tracks[0].id, "muted": True})),
        ("Set canvas", lambda: ops(w, {"op": "set_canvas", "ratio": "9:16"})),
        ("Move track", lambda: ops(w, {"op": "move_track", "track": w.store.load_timeline(NAME).tracks[-1].id, "to_index": 0})),
        ("Remove clip", lambda: timeline_service.remove_clip(w.store, NAME, clip(w, 1), w.media)),
        ("Remove track", lambda: ops(w, {"op": "remove_track", "track": w.store.load_timeline(NAME).tracks[0].id})),
        ("Apply plan", lambda: edit_plan.apply_plan(w.media, w.store, minimal(w, mode="replace", project=NAME, replace_unconditionally=True))),
    ]  # fmt: skip


def test_undo_and_redo_restore_the_exact_project_after_every_edit_type(world):
    timeline_service.create_project(world.store, NAME)
    shots = [dump(world)]
    labels = []
    revisions = [0]
    for label, edit in steps(world):
        edit()
        shots.append(dump(world))
        labels.append(label)
        revisions.append(world.store.load_timeline(NAME).revision)
        assert world.store.history_state("timeline", NAME).undo_label == label
    assert revisions == sorted(set(revisions)), "every edit is a new revision"

    for i in range(len(shots) - 1, 0, -1):
        before = world.store.load_timeline(NAME).revision
        undone = world.store.step_history("timeline", NAME, "undo")
        assert undone.revision == before + 1, "undo is a forward write"
        assert dump(world) == shots[i - 1], f"undo of step {i}"
    shown = world.store.history_state("timeline", NAME)
    assert (shown.can_undo, shown.can_redo) == (False, True)

    for i in range(1, len(shots)):
        world.store.step_history("timeline", NAME, "redo")
        assert dump(world) == shots[i], f"redo of step {i}"
    assert not world.store.history_state("timeline", NAME).can_redo


def test_labels_follow_the_cursor(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "add_clip", "asset": world.ids["a"], "in": 0, "out": 30})
    ops(world, {"op": "split_clip", "clip": clip(world), "at": 10})
    assert world.store.history_state("timeline", NAME).to_dict() == {
        "can_undo": True,
        "can_redo": False,
        "undo_label": "Split clip",
        "redo_label": None,
    }
    world.store.step_history("timeline", NAME, "undo")
    shown = world.store.history_state("timeline", NAME)
    assert (shown.undo_label, shown.redo_label) == ("Add clip", "Split clip")


def test_a_batch_is_one_entry(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "add_clip", "asset": world.ids["a"], "in": 0, "out": 90})
    before = dump(world)
    first = clip(world)
    ops(world, {"op": "split_clip", "id": "p2", "clip": first, "at": 20}, {"op": "split_clip", "clip": "p2", "at": 50})
    assert len(world.store.load_timeline(NAME).tracks[0].clips) == 3
    assert world.store.history_state("timeline", NAME).undo_label == "Split clip"
    world.store.step_history("timeline", NAME, "undo")
    assert dump(world) == before


def test_a_new_edit_drops_the_redo_stack(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "add_clip", "asset": world.ids["a"], "in": 0, "out": 30})
    ops(world, {"op": "set_canvas", "ratio": "1:1"})
    world.store.step_history("timeline", NAME, "undo")
    assert world.store.history_state("timeline", NAME).can_redo
    ops(world, {"op": "set_canvas", "ratio": "9:16"})
    assert not world.store.history_state("timeline", NAME).can_redo
    with pytest.raises(HistoryEmptyError):
        world.store.step_history("timeline", NAME, "redo")


def test_nothing_to_undo_is_an_error(world):
    timeline_service.create_project(world.store, NAME)
    assert world.store.history_state("timeline", NAME) == history.HistoryState()
    with pytest.raises(HistoryEmptyError) as caught:
        world.store.step_history("timeline", NAME, "undo")
    assert caught.value.error_code == "history.empty"
    assert world.store.load_timeline(NAME).revision == 0
    with pytest.raises(ProjectNotFoundError):
        world.store.step_history("timeline", "missing", "undo")


def test_undo_carries_a_base_revision(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "add_clip", "asset": world.ids["a"], "in": 0, "out": 30})
    ops(world, {"op": "set_canvas", "ratio": "1:1"})
    with pytest.raises(RevisionConflictError) as caught:
        world.store.step_history("timeline", NAME, "undo", base_revision=1)
    assert caught.value.current == 2
    assert dump(world)["canvas"] == "1:1"
    world.store.step_history("timeline", NAME, "undo", base_revision=2)
    assert "canvas" not in dump(world)


def test_another_writers_edit_is_the_latest_entry(world):
    """Undo reverses the latest write whoever made it; a second store on the same files (the CLI) records too."""
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "add_clip", "asset": world.ids["a"], "in": 0, "out": 30})
    mine = dump(world)
    other = LocalProjectStore(world.media.root) if world.kind == "local" else world.store
    timeline_service.set_canvas(other, NAME, "1:1")
    world.store.step_history("timeline", NAME, "undo")
    assert dump(world) == mine


def test_a_change_the_history_did_not_see_disables_undo_until_the_next_edit(tmp_path):
    w = World("local", tmp_path)
    timeline_service.create_project(w.store, NAME)
    ops(w, {"op": "add_clip", "asset": w.ids["a"], "in": 0, "out": 30})
    path = w.store.timeline_path(NAME)
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "revision": data["revision"] + 1, "canvas": "1:1"}))
    assert not w.store.history_state("timeline", NAME).can_undo
    with pytest.raises(HistoryEmptyError):
        w.store.step_history("timeline", NAME, "undo")
    ops(w, {"op": "set_canvas", "ratio": "9:16"})
    w.store.step_history("timeline", NAME, "undo")
    assert dump(w)["canvas"] == "1:1"


def test_history_survives_reopening_the_store(tmp_path):
    w = World("local", tmp_path)
    timeline_service.create_project(w.store, NAME)
    ops(w, {"op": "add_clip", "asset": w.ids["a"], "in": 0, "out": 30})
    before = dump(w)
    ops(w, {"op": "set_canvas", "ratio": "1:1"})
    assert (tmp_path / ".split-video-history" / f"{NAME}.split-video-timeline.json.json").exists()

    reopened = LocalProjectStore(tmp_path)
    assert reopened.history_state("timeline", NAME).undo_label == "Set canvas"
    reopened.step_history("timeline", NAME, "undo")
    assert timeline.to_dict(reopened.load_timeline(NAME)) == before
    assert LocalProjectStore(tmp_path).history_state("timeline", NAME).redo_label == "Set canvas"


def test_recreating_a_project_starts_with_no_history(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "set_canvas", "ratio": "1:1"})
    if world.kind == "local":
        world.store.timeline_path(NAME).unlink()
    else:
        world.store._timelines.pop(NAME)
    timeline_service.create_project(world.store, NAME)
    assert not world.store.history_state("timeline", NAME).can_undo


def test_the_log_is_capped_by_entries_and_bytes(world, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 3)
    timeline_service.create_project(world.store, NAME)
    for ratio in ("1:1", "9:16", "16:9", "1:1", "9:16"):
        ops(world, {"op": "set_canvas", "ratio": ratio})
    undone = 0
    while world.store.history_state("timeline", NAME).can_undo:
        world.store.step_history("timeline", NAME, "undo")
        undone += 1
    assert undone == 3

    monkeypatch.setattr(history, "MAX_BYTES", 10)  # smaller than one state: the last edit stays undoable
    ops(world, {"op": "set_canvas", "ratio": "1:1"})
    ops(world, {"op": "set_canvas", "ratio": "9:16"})
    assert world.store.history_state("timeline", NAME).can_undo
    world.store.step_history("timeline", NAME, "undo")
    assert not world.store.history_state("timeline", NAME).can_undo


def test_undo_of_a_recordings_first_edit_goes_back_to_the_whole_recording(world):
    """A video's splits are its timeline project, and nothing is stored until the first edit."""
    whole = [(0.0, 300.0)]
    assert [(s.start, s.end) for s in project.current_segments(world.store, world.media, VIDEO)] == whole
    project.edit_segments(world.store, world.media, VIDEO, lambda segs: project.add_split(segs, 100.0)[0])
    project.edit_segments(world.store, world.media, VIDEO, lambda segs: project.add_split(segs, 200.0)[0])
    assert world.store.history_state("split", VIDEO).undo_label == "Edit splits"

    world.store.step_history("split", VIDEO, "undo")
    assert len(project.current_segments(world.store, world.media, VIDEO)) == 2
    world.store.step_history("split", VIDEO, "undo")
    assert [(s.start, s.end) for s in project.current_segments(world.store, world.media, VIDEO)] == whole
    assert not world.store.history_state("split", VIDEO).can_undo
    world.store.step_history("split", VIDEO, "redo")
    assert len(project.current_segments(world.store, world.media, VIDEO)) == 2


def test_video_timeline_ops_are_undoable(world):
    target = project.video_ops_target(world.store, world.media, VIDEO)
    clip_id = project.video_timeline(world.store, world.media, VIDEO).tracks[0].clips[0].id
    edit_ops.apply_ops(
        world.media,
        world.store,
        {"schema_version": 1, "project": VIDEO, "ops": [{"op": "split_clip", "clip": clip_id, "at": 50}]},
        target=target,
    )
    assert len(project.video_timeline(world.store, world.media, VIDEO).tracks[0].clips) == 2
    assert world.store.history_state("split", VIDEO).undo_label == "Split clip"
    world.store.step_history("split", VIDEO, "undo")
    assert len(project.video_timeline(world.store, world.media, VIDEO).tracks[0].clips) == 1


def test_describe_carries_the_history(world):
    timeline_service.create_project(world.store, NAME)
    ops(world, {"op": "set_canvas", "ratio": "1:1"})
    state = world.store.history_state("timeline", NAME)
    shown = timeline_service.describe(world.media, NAME, world.store.load_timeline(NAME), history=state)
    assert shown["history"]["undo_label"] == "Set canvas"
    assert "history" not in timeline_service.describe(world.media, NAME, world.store.load_timeline(NAME))
