"""Golden frames (SPL-74): the preview and the export must show the same picture.

One small project per feature (overlay, cross-fade, colour grade, text) is exported once; the preview's frame at
several times is compared with the export's by `golden.measure` under that case's tolerance. The mutation tests
change one thing on the preview side and demand the harness reports it, which shows each tolerance is tight enough
to catch a real divergence. Both renderers share `render.build_plan`, so a change there moves them together: the
pinned-colour tests compare against absolute values to catch that. See "Golden-frame tests" in docs/compose.md.
"""

from dataclasses import replace

import numpy as np
import pytest
from golden import SIZE, Tolerance, export, export_frame, make_source, measure
from test_frames import decode

from split_video import assets
from split_video import timeline_ops as ops
from split_video.local_media_store import LocalMediaStore
from split_video.preview import preview_frame
from split_video.render import render
from split_video.timeline import empty_project

GRADED = (136, 70, 56)  # (200, 60, 40) at contrast 1.3, saturation 0.5
W = "color=s=640x360:r=30:d=3,"
SOURCES = {
    "a": W + "geq=r='128+100*sin(2*PI*(X/W+T/3))':g='X*255/W':b='Y*255/H'",
    "b": W + "geq=r='Y*255/H':g='128+100*sin(2*PI*(Y/H+T/3))':b='255-X*255/W'",
    "bars": "smptebars=size=320x180:rate=30:duration=3",
    "flat": "color=c=0xC83C28:s=640x360:r=30:d=3",  # (200, 60, 40)
    "blue": "color=c=0x2850C8:s=320x180:r=30:d=3",  # (40, 80, 200)
    "black": "color=c=black:s=640x360:r=30:d=3",
}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("golden")
    for name, lavfi in SOURCES.items():
        make_source(root / f"{name}.mp4", lavfi)
    return LocalMediaStore(root.resolve())


def _clip(media, p, track, name, duration, **kw):
    a = assets.register_path(media, f"{name}.mp4")
    return ops.add_clip(p, track, f"{name}.mp4", 3.0, out_point=duration, asset=a.id, **kw)


def overlay(media, x=0.75, y=0.3, scale=0.35, opacity=0.7):
    p = empty_project()
    p, _ = _clip(media, p, p.tracks[0].id, "a", 3.0)
    p, over = ops.add_track(p, "video")
    p, pip = _clip(media, p, over.id, "bars", 2.0, start=0.5)
    return ops.set_clip_properties(p, pip.id, x=x, y=y, scale=scale, opacity=opacity)


def crossfade(media, duration=1.0):
    p = empty_project()
    main = p.tracks[0].id
    p, _ = _clip(media, p, main, "a", 2.0)
    p, second = _clip(media, p, main, "b", 2.0)
    return ops.set_transition(p, second.id, "crossfade", duration)  # 3 s: the fade is from 1.0 to 2.0


def grade(media, contrast=1.4, saturation=0.6, brightness=0.1):
    p = empty_project()
    p, clip = _clip(media, p, p.tracks[0].id, "a", 3.0)
    return ops.set_clip_properties(p, clip.id, contrast=contrast, saturation=saturation, brightness=brightness)


def title(media, text="Golden 123", y=0.5, font_size=0.15):
    p = empty_project()
    p, _ = _clip(media, p, p.tracks[0].id, "a", 3.0)
    p, track = ops.add_track(p, "video")
    p, _ = ops.add_text_clip(p, track.id, text, 2.0, start=0.5, font_size=font_size, y=y)
    return p


# Measured on ffmpeg 4.4 (docs/compose.md has the numbers): the limits sit between the worst good frame and the
# smallest mutation below. The mean catches colour and opacity, the share of bad samples catches small moves and text.
CASES = {
    "overlay": (overlay, [0.25, 1.0, 2.0], Tolerance(mean=2.5, share=0.006, separation=3)),
    # 0.5 is before the fade, 1.5 in it, 2.0 and 2.5 at and after its end (SPL-123: no allowance for a late frame).
    "crossfade": (crossfade, [0.5, 1.5, 2.0, 2.5], Tolerance(mean=2.5, share=0.006, separation=3)),
    "grade": (grade, [0.0, 1.5, 2.9], Tolerance(mean=2.5, share=0.006, separation=3)),
    "title": (title, [0.2, 1.0, 2.4], Tolerance(mean=2.0, share=0.0015)),
}
POINTS = [(name, at) for name, (_, times, _) in CASES.items() for at in times]


