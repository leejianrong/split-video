"""`preview.preview_frame`: the composite at one time, against the export's own frame (SPL-69), with real ffmpeg.

The frame-by-frame match with the export lives in `test_golden_frames.py` (SPL-74).
"""

import subprocess
import time

import numpy as np
import pytest
from test_frames import decode

from split_video import assets, frames, preview_range, proxy
from split_video import timeline_ops as ops
from split_video.local_media_store import LocalMediaStore
from split_video.preview import preview_frame, window
from split_video.timeline import TimelineError, empty_project

# Seconds a preview may take in CI (it takes about a tenth of that locally; the card asks for one second).
CI_BUDGET = 3.0


def _make(path, lavfi):
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ]
    assert subprocess.run([*cmd, "-g", "30", str(path)], capture_output=True, check=False, timeout=120).returncode == 0


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """Smooth footage that changes with time (so a frame from the wrong moment does not match), 1080p at 30 fps."""
    root = tmp_path_factory.mktemp("preview")
    # `gradients` is not reproducible (two runs give two files), which made the proxy test below flaky
    _make(root / "a.mp4", "color=s=1920x1080:r=30:d=5,geq=r='X*255/W':g='128+100*sin(2*PI*(Y/H+T/2))':b='255-Y*255/H'")
    bands = "geq=r='128+100*sin(2*PI*(X/W+T))':g='X*255/W':b='Y*255/H'"
    _make(root / "b.mp4", f"color=s=1920x1080:r=30:d=4,{bands}")
    _make(root / "pip.mp4", "smptebars=size=640x360:rate=30:duration=4")
    return LocalMediaStore(root.resolve())


def _project(media):
    """The typical project: two main clips with a one second cross-fade (5 s in all), a graded first one, a
    picture-in-picture overlay at 0.5 s to 3.5 s and a title from 1 s to 3.5 s."""
    ids = {n: assets.register_path(media, f"{n}.mp4") for n in ("a", "b", "pip")}
    p = empty_project()
    main = p.tracks[0].id
    p, first = ops.add_clip(p, main, "a.mp4", 5.0, in_point=1.0, out_point=4.0, asset=ids["a"].id)
    p, second = ops.add_clip(p, main, "b.mp4", 4.0, out_point=3.0, asset=ids["b"].id)
    p = ops.set_transition(p, second.id, "crossfade", 1.0)
    p = ops.set_clip_properties(p, first.id, contrast=1.3, saturation=0.8)
    p, overlay = ops.add_track(p, "video")
    p, pip = ops.add_clip(p, overlay.id, "pip.mp4", 4.0, out_point=3.0, start=0.5, asset=ids["pip"].id)
    p = ops.set_clip_properties(p, pip.id, x=0.8, y=0.25, scale=0.3, opacity=0.8)
    p, titles = ops.add_track(p, "video")
    p, _ = ops.add_text_clip(p, titles.id, "Hello", 2.5, start=1.0, font_size=0.12, y=0.5)
    return p


@pytest.fixture(scope="module")
def project(workspace):
    return _project(workspace)


def test_window_keeps_only_what_is_on_screen_at_the_time(project):
    cut, w0 = window(project, 1.5)  # inside the first main clip, the overlay and the title
    assert w0 == 1.5 and [t.kind for t in cut.tracks] == ["video"] * 3
    main, overlay, titles = (t.clips for t in cut.tracks)
    assert [(c.in_point, c.start) for c in main] == [(2.5, 0.0)]  # 1.5 s into the clip that starts at 1.0 in source
    assert [(c.in_point, c.start) for c in overlay] == [(1.0, 0.0)]  # started at 0.5
    assert [(c.start, c.end) for c in titles] == [(0.0, 2.0)]  # runs to 3.5

    cut, w0 = window(project, 4.2)  # only the second main clip is left
    assert [len(t.clips) for t in cut.tracks] == [1, 0, 0] and w0 == 4.2

    cut, w0 = window(project, 2.4)  # in the cross-fade: both main clips, from where the fade starts
    assert w0 == 2.0 and [c.start for c in cut.tracks[0].clips] == [0.0, 0.0]


def test_range_window_keeps_everything_that_overlaps_it(project):
    cut, w0 = window(project, 2.5, 3.2)  # starts in the cross-fade, ends inside the overlay and the title
    assert w0 == 2.0 and [len(t.clips) for t in cut.tracks] == [2, 1, 1]
    cut, w0 = window(project, 3.6, 4.5)  # the overlay and the title are over by then; the second clip is cut whole
    assert w0 == 3.6 and [len(t.clips) for t in cut.tracks] == [1, 0, 0]
    assert [(c.in_point, c.start) for c in cut.tracks[0].clips] == [(1.6, 0.0)]


