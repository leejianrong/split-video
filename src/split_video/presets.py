"""Export presets: named destinations (YouTube, a vertical short, ...) instead of codec flags.

A preset fixes the frame size, frame rate and encoder settings of a render. This table is the one place they live;
the HTTP route, CLI and MCP server only list and name them. Every preset writes an mp4 (H.264 video, AAC audio,
`+faststart` so the moov atom comes first and the file starts playing before it has downloaded).

Video rate control is constant quality (`-crf`, lower is better) at an x264 speed `preset`; there is no bitrate
target, so the file size follows the picture. Audio is AAC at a fixed bitrate and sample rate.

`resolve_export` applies the precedence the render options share: an explicit width and height beat the preset's size,
which beats the first clip's size. Frame rate and encoder settings only ever come from the preset (or today's defaults
when there is none: 30 fps, x264 `veryfast` at its default quality, AAC at its default bitrate).

How a picture whose aspect differs from the frame is fitted is a separate, explicit choice (`FITS`): `letterbox`
(scale to fit and pad with black bars, the default) or `crop` (scale to cover the frame and cut off the overflow,
centered). A clip with its own crop (`Clip.crop`) keeps its crop-then-fit behaviour under either.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from split_video.timeline import TimelineError

FITS = ("letterbox", "crop")
DEFAULT_FIT = "letterbox"
DEFAULT_FPS = 30
DEFAULT_X264_PRESET = "veryfast"


class PresetError(TimelineError):
    """An unknown export preset or fit; the message lists the valid names."""


@dataclass(frozen=True)
class ExportPreset:
    name: str
    label: str
    description: str
    width: int
    height: int
    fps: int
    video_codec: str  # ffmpeg encoder
    x264_preset: str  # encoder speed/efficiency trade-off
    crf: int  # constant quality, 0 (lossless) to 51 (worst)
    pix_fmt: str
    audio_codec: str
    audio_bitrate: str
    audio_sample_rate: int
    container: str
    faststart: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _preset(name, label, description, width, height, fps=30, x264_preset="veryfast", crf=20, audio_bitrate="192k"):
    return ExportPreset(
        name, label, description, width, height, fps, "libx264", x264_preset, crf, "yuv420p",
        "aac", audio_bitrate, 48000, "mp4", True,
    )  # fmt: skip


PRESETS: dict[str, ExportPreset] = {
    p.name: p
    for p in (
        _preset(
            "youtube-1080p", "YouTube 1080p", "Landscape 1920x1080 at 30 fps, for YouTube and most players.", 1920, 1080
        ),
        _preset(
            "vertical-1080",
            "Vertical 1080x1920",
            "Portrait 1080x1920 at 30 fps, for Shorts, Reels and TikTok.",
            1080,
            1920,
        ),
        _preset("square-1080", "Square 1080x1080", "Square 1080x1080 at 30 fps, for feed posts.", 1080, 1080),
        _preset(
            "draft-720",
            "Draft 720p",
            "Quick review render: 1280x720 at 24 fps, fastest encoder setting and lower quality.",
            1280,
            720,
            fps=24,
            x264_preset="ultrafast",
            crf=30,
            audio_bitrate="96k",
        ),
    )
}


def names() -> list[str]:
    return sorted(PRESETS)


def get(name: str) -> ExportPreset:
    """The preset called `name`; `PresetError` listing the valid names if there is none."""
    try:
        return PRESETS[name]
    except (KeyError, TypeError):
        raise PresetError(f"unknown preset {name!r}; valid presets: {', '.join(names())}") from None


def check_fit(fit: str | None) -> str:
    """`fit` validated (None means the default); `PresetError` listing the valid choices."""
    if fit is None:
        return DEFAULT_FIT
    if fit not in FITS:
        raise PresetError(f"unknown fit {fit!r}; valid fits: {', '.join(FITS)}")
    return fit


@dataclass(frozen=True)
class ExportSettings:
    """What a render does: the preset used (None for the defaults), the frame size (None: the first clip's),
    the frame rate, the encoder settings and how a mismatched picture is fitted."""

    preset: ExportPreset | None
    size: tuple[int, int] | None
    fps: int
    fit: str


def resolve_export(preset: str | None, width: int | None, height: int | None, fit: str | None = None) -> ExportSettings:
    """Resolve a render request. Size: explicit `width`/`height` (both or neither, see `render.check_size`) over the
    preset's over None (the first clip's). Raises `PresetError` or `TimelineError`."""
    from split_video.render import check_size  # render imports this module

    chosen = get(preset) if preset is not None else None
    explicit = check_size(width, height)
    size = explicit or ((chosen.width, chosen.height) if chosen else None)
    return ExportSettings(chosen, size, chosen.fps if chosen else DEFAULT_FPS, check_fit(fit))


def listing() -> dict:
    """The JSON the HTTP route, CLI and MCP tool all return: every preset with all its fields, and the valid fits."""
    return {"presets": [PRESETS[n].to_dict() for n in names()], "default_fit": DEFAULT_FIT, "fits": list(FITS)}
