import itertools

import pytest

from split_video import project
from split_video.segments import Segment
from split_video.store import SavedSegment
from split_video.timefmt import parse_time


def segs(*bounds):
    """Contiguous segments from boundary times, e.g. segs(0, 10, 25)."""
    return [SavedSegment(start=a, end=b) for a, b in itertools.pairwise(bounds)]


def bounds(segments):
    return [segments[0].start, *[s.end for s in segments]]


def test_add_split_cuts_the_containing_segment_and_inherits_metadata():
    base = [SavedSegment(0, 10, label="A", color="#112233", included=False, export_name="custom"), SavedSegment(10, 20)]
    result, number = project.add_split(base, 4)
    assert number == 1
    assert bounds(result) == [0, 4, 10, 20]
    assert [s.label for s in result[:2]] == ["A", "A"]
    assert all(s.color == "#112233" and s.included is False for s in result[:2])
    # A custom export name can't apply to both halves.
    assert [s.export_name for s in result[:2]] == [None, None]
    assert result[2:] == base[1:]


@pytest.mark.parametrize("at", [0, 0.03, 20, 19.99, 10.02])
def test_add_split_rejects_positions_on_or_next_to_a_boundary(at):
    with pytest.raises(project.ProjectError):
        project.add_split(segs(0, 10, 20), at)


def test_remove_split_merges_and_keeps_the_earlier_segments_metadata():
    base = [SavedSegment(0, 10, label="first"), SavedSegment(10, 20, label="second"), SavedSegment(20, 30)]
    result = project.remove_split(base, 1)
    assert bounds(result) == [0, 20, 30]
    assert result[0].label == "first"


def test_remove_split_rejects_a_split_that_does_not_exist():
    with pytest.raises(project.ProjectError, match="single-segment"):
        project.remove_split(segs(0, 10), 1)
    with pytest.raises(project.ProjectError):
        project.remove_split(segs(0, 10, 20), 2)
    with pytest.raises(project.ProjectError):
        project.remove_split(segs(0, 10, 20), 0)


def test_move_split_moves_and_clamps_against_neighbors():
    base = segs(0, 10, 20, 30)
    moved, applied = project.move_split(base, 1, 12)
    assert applied == 12
    assert bounds(moved) == [0, 12, 20, 30]

    _, applied = project.move_split(base, 1, 500)
    assert applied == pytest.approx(20 - project.MIN_GAP)
    _, applied = project.move_split(base, 2, -5)
    assert applied == pytest.approx(10 + project.MIN_GAP)


def test_update_segment_changes_only_the_given_fields():
    base = [SavedSegment(0, 10, label="keep", color="#aabbcc", export_name="n")]
    result = project.update_segment(base, 1, included=False)
    assert result == [SavedSegment(0, 10, label="keep", color="#aabbcc", included=False, export_name="n")]

    result = project.update_segment(base, 1, label="  New  ", color="#FFAA00")
    assert result[0].label == "New"
    assert result[0].color == "#ffaa00"


def test_update_segment_clears_color_and_export_name_with_empty_string():
    base = [SavedSegment(0, 10, color="#aabbcc", export_name="n")]
    result = project.update_segment(base, 1, color="", export_name="")
    assert result[0].color is None
    assert result[0].export_name is None


@pytest.mark.parametrize("color", ["red", "#12345", "#gggggg", "123456"])
def test_update_segment_rejects_a_malformed_color(color):
    with pytest.raises(project.ProjectError):
        project.update_segment(segs(0, 10), 1, color=color)


def test_update_segment_rejects_an_unknown_segment():
    with pytest.raises(project.ProjectError):
        project.update_segment(segs(0, 10), 2, label="x")


def test_select_for_export_defaults_to_included_and_only_overrides_it():
    base = [SavedSegment(0, 1), SavedSegment(1, 2, included=False), SavedSegment(2, 3)]
    assert project.select_for_export(base) == [base[0], base[2]]
    assert project.select_for_export(base, [2]) == [base[1]]
    assert project.select_for_export(base, [3, 1, 3]) == [base[0], base[2]]
    with pytest.raises(project.ProjectError):
        project.select_for_export(base, [4])


def test_plan_export_names_files_and_detects_conflicts(tmp_path):
    source = tmp_path / "show.mp4"
    source.touch()
    plan = project.plan_export(source, [(0, 5, None), (5, 9, "Encore")])
    assert plan.filenames == ["01 - show.mp4", "Encore.mp4"]
    assert plan.output_dir == tmp_path / "show_split"

    plan.output_dir.mkdir()
    (plan.output_dir / "Encore.mp4").touch()
    with pytest.raises(project.ExportConflictError, match="Encore.mp4"):
        project.plan_export(source, [(0, 5, None), (5, 9, "Encore")])
    project.plan_export(source, [(0, 5, None), (5, 9, "Encore")], overwrite=True)


def test_plan_export_rejects_bad_segment_lists(tmp_path):
    source = tmp_path / "show.mp4"
    with pytest.raises(project.ProjectError, match="empty"):
        project.plan_export(source, [])
    with pytest.raises(project.ProjectError, match="overlap"):
        project.plan_export(source, [(0, 6, None), (5, 9, None)])
    with pytest.raises(project.ProjectError, match="unique"):
        project.plan_export(source, [(0, 5, "x"), (5, 9, "x")])


def test_contiguous_collapses_trimmed_silence_gaps_into_single_boundaries():
    result = project.contiguous([Segment(1, 0.5, 10), Segment(2, 14, 20), Segment(3, 25, 29)])
    assert bounds(result) == [0.5, 10, 20, 29]


@pytest.mark.parametrize(
    "text,expected",
    [("83.5", 83.5), ("1:23.5", 83.5), ("1:02:03", 3723), ("0", 0)],
)
def test_parse_time(text, expected):
    assert parse_time(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-5", "1:-5"])
def test_parse_time_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_time(text)