@pytest.fixture(scope="module")
def exports(media):
    """Each case's export, rendered once (by name)."""
    done = {}

    def get(name):
        if name not in done:
            export(CASES[name][0](media), media, f"{name}.mp4")
            done[name] = f"{name}.mp4"
        return done[name]

    return get


@pytest.mark.parametrize(("case", "at"), POINTS)
def test_preview_matches_the_export(media, exports, capsys, case, at):
    build, _, tol = CASES[case]
    m = measure(build(media), media, exports(case), at, tol)
    with capsys.disabled():
        print(f"\n{case} {m}")
    assert not tol.failures(m), (m, tol.failures(m))


@pytest.mark.parametrize("preset", ["vertical-1080", "square-1080"])
@pytest.mark.parametrize("at", [0.25, 1.5])
def test_the_canvas_preset_gives_the_export_frame(media, capsys, preset, at):
    """SPL-124: the top bar's canvas is a preset; the preview at that preset is the export's frame, same size."""
    project = overlay(media)
    export(project, media, f"{preset}.mp4", preset=preset)
    tol = Tolerance(mean=2.5, share=0.006, separation=3)
    m = measure(project, media, f"{preset}.mp4", at, tol, preset=preset)
    with capsys.disabled():
        print(f"\n{preset} {m}")
    assert not tol.failures(m), (m, tol.failures(m))


# One change on the preview side only: (case, what changed, time to look at, the overrides of the case's builder).
MUTATIONS = [
    ("overlay", "moved 5 px", 1.0, {"x": 0.76}),
    ("overlay", "moved 24 px", 1.0, {"x": 0.8}),
    ("overlay", "opacity 0.7 to 0.55", 1.0, {"opacity": 0.55}),
    ("overlay", "scale 0.35 to 0.4", 1.0, {"scale": 0.4}),
    ("crossfade", "fade 1 s to 0.6 s", 1.5, {"duration": 0.6}),
    ("grade", "contrast 1.4 to 1.3", 1.5, {"contrast": 1.3}),
    ("grade", "saturation 0.6 to 0.7", 1.5, {"saturation": 0.7}),
    ("grade", "brightness 0.1 to 0.13", 1.5, {"brightness": 0.13}),
    ("title", "other text", 1.0, {"text": "Golden 124"}),
    ("title", "moved 6 px", 1.0, {"y": 0.52}),
    ("title", "font 0.15 to 0.16", 1.0, {"font_size": 0.16}),
]


@pytest.mark.parametrize(("case", "what", "at", "override"), MUTATIONS, ids=[f"{c}-{w}" for c, w, _, _ in MUTATIONS])
def test_a_changed_renderer_is_reported(media, exports, capsys, case, what, at, override):
    build, _, tol = CASES[case]
    m = measure(build(media, **override), media, exports(case), at, tol)
    with capsys.disabled():
        print(f"\nmutation {case}: {what}: {m}")
    assert tol.failures(m), f"{case} {what} stayed inside the tolerance: {m}"


def _pixel(img, fx, fy):
    return tuple(int(v) for v in img[int(fy * SIZE[1]), int(fx * SIZE[0])])


def _close(got, want, slack=4):
    return all(abs(g - w) <= slack for g, w in zip(got, want, strict=True))


def test_pinned_colours_do_not_drift_in_either_renderer(media):
    """Flat sources: a change in `build_plan` moves preview and export together, so the colours are pinned."""
    p = empty_project()
    p, _ = _clip(media, p, p.tracks[0].id, "flat", 3.0)
    p, over = ops.add_track(p, "video")
    p, blue = _clip(media, p, over.id, "blue", 3.0)
    p = ops.set_clip_properties(p, blue.id, x=0.5, y=0.5, scale=0.5, opacity=0.5)
    graded = empty_project()
    graded, clip = _clip(media, graded, graded.tracks[0].id, "flat", 3.0)
    graded = ops.set_clip_properties(graded, clip.id, contrast=1.3, saturation=0.5)
    # (project, where, the colour there): the main clip alone, the overlay half over it, and a graded clip
    pins = [(p, (0.05, 0.05), (200, 60, 40)), (p, (0.5, 0.5), (120, 70, 120)), (graded, (0.5, 0.5), GRADED)]
    for i, (project, (fx, fy), want) in enumerate(pins):
        export(project, media, f"pin{i}.mp4")
        shown = decode(preview_frame(project, media, 1.0, width=SIZE[0], format="png", source="original").data)[2]
        assert _close(_pixel(shown, fx, fy), want), ("preview", i, _pixel(shown, fx, fy), want)
        assert _close(_pixel(export_frame(media, f"pin{i}.mp4", 1.0), fx, fy), want), ("export", i)