def test_a_range_chunk_of_the_typical_project_renders_and_cuts_to_the_range(workspace, project):
    plan = preview_range.plan_range(project, workspace, "typical.timeline", 1.5, 3.5, height=90, source="original")
    result = preview_range.render_range(workspace, plan)  # main track, cross-fade, overlay and title, from 1.5 s
    assert (result["start"], result["end"], result["width"], result["height"]) == (1.5, 3.5, 160, 90)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", result["path"]],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout  # fmt: skip
    assert float(out) == pytest.approx(2.0, abs=0.1)


def test_time_is_the_frame_the_export_has(workspace, project):
    frame = preview_frame(project, workspace, 1.51, width=160, source="original")
    assert frame.requested_time == 1.51 and frame.time == pytest.approx(1.5, abs=1e-3) and frame.duration == 5.0
    assert (frame.revision, frame.format) == (0, "jpeg")
    assert preview_frame(project, workspace, 5.0, width=160).time == pytest.approx(5 - 1 / 30, abs=1e-3)


def test_width_sets_the_size_by_the_exports_aspect_and_never_goes_above_it(workspace, project):
    frame = preview_frame(project, workspace, 1.0, width=320)
    assert (frame.width, frame.height) == (320, 180) and decode(frame.data)[:2] == (320, 180)
    assert preview_frame(project, workspace, 1.0).width == 640
    assert preview_frame(project, workspace, 1.0, width=4000).width == 1920
    vertical = preview_frame(project, workspace, 1.0, width=270, preset="vertical-1080")
    assert (vertical.width, vertical.height) == (270, 480)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"t": -1.0}, frames.FrameTimeError),
        ({"t": float("nan")}, frames.FrameTimeError),
        ({"t": 5.5}, frames.FrameTimeError),
        ({"t": 1.0, "width": 1}, frames.FrameArgumentError),
        ({"t": 1.0, "format": "gif"}, frames.FrameArgumentError),
        ({"t": 1.0, "source": "proxy"}, frames.FrameArgumentError),
        ({"t": 1.0, "quality": 0}, frames.FrameArgumentError),
    ],
)
def test_bad_arguments(workspace, project, kwargs, error):
    with pytest.raises(error):
        preview_frame(project, workspace, **kwargs)


def test_past_the_end_states_the_duration(workspace, project):
    with pytest.raises(frames.FrameTimeError, match=r"past the end of the timeline, which is 5 s long"):
        preview_frame(project, workspace, 6.0)


def test_an_empty_timeline_is_refused(workspace):
    with pytest.raises(TimelineError, match="no clips"):
        preview_frame(empty_project(), workspace, 0.0)


def test_nothing_is_written_to_the_workspace(workspace, project):
    before = sorted(p.name for p in workspace.root.rglob("*"))
    preview_frame(project, workspace, 1.5, width=160)
    assert sorted(p.name for p in workspace.root.rglob("*")) == before


def test_a_proxy_is_read_when_there_is_one_and_the_original_when_asked(workspace, project):
    a = assets.register_path(workspace, "a.mp4")
    proxy.prepare(workspace, a.id)  # only the first main clip has one
    mixed = preview_frame(project, workspace, 1.5, width=320, format="png")
    assert mixed.source == "mixed"
    exact = preview_frame(project, workspace, 1.5, width=320, format="png", source="original")
    assert exact.source == "original"
    diff = np.abs(decode(mixed.data)[2].astype(int) - decode(exact.data)[2].astype(int))
    assert diff.mean() <= 6.0  # 15 fps proxy: the picture is the same, within a proxy frame


def test_a_preview_is_fast_on_the_typical_project(workspace, project, capsys):
    """The card asks for under a second; CI runners get `CI_BUDGET`. The numbers are printed (`pytest -s`)."""
    a = assets.register_path(workspace, "a.mp4")
    proxy.prepare(workspace, a.id)
    for name in ("b", "pip"):
        proxy.prepare(workspace, assets.register_path(workspace, f"{name}.mp4").id)
    seen = {}
    for source in ("auto", "original"):
        preview_frame(project, workspace, 0.5, source=source)  # warm the disk cache and the probes
        runs = []
        for at in (1.5, 2.5, 4.5):
            started = time.perf_counter()
            frame = preview_frame(project, workspace, at, source=source)
            runs.append(time.perf_counter() - started)
            assert (frame.width, frame.height) == (640, 360)
        seen[source] = max(runs)
    with capsys.disabled():
        print(f"\npreview_frame, 1080p main+overlay+title, 640 wide: worst of 3 {seen}")
    assert max(seen.values()) < CI_BUDGET
