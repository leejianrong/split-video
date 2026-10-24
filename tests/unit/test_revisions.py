"""Project revisions: the ProjectStore contract, run against both stores, and concurrent writers.

Every project carries a revision (0 = never saved, +1 per save). A write with a `base_revision` is a
compare-and-swap; one without applies to the latest version. The local store makes both atomic across
processes with a lock file, so besides threads these tests run real subprocesses on one directory.
"""

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest

from split_video import timeline, timeline_ops, timeline_service
from split_video.errors import ProjectError, RevisionConflictError
from split_video.local_store import LOCK_DIR, LOCK_SUFFIX, LocalProjectStore
from split_video.store import (
    InMemoryProjectStore,
    RevisionChange,
    SavedSegment,
    subscribe_revisions,
)
from split_video.timeline import ProjectNotFoundError, TimelineError


@pytest.fixture(params=["memory", "local"])
def store(request, tmp_path):
    return InMemoryProjectStore() if request.param == "memory" else LocalProjectStore(tmp_path)


def seg(end: float, label: str = "") -> SavedSegment:
    return SavedSegment(start=0.0, end=end, label=label)


# --- split projects: the contract ----------------------------------------------


def test_a_never_saved_split_project_is_revision_0(store):
    loaded = store.load_split_versioned("v.mp4")
    assert (loaded.value, loaded.revision) == (None, 0)


def test_each_split_save_increments_the_revision_by_one(store):
    assert store.save_split("v.mp4", [seg(1.0)]) == 1
    assert store.save_split("v.mp4", [seg(2.0)]) == 2
    assert store.save_split("other.mp4", [seg(2.0)]) == 1  # per project
    loaded = store.load_split_versioned("v.mp4")
    assert loaded.revision == 2 and loaded.value == [seg(2.0)]
    assert store.load_split("v.mp4") == [seg(2.0)]


def test_a_split_save_with_the_current_base_revision_succeeds(store):
    assert store.save_split("v.mp4", [seg(1.0)], base_revision=0) == 1
    assert store.save_split("v.mp4", [seg(2.0)], base_revision=1) == 2


def test_a_stale_split_save_raises_with_both_revisions_and_writes_nothing(store):
    store.save_split("v.mp4", [seg(1.0)])
    store.save_split("v.mp4", [seg(2.0)])
    with pytest.raises(RevisionConflictError) as caught:
        store.save_split("v.mp4", [seg(9.0)], base_revision=1)
    assert (caught.value.current, caught.value.base, caught.value.project) == (2, 1, "v.mp4")
    assert "base_revision=2" in str(caught.value)  # retry guidance
    assert store.load_split_versioned("v.mp4").value == [seg(2.0)]
    assert store.load_split_versioned("v.mp4").revision == 2


def test_a_base_revision_ahead_of_the_project_is_a_conflict_too(store):
    with pytest.raises(RevisionConflictError) as caught:
        store.save_split("v.mp4", [seg(1.0)], base_revision=5)
    assert (caught.value.current, caught.value.base) == (0, 5)


def test_the_conflict_is_both_a_project_error_and_a_timeline_error():
    exc = RevisionConflictError("x", 3, 1)
    assert isinstance(exc, ProjectError) and isinstance(exc, TimelineError) and isinstance(exc, ValueError)


def test_update_split_applies_to_the_latest_and_returns_the_new_revision(store):
    seen = []

    def change(saved):
        seen.append(saved)
        return [*(saved or []), seg(float(len(seen)))]

    first = store.update_split("v.mp4", change)
    second = store.update_split("v.mp4", change)
    assert seen[0] is None and seen[1] == first.value
    assert (first.revision, second.revision) == (1, 2)
    assert store.load_split("v.mp4") == second.value == [seg(1.0), seg(2.0)]


def test_update_split_checks_the_base_before_running_change(store):
    store.save_split("v.mp4", [seg(1.0)])

    def change(saved):
        raise AssertionError("must not run for a stale write")

    with pytest.raises(RevisionConflictError):
        store.update_split("v.mp4", change, base_revision=0)


def test_update_split_writes_nothing_if_change_raises(store):
    store.save_split("v.mp4", [seg(1.0)])

    def change(saved):
        raise ProjectError("nope")

    with pytest.raises(ProjectError):
        store.update_split("v.mp4", change)
    assert store.load_split_versioned("v.mp4").revision == 1