def _text_box(img):
    """(left, top, right, bottom) of the bright pixels, inclusive."""
    ys, xs = np.where(img.max(axis=2) > 128)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# SPL-125: white "Golden 123" at y=0.5, font_size 0.15 on black, 480x270, measured on ffmpeg 4.4 with the first font
# `find_font` picks (DejaVu Sans): box (127, 120)-(350, 149), 224 x 30 px, centred on (238.5, 134.5). Glyph hinting
# and the font differ by a pixel or two between ffmpeg and font versions, so the margins are 4 px on the centre, 8 on
# the width and 3 on the height: room for that drift, and just under the smallest mutation below (a 5 px move, a
# font step that adds 15 px of width).
TEXT_PIN = {
    "centre": (238.5, 134.5),
    "centre_slack": 4,
    "width": 224,
    "width_slack": 8,
    "height": 30,
    "height_slack": 3,
}


def _text_pin_failures(box):
    left, top, right, bottom = box
    p = TEXT_PIN
    out = []
    if (
        abs((left + right) / 2 - p["centre"][0]) > p["centre_slack"]
        or abs((top + bottom) / 2 - p["centre"][1]) > p["centre_slack"]
    ):
        out.append(f"centre {(left + right) / 2, (top + bottom) / 2} is off {p['centre']}")
    if abs(right - left + 1 - p["width"]) > p["width_slack"]:
        out.append(f"width {right - left + 1} is off {p['width']}")
    if abs(bottom - top + 1 - p["height"]) > p["height_slack"]:
        out.append(f"height {bottom - top + 1} is off {p['height']}")
    return out


def _text_on_black(media, **kw):
    p = empty_project()
    p, _ = _clip(media, p, p.tracks[0].id, "black", 3.0)
    p, track = ops.add_track(p, "video")
    p, _ = ops.add_text_clip(p, track.id, "Golden 123", 2.0, start=0.5, **{"font_size": 0.15, "y": 0.5, **kw})
    return p


def _text_boxes(media, project, name):
    export(project, media, name)
    shown = decode(preview_frame(project, media, 1.0, width=SIZE[0], format="png", source="original").data)[2]
    return {"preview": _text_box(shown.astype(int)), "export": _text_box(export_frame(media, name, 1.0))}


def test_pinned_text_box_does_not_drift_in_either_renderer(media):
    """A change to text position or size in `build_plan` moves preview and export together: pin the box."""
    for renderer, box in _text_boxes(media, _text_on_black(media), "text-pin.mp4").items():
        assert not _text_pin_failures(box), (renderer, box, _text_pin_failures(box))


@pytest.mark.parametrize(
    ("what", "override"),
    [("moved 5 px down", {"y": 0.52}), ("moved 5 px left", {"x": 0.489}), ("font 0.15 to 0.16", {"font_size": 0.16})],
)
def test_a_moved_or_resized_text_box_is_reported(media, what, override):
    boxes = _text_boxes(media, _text_on_black(media, **override), "text-mut.mp4")
    for renderer, box in boxes.items():
        assert _text_pin_failures(box), f"{what} stayed inside the pin in the {renderer}: {box}"


def test_an_export_without_a_preset_follows_the_project_canvas(media, capsys):
    """SPL-82: a project with a canvas exports at the canvas's size by default, and that frame is the preview's."""
    project = replace(overlay(media), canvas="9:16")
    render(project, media, "canvas-default.mp4")
    tol = Tolerance(mean=2.5, share=0.006, separation=3)
    m = measure(project, media, "canvas-default.mp4", 1.0, tol, preset="vertical-1080")
    with capsys.disabled():
        print(f"\ncanvas default {m}")
    assert not tol.failures(m), (m, tol.failures(m))
    shown = preview_frame(project, media, 1.0, format="png", source="original")  # no preset: the canvas's
    assert shown.width < shown.height
