"""`preview.preview_frame`: the composite at one time, against the export's own frame (SPL-69), with real ffmpeg.

Tolerance of the match test: the preview is a lossless PNG of the filter graph's output; the export's frame went
through libx264 at its default crf (23), so it differs by the encoder's loss. The mean absolute difference over all
channels must stay at or under `MEAN_TOLERANCE` (of 255) and no more than `BAD_SHARE` of the samples may differ by
more than `PIXEL_TOLERANCE`; a wrong overlay, text, crop, colour or fade position moves both far past that, and
the frame has to be at least `SEPARATION` times closer than the export's frame 0.2 s later. Of the export's
frames at the time and one frame later the closer one counts: after a cross-fade ffmpeg's `xfade` can show the
incoming clip one frame late (4.4 does), while the preview shows it on time.
"""

import subprocess
import time

import numpy as np
import pytest
from test_frames import decode

from split_video import assets, frames, proxy
from split_video import timeline_ops as ops
from split_video.local_media_store import LocalMediaStore
from split_video.preview import preview_frame, window
from split_video.render import render
from split_video.timeline import TimelineError, empty_project

MEAN_TOLERANCE = 4.0
PIXEL_TOLERANCE = 24
BAD_SHARE = 0.03
SEPARATION = 3
EXPORT_SIZE = (480, 270)
# Seconds a preview may take in CI (it takes about a tenth of that locally; the card asks for one second).
CI_BUDGET = 3.0


def _make(path, lavfi):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", lavfi, "-pix_fmt", "yuv420p", "-c:v", "libx264"]
    assert subprocess.run([*cmd, "-g", "30", str(path)], capture_output=True, check=False, timeout=120).returncode == 0


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """Smooth footage that changes with time (so a frame from the wrong moment does not match), 1080p at 30 fps."""
    root = tmp_path_factory.mktemp("preview")
    _make(root / "a.mp4", "gradients=seed=3:speed=0.5:size=1920x1080:rate=30:duration=5")
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


@pytest.fixture(scope="module")
def exported(workspace, project, tmp_path_factory):
    render(project, workspace, "export.mp4", width=EXPORT_SIZE[0], height=EXPORT_SIZE[1])
    return workspace


def _export_frame(media, at):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at}", "-i", str(media.root / "export.mp4")]
    cmd += ["-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    return decode(out)[2].astype(int)


@pytest.mark.parametrize("at", [0.5, 1.5, 2.5, 2.9, 4.0, 4.9])
def test_preview_matches_the_export_frame(exported, project, at):
    # 0.5 main + overlay, 1.5 + title, 2.5 and 2.9 in the cross-fade, 4.0 and 4.9 second clip only
    frame = preview_frame(project, exported, at, width=EXPORT_SIZE[0], format="png", source="original")
    assert (frame.width, frame.height) == EXPORT_SIZE and frame.time == pytest.approx(at, abs=1 / 30)
    mine = decode(frame.data)[2].astype(int)
    best = min((np.abs(mine - _export_frame(exported, frame.time + k / 30)) for k in (0, 1)), key=np.mean)
    away = np.abs(mine - _export_frame(exported, frame.time + (0.2 if at < 4 else -0.2)))
    assert best.mean() <= MEAN_TOLERANCE, best.mean()
    assert (best > PIXEL_TOLERANCE).mean() <= BAD_SHARE, (best > PIXEL_TOLERANCE).mean()
    assert best.mean() * SEPARATION < away.mean()


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