# --- timeline projects: the contract ---------------------------------------------


def _with_track(project):
    new, _ = timeline_ops.add_track(project, "audio")
    return new


def test_created_timeline_is_revision_0_and_each_save_adds_one(store):
    assert store.create_timeline("show").revision == 0
    assert store.load_timeline("show").revision == 0
    project = store.load_timeline("show")
    assert store.save_timeline("show", project) == 1
    assert store.save_timeline("show", project, base_revision=1) == 2
    assert store.load_existing_timeline("show").revision == 2
    assert store.load_timeline("missing").revision == 0


def test_a_stale_timeline_save_raises_and_writes_nothing(store):
    store.create_timeline("show")
    project = store.load_timeline("show")
    store.save_timeline("show", project)
    with pytest.raises(RevisionConflictError) as caught:
        store.save_timeline("show", _with_track(project), base_revision=0)
    assert (caught.value.current, caught.value.base, caught.value.project) == (1, 0, "show")
    assert len(store.load_timeline("show").tracks) == 1


def test_update_timeline_stamps_the_new_revision_and_needs_an_existing_project(store):
    with pytest.raises(ProjectNotFoundError):
        store.update_timeline("nope", _with_track)
    store.create_timeline("show")
    updated = store.update_timeline("show", _with_track)
    assert updated.revision == 1 and len(updated.tracks) == 2
    assert store.load_timeline("show").revision == 1
    with pytest.raises(RevisionConflictError):
        store.update_timeline("show", _with_track, base_revision=0)
    assert store.update_timeline("show", _with_track, base_revision=1).revision == 2


def test_revision_is_not_part_of_project_equality(store):
    store.create_timeline("show")
    store.save_timeline("show", store.load_timeline("show"))
    assert store.load_timeline("show") == replace(store.load_timeline("show"), revision=0)


def test_service_edit_honours_base_revision(store):
    store.create_timeline("show")
    timeline_service.add_track(store, "show", "audio", base_revision=0)
    with pytest.raises(RevisionConflictError):
        timeline_service.add_track(store, "show", "audio", base_revision=0)
    assert timeline_service.add_track(store, "show", "audio").revision == 2  # no base: latest
    with pytest.raises(RevisionConflictError):  # looking a clip number up against an old revision fails too
        timeline_service.load_existing(store, "show", base_revision=1)


def test_revision_listeners_hear_each_committed_write_once(store):
    heard: list[RevisionChange] = []
    unsubscribe = subscribe_revisions(heard.append)
    try:
        store.save_split("v.mp4", [seg(1.0)])
        store.update_split("v.mp4", lambda saved: [seg(2.0)])
        store.create_timeline("show")
        store.update_timeline("show", _with_track)
        with pytest.raises(RevisionConflictError):
            store.save_split("v.mp4", [seg(3.0)], base_revision=0)  # refused: nothing to announce
    finally:
        unsubscribe()
    store.save_split("v.mp4", [seg(4.0)])  # unsubscribed
    assert heard == [
        RevisionChange("split", "v.mp4", 1),
        RevisionChange("split", "v.mp4", 2),
        RevisionChange("timeline", "show", 0),
        RevisionChange("timeline", "show", 1),
    ]


def test_write_listeners_hear_begin_before_the_commit_and_abort_on_failure(store):
    """SPL-106: `on_begin` fires before the stored project changes, then exactly one of commit/abort."""
    log: list[tuple[str, str, int | None]] = []

    def begin(kind, project_id, origin):
        log.append(("begin", project_id, store.load_split_versioned("v.mp4").revision))

    unsubscribe = subscribe_revisions(
        lambda c: log.append(("commit", c.project_id, c.revision)),
        on_begin=begin,
        on_abort=lambda kind, project_id, origin: log.append(("abort", project_id, None)),
    )
    try:
        store.save_split("v.mp4", [seg(1.0)])
        with pytest.raises(RevisionConflictError):
            store.save_split("v.mp4", [seg(2.0)], base_revision=0)
    finally:
        unsubscribe()
    assert log == [
        ("begin", "v.mp4", 0),  # nothing stored yet when it began
        ("commit", "v.mp4", 1),
        ("begin", "v.mp4", 1),
        ("abort", "v.mp4", None),
    ]


def test_a_failing_listener_does_not_fail_the_write(store):
    def boom(change):
        raise RuntimeError("subscriber bug")

    unsubscribe = subscribe_revisions(boom)
    try:
        assert store.save_split("v.mp4", [seg(1.0)]) == 1
    finally:
        unsubscribe()


# --- concurrent writers in one process ------------------------------------------------


WRITERS, EDITS = 6, 15


def _run_threads(target, count):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_threads_without_a_base_revision_lose_no_updates_split(store):
    def writer(n):
        for m in range(EDITS):
            store.update_split("v.mp4", lambda saved, n=n, m=m: [*(saved or []), seg(1.0, f"{n}-{m}")])

    _run_threads(writer, WRITERS)
    loaded = store.load_split_versioned("v.mp4")
    assert loaded.revision == WRITERS * EDITS
    assert {s.label for s in loaded.value} == {f"{n}-{m}" for n in range(WRITERS) for m in range(EDITS)}
    assert len(loaded.value) == WRITERS * EDITS


def test_threads_without_a_base_revision_lose_no_updates_timeline(store):
    store.create_timeline("show")

    def writer(n):
        for _ in range(EDITS):
            timeline_service.add_track(store, "show", "audio")

    _run_threads(writer, WRITERS)
    final = store.load_timeline("show")
    assert final.revision == WRITERS * EDITS
    assert len(final.tracks) == 1 + WRITERS * EDITS


def _race_two_writers(store, base):
    """Two threads release together, both based on `base`; returns how each fared."""
    outcomes = []
    gate = threading.Barrier(2)

    def writer(n):
        gate.wait()
        try:
            store.save_split("v.mp4", [seg(1.0, f"w{n}")], base_revision=base)
            outcomes.append("won")
        except RevisionConflictError as exc:
            assert exc.current == base + 1 and exc.base == base
            outcomes.append("conflict")

    _run_threads(writer, 2)
    return sorted(outcomes)


def test_two_threads_based_on_the_same_revision_exactly_one_wins(store):
    for round_ in range(20):
        assert _race_two_writers(store, round_) == ["conflict", "won"]
        assert store.load_split_versioned("v.mp4").revision == round_ + 1


def test_optimistic_retry_loops_lose_nothing_either(store):
    """Writers that read, then write with base_revision and retry on conflict, still add up."""

    def writer(n):
        for m in range(EDITS):
            while True:
                current = store.load_split_versioned("v.mp4")
                try:
                    store.save_split(
                        "v.mp4", [*(current.value or []), seg(1.0, f"{n}-{m}")], base_revision=current.revision
                    )
                    break
                except RevisionConflictError:
                    continue

    _run_threads(writer, WRITERS)
    loaded = store.load_split_versioned("v.mp4")
    assert loaded.revision == WRITERS * EDITS and len(loaded.value) == WRITERS * EDITS


# --- concurrent writers in separate processes (local store only) ------------------------

_WORKER = r"""
import json, sys, time
from pathlib import Path
from split_video import timeline_service
from split_video.errors import RevisionConflictError
from split_video.local_store import LocalProjectStore
from split_video.store import SavedSegment

mode, root, ident, count = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
store = LocalProjectStore(root)
go = root / "go"
while not go.exists():
    time.sleep(0.001)
won = conflicts = 0
if mode == "split-nobase":
    for m in range(count):
        store.update_split("v.mp4", lambda saved, m=m: [*(saved or []), SavedSegment(0.0, 1.0, f"{ident}-{m}")])
        won += 1
elif mode == "timeline-nobase":
    for m in range(count):
        timeline_service.add_track(store, "show", "audio")
        won += 1
elif mode == "split-retry":
    for m in range(count):
        while True:
            cur = store.load_split_versioned("v.mp4")
            try:
                store.save_split("v.mp4", [*(cur.value or []), SavedSegment(0.0, 1.0, f"{ident}-{m}")], cur.revision)
                won += 1
                break
            except RevisionConflictError:
                conflicts += 1
elif mode == "split-once":  # every process is based on revision 0: one wins
    try:
        store.save_split("v.mp4", [SavedSegment(0.0, 1.0, ident)], base_revision=0)
        won = 1
    except RevisionConflictError as exc:
        conflicts = 1
        assert (exc.current, exc.base) == (1, 0)
print(json.dumps({"won": won, "conflicts": conflicts}))
"""


def _spawn(tmp_path, mode, count, edits):
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, mode, str(tmp_path), str(i), str(edits)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(count)
    ]
    time.sleep(1.0)  # let every process finish importing and park on the start gate
    (tmp_path / "go").write_text("")
    results = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0
        results.append(json.loads(out))
    return results


def test_processes_without_a_base_revision_lose_no_updates_split(tmp_path):
    procs, edits = 4, 25
    _spawn(tmp_path, "split-nobase", procs, edits)
    loaded = LocalProjectStore(tmp_path).load_split_versioned("v.mp4")
    assert loaded.revision == procs * edits
    assert {s.label for s in loaded.value} == {f"{i}-{m}" for i in range(procs) for m in range(edits)}


def test_processes_without_a_base_revision_lose_no_updates_timeline(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.create_timeline("show")
    procs, edits = 4, 25
    _spawn(tmp_path, "timeline-nobase", procs, edits)
    final = store.load_timeline("show")
    assert final.revision == procs * edits
    assert len(final.tracks) == 1 + procs * edits


def test_processes_retrying_on_conflict_add_up(tmp_path):
    procs, edits = 3, 15
    results = _spawn(tmp_path, "split-retry", procs, edits)
    loaded = LocalProjectStore(tmp_path).load_split_versioned("v.mp4")
    assert sum(r["won"] for r in results) == loaded.revision == procs * edits
    assert len(loaded.value) == procs * edits


def test_of_processes_based_on_the_same_revision_exactly_one_wins(tmp_path):
    results = _spawn(tmp_path, "split-once", 3, 1)
    assert sum(r["won"] for r in results) == 1 and sum(r["conflicts"] for r in results) == 2
    assert LocalProjectStore(tmp_path).load_split_versioned("v.mp4").revision == 1


# --- the lock file ------------------------------------------------------------------------


def test_lock_files_sit_in_a_hidden_directory_next_to_project_files_and_are_not_projects(tmp_path):
    store = LocalProjectStore(tmp_path)
    (tmp_path / "gigs").mkdir()
    store.save_split("gigs/set.mp4", [seg(1.0)])
    store.create_timeline("show")
    assert (tmp_path / "gigs" / LOCK_DIR / f"set.mp4.split-video-project.json{LOCK_SUFFIX}").is_file()
    assert (tmp_path / LOCK_DIR / f"show.split-video-timeline.json{LOCK_SUFFIX}").is_file()
    assert not list(tmp_path.glob("*.lock")) and not list((tmp_path / "gigs").glob("*.lock"))
    assert store.list_timelines() == ["show"]
    store.save_timeline("show", store.load_timeline("show"))  # the lock file survives writes and is reused
    assert store.list_timelines() == ["show"]


def test_writes_still_work_when_the_lock_cannot_be_taken(tmp_path, monkeypatch):
    from split_video import local_store

    def refuse(fd, op):
        raise OSError("no locks on this filesystem")

    monkeypatch.setattr(local_store.fcntl, "flock", refuse)
    store = LocalProjectStore(tmp_path)
    assert store.save_split("v.mp4", [seg(1.0)]) == 1
    assert store.save_split("v.mp4", [seg(2.0)], base_revision=1) == 2


# --- files from before revisions ------------------------------------------------------------


def test_files_without_a_revision_load_as_revision_0_and_the_first_save_makes_it_1(tmp_path):
    (tmp_path / "old.mp4.split-video-project.json").write_text(
        '{"version":2,"source_file":"old.mp4","segments":[{"start":0,"end":5}]}'
    )
    (tmp_path / "old.split-video-timeline.json").write_text(
        '{"version":2,"tracks":[{"id":"t","kind":"video","clips":[]}]}'
    )
    store = LocalProjectStore(tmp_path)
    assert store.load_split_versioned("old.mp4").revision == 0
    assert store.load_existing_timeline("old").revision == 0
    store.save_split("old.mp4", store.load_split("old.mp4"), base_revision=0)
    assert store.load_split_versioned("old.mp4").revision == 1
    assert store.update_timeline("old", lambda p: p, base_revision=0).revision == 1


@pytest.mark.parametrize("bad", ['"x"', "-3", "1.5", "true", "null", "[1]"])
def test_a_garbage_revision_key_reads_as_0(tmp_path, bad):
    (tmp_path / "v.mp4.split-video-project.json").write_text(
        f'{{"version":2,"revision":{bad},"source_file":"v.mp4","segments":[{{"start":0,"end":5}}]}}'
    )
    assert LocalProjectStore(tmp_path).load_split_versioned("v.mp4").revision == 0


def test_a_damaged_timeline_keeps_its_revision_so_revisions_never_go_backwards(tmp_path):
    (tmp_path / "x.split-video-timeline.json").write_text('{"version":2,"revision":7,"tracks":[{"kind":"bogus"}]}')
    store = LocalProjectStore(tmp_path)
    assert store.load_timeline("x").revision == 7
    assert store.update_timeline("x", lambda p: p).revision == 8


def _old_load_split(text):
    """`LocalProjectStore.load_split`'s parsing as it was before revisions existed (commit 76a5b7f)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return [
            SavedSegment(
                start=float(s["start"]),
                end=float(s["end"]),
                label=str(s.get("label", "")),
                color=s.get("color"),
                included=bool(s.get("included", True)),
                export_name=s.get("export_name"),
            )
            for s in data["segments"]
        ]
    except (KeyError, TypeError, ValueError):
        return None


def test_older_readers_ignore_the_extra_revision_key(tmp_path):
    """The claim that needs no format version bump: pre-revision loaders pick fields by name."""
    store = LocalProjectStore(tmp_path)
    store.save_split("v.mp4", [seg(3.0, "a"), SavedSegment(3.0, 6.0, color="#aabbcc", included=False)])
    store.save_split("v.mp4", [seg(3.0, "a"), SavedSegment(3.0, 6.0, color="#aabbcc", included=False)])
    text = (tmp_path / "v.mp4.split-video-project.json").read_text()
    assert json.loads(text)["revision"] == 2
    assert _old_load_split(text) == store.load_split("v.mp4")

    store.create_timeline("show")
    timeline_service.add_track(store, "show", "audio")
    data = json.loads((tmp_path / "show.split-video-timeline.json").read_text())
    assert data["revision"] == 1 and data["version"] == 2
    old = timeline.from_dict(data)  # from_dict is unchanged since before revisions: it reads named keys only
    assert old == store.load_timeline("show") and old.revision == 0  # an old reader just doesn't see it


# --- peek_revisions (SPL-37): how the event stream notices other processes' writes -------------


@pytest.mark.parametrize("kind", ["local", "memory"])
def test_peek_revisions_reports_timelines_and_the_requested_split_projects(tmp_path, kind):
    store = LocalProjectStore(tmp_path) if kind == "local" else InMemoryProjectStore()
    assert store.peek_revisions() == {}
    store.create_timeline("a")
    store.create_timeline("b")
    store.update_timeline("b", lambda p: p)
    store.save_split("v.mp4", [SavedSegment(0.0, 1.0)])
    found = store.peek_revisions(["v.mp4", "unsaved.mp4"])
    assert found == {("timeline", "a"): 0, ("timeline", "b"): 1, ("split", "v.mp4"): 1, ("split", "unsaved.mp4"): 0}
    assert ("split", "v.mp4") not in store.peek_revisions()  # split projects only when asked for
    store.save_split("v.mp4", [SavedSegment(0.0, 2.0)])
    assert store.peek_revisions(["v.mp4"])[("split", "v.mp4")] == 2


def test_local_peek_sees_a_write_by_another_store_instance_and_survives_damage(tmp_path):
    store, other = LocalProjectStore(tmp_path), LocalProjectStore(tmp_path)
    store.create_timeline("t")
    assert store.peek_revisions()[("timeline", "t")] == 0
    for expected in range(1, 12):  # same-size files across 9 -> 10 included: the cache must not go stale
        other.update_timeline("t", lambda p: p)
        assert store.peek_revisions()[("timeline", "t")] == expected
    other.timeline_path("t").write_text("{ not json")
    assert store.peek_revisions()[("timeline", "t")] == 0  # unreadable counts as 0, never raises
    assert store.peek_revisions(["../escape.mp4"]) == {("timeline", "t"): 0}  # bad ids are left out


def test_revision_changes_name_the_store_that_wrote_them(tmp_path):
    seen = []
    unsubscribe = subscribe_revisions(seen.append)
    try:
        store = LocalProjectStore(tmp_path)
        memory = InMemoryProjectStore()
        store.create_timeline("t")
        memory.create_timeline("m")
    finally:
        unsubscribe()
    assert [c.origin for c in seen] == [store, memory]
