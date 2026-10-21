"""Burned-in captions with the spoken word highlighted, built from the assets' transcripts at render time.

Captions are a render option, not part of the edit plan or the project file: nothing here is stored. `render.render`
asks `build` for the caption events of a project, writes them as an ASS subtitle document in a temporary directory
(never the workspace) and burns it in with ffmpeg's `ass` filter (libass) after the overlays and text clips, at the
output resolution, so they work with any preset, fit and crop.

Mapping to the timeline
-----------------------
For a media clip showing source `[in, out)` placed at timeline time `S`, a transcript word at source time `t`
appears at `S + (t - in)`. Clips come from the main video track's clips (the first video track, when it is magnetic),
or from the 1-based track numbers in `caption_tracks`. A muted clip, a text clip and a clip from an asset without a
transcript give no captions (the last are listed in the render result as `clips_without`); a clip's gain does not matter.
The clip's `start` is what `timeline_ops.normalize_track` computed, so a crossfade, which pulls the next clip back by
its length, is already in it. Within a crossfade the clip that is fading in takes over: the words of the earlier clip
stop at the next clip's start. A word is kept when its midpoint lies inside the clip's visible part, and its times are
then clamped to that part, so a word straddling a cut is dropped when most of it is cut off and truncated when most of
it is shown. `caption_range` (timeline seconds) cuts the captions the same way.

Lines
-----
Words are grouped into caption lines, never across a clip boundary: a line ends at a word that ends a sentence
(`.`, `?`, `!`), before a pause longer than `PAUSE_BREAK` seconds, when it would last longer than `MAX_LINE_SECONDS`, or
when the next word no longer fits `max_lines` rows of `max_chars` characters. The characters per row are also limited
by what fits the frame's width at the style's font size (a vertical frame is narrower than a landscape one, so rows are
shorter there). A line shows from its first word's start to its last word's end plus `HOLD_SECONDS`, clamped so that
it never overlaps the next line.

Highlight
---------
Karaoke by events, not by `\\k` tags: a line is one ASS `Dialogue` event per word, each repeating the whole line with the
word being spoken in the highlight colour (`{\\1c...}`), shown from that word's start until the next word starts (the
last word until its end, then the plain line for the hold). The result does not depend on libass's karaoke timing and
the frames are easy to test. The cost is one event per word.

Styles
------
`STYLES` is the one table of styles; the HTTP route, CLI and MCP tool only list and name them. Sizes are fractions of the
output height so they scale across presets. A request may override `font_size`, `highlight_color` and `position`
(`top`, `middle`, `bottom`); everything is validated here with `CaptionError` messages that list the valid values.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from split_video import transcribe
from split_video.timeline import Clip, Project, TimelineError, Track

PAUSE_BREAK = 0.6  # a longer gap between two words starts a new line
MAX_LINE_SECONDS = 4.0  # a line never lasts longer than this
HOLD_SECONDS = 0.25  # a line stays this long after its last word (unless the next line comes sooner)
MIN_WORD_SECONDS = 0.04  # a word is shown at least this long, so it is never a single-frame flash
POSITIONS = ("top", "middle", "bottom")
SENTENCE_END = (".", "?", "!", "…")
# A full stop after these is not the end of a sentence ("Mr. Quilter"); compared lower-cased, without the dot.
ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "mt", "gen", "col", "lt", "capt"})
MIN_FONT_SIZE, MAX_FONT_SIZE = 0.01, 0.2
SAFE_MARGIN = 0.10  # top and bottom 10% are kept free (the middle 80% is the safe area of a vertical video)
SIDE_MARGIN = 0.06  # fraction of the width kept free at each side


class CaptionError(TimelineError):
    """An unknown caption style or position, or an invalid caption option; the message lists the valid values."""


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    label: str
    description: str
    font_size: float  # fraction of the output height
    primary: str  # text colour, "#RRGGBB"
    highlight: str  # colour of the word being spoken
    outline: str  # outline (or box) colour
    outline_width: float  # fraction of the output height; with `box`, the padding of the box
    shadow: float  # fraction of the output height (0 for none)
    box: bool  # an opaque box behind the text instead of an outline
    bold: bool
    position: str  # one of POSITIONS
    margin_v: float  # fraction of the output height kept free between the text and the frame edge it sits at
    max_chars: int  # characters per row (also limited by the frame's width)
    max_lines: int  # rows per caption (1 or 2)
    uppercase: bool

    def to_dict(self) -> dict:
        return asdict(self)


STYLES: dict[str, CaptionStyle] = {
    s.name: s
    for s in (
        CaptionStyle(
            "classic", "Classic", "White text on a dark box in the lower third, the spoken word in yellow.",
            0.045, "#FFFFFF", "#FFE600", "#101010", 0.008, 0.0, True, False, "bottom", 0.10, 42, 2, False,
        ),
        CaptionStyle(
            "shorts", "Shorts", "Large bold text with a thick outline in the lower-middle of a vertical frame, uppercase, the spoken word in green.",
            0.05, "#FFFFFF", "#39FF14", "#000000", 0.006, 0.002, False, True, "bottom", 0.24, 22, 2, True,
        ),
        CaptionStyle(
            "minimal", "Minimal", "Small light text at the bottom with a thin outline, the spoken word in pale blue.",
            0.034, "#F2F2F2", "#8EC8FF", "#000000", 0.003, 0.0, False, False, "bottom", 0.08, 50, 1, False,
        ),
    )
}  # fmt: skip
DEFAULT_STYLE = "classic"


def names() -> list[str]:
    return sorted(STYLES)


def get(name: str) -> CaptionStyle:
    """The style called `name`; `CaptionError` listing the valid names if there is none."""
    try:
        return STYLES[name]
    except (KeyError, TypeError):
        raise CaptionError(f"unknown caption style {name!r}; valid styles: {', '.join(names())}") from None


def listing() -> dict:
    """The JSON the HTTP route, CLI and MCP tool all return: every style with all its fields, and the valid values of
    the request's overrides."""
    return {
        "styles": [STYLES[n].to_dict() for n in names()],
        "positions": list(POSITIONS),
        "font_size_range": [MIN_FONT_SIZE, MAX_FONT_SIZE],
        "default_style": DEFAULT_STYLE,
    }


def check_color(value: str, what: str = "color") -> str:
    """`value` as an upper-case `#RRGGBB`; `CaptionError` otherwise."""
    if (
        not isinstance(value, str)
        or len(value) != 7
        or value[0] != "#"
        or any(c not in "0123456789abcdefABCDEF" for c in value[1:])
    ):
        raise CaptionError(f"{what} must look like #RRGGBB, not {value!r}")
    return value.upper()


@dataclass(frozen=True)
class CaptionRequest:
    """The caption options of a render, validated. Build it with `request`."""

    style: str
    range: tuple[float, float] | None = None  # timeline seconds
    font_size: float | None = None
    highlight_color: str | None = None
    position: str | None = None
    tracks: tuple[int, ...] | None = None  # 1-based track numbers; None: the main video track


def check_range(value: Any) -> tuple[float, float] | None:
    """A caption range as `(start, end)` seconds from a `(start, end)` pair or a `{"start", "end"}` dict; None passes."""
    if value is None:
        return None
    try:
        start, end = (value["start"], value["end"]) if isinstance(value, dict) else value
        start, end = float(start), float(end)
    except (TypeError, ValueError, KeyError):
        raise CaptionError("caption_range must be {start, end} in seconds") from None
    if not (start >= 0 and end > start) or end == float("inf"):
        raise CaptionError("caption_range needs 0 <= start < end (seconds on the timeline)")
    return start, end


def parse_range(text: str) -> tuple[float, float]:
    """`"10-40"` (seconds) as (10.0, 40.0); `CaptionError` if it isn't START-END."""
    try:
        start, end = text.split("-")
        return check_range((float(start), float(end)))  # type: ignore[return-value]
    except ValueError:
        raise CaptionError(f"caption range must look like 10-40 (seconds), not {text!r}") from None


def request(
    captions: str | bool | None,
    caption_range: Any = None,
    font_size: float | None = None,
    highlight_color: str | None = None,
    position: str | None = None,
    tracks: list[int] | tuple[int, ...] | None = None,
) -> CaptionRequest | None:
    """Validate the caption options of a render request. `captions` is a style name (`True` means the default
    style); `None`/`False` mean no captions and then the other options are ignored (None is returned). Raises
    `CaptionError`."""
    if captions is None or captions is False:
        return None
    style = get(DEFAULT_STYLE if captions is True else captions).name
    if font_size is not None and (
        isinstance(font_size, bool)
        or not isinstance(font_size, int | float)
        or not (MIN_FONT_SIZE <= font_size <= MAX_FONT_SIZE)
    ):
        raise CaptionError(
            f"caption font_size is a fraction of the frame height from {MIN_FONT_SIZE} to {MAX_FONT_SIZE}"
        )
    if position is not None and position not in POSITIONS:
        raise CaptionError(f"unknown caption position {position!r}; valid positions: {', '.join(POSITIONS)}")
    if tracks is not None and (
        not tracks
        or any(isinstance(t, bool) or not isinstance(t, int) or t < 1 for t in tracks)
        or len(set(tracks)) != len(tracks)
    ):
        raise CaptionError("caption_tracks is a list of distinct track numbers from 1 (as `compose show` numbers them)")
    return CaptionRequest(
        style,
        check_range(caption_range),
        None if font_size is None else float(font_size),
        None if highlight_color is None else check_color(highlight_color, "caption highlight_color"),
        position,
        None if tracks is None else tuple(tracks),
    )


def resolve_style(req: CaptionRequest) -> CaptionStyle:
    """The named style with the request's overrides applied."""
    style = get(req.style)
    if req.font_size is not None:
        style = replace(style, font_size=req.font_size)
    if req.highlight_color is not None:
        style = replace(style, highlight=req.highlight_color)
    if req.position is not None:
        style = replace(style, position=req.position)
    return style


# --- which words, where -------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    start: float  # timeline seconds
    end: float
    text: str


def caption_tracks(project: Project, req: CaptionRequest) -> list[Track]:
    if req.tracks is None:
        from split_video.render import _main_track

        main = _main_track(project)
        return [main] if main else []
    chosen = []
    for number in req.tracks:
        if number > len(project.tracks):
            raise CaptionError(f"caption_tracks: there is no track {number} (the timeline has {len(project.tracks)})")
        chosen.append(project.tracks[number - 1])
    return chosen


def clip_words(
    clip: Clip, transcript: dict[str, Any], window_end: float, bounds: tuple[float, float] | None = None
) -> list[Word]:
    """The transcript words of one clip on the timeline. `window_end` is where the clip stops being shown (its end,
    or the start of the next clip when that one crossfades in); `bounds` is the caption range. A word counts when its
    midpoint is inside the shown part, and its times are clamped to it (see the module docstring)."""
    shown_end = min(clip.end, window_end)
    lo, hi = clip.start, shown_end
    if bounds is not None:
        lo, hi = max(lo, bounds[0]), min(hi, bounds[1])
    if hi <= lo:
        return []
    source_lo = clip.in_point + (lo - clip.start)
    source_hi = clip.in_point + (hi - clip.start)
    words = []
    for w in transcribe.words_between(transcript, source_lo, source_hi):
        if not (source_lo <= (w["start"] + w["end"]) / 2 < source_hi):
            continue
        text = " ".join(str(w["word"]).split())
        if not text:
            continue
        start = clip.start + (max(w["start"], source_lo) - clip.in_point)
        end = clip.start + (min(w["end"], source_hi) - clip.in_point)
        words.append(Word(start, max(end, min(start + MIN_WORD_SECONDS, hi)), text))
    return words


def collect(
    project: Project,
    req: CaptionRequest,
    load: Callable[[str], dict[str, Any] | None],
) -> tuple[list[list[Word]], dict[str, Any]]:
    """The words to caption, as one list per clip in timeline order, and the report
    `{"clips_with_transcript", "clips_without", "words"}`. `load(asset_id)` returns an asset's transcript or None."""
    groups: list[tuple[float, list[Word]]] = []
    seen: dict[str, dict[str, Any] | None] = {}
    with_transcript, without = 0, []
    for track in caption_tracks(project, req):
        clips = sorted(track.clips, key=lambda c: c.start)
        for n, clip in enumerate(clips):
            if clip.kind != "media" or clip.muted:
                continue
            if clip.asset not in seen:
                seen[clip.asset] = load(clip.asset) if clip.asset else None
            transcript = seen[clip.asset]
            if transcript is None:
                without.append(clip.id)
                continue
            with_transcript += 1
            following = clips[n + 1].start if track.magnetic and n + 1 < len(clips) else float("inf")
            words = clip_words(clip, transcript, following, req.range)
            if words:
                groups.append((clip.start, words))
    groups.sort(key=lambda g: g[0])
    ordered = [words for _, words in groups]
    report = {
        "clips_with_transcript": with_transcript,
        "clips_without": without,
        "words": sum(len(g) for g in ordered),
    }
    return ordered, report


# --- lines ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    words: tuple[Word, ...]
    rows: tuple[tuple[Word, ...], ...]  # the words as the rows they are broken into

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def ends_sentence(text: str) -> bool:
    """Whether a word ends a sentence: it ends in `.`, `?`, `!` or an ellipsis (quotes and brackets after the mark are
    ignored), and is not an abbreviation such as "Mr."."""
    bare = text.rstrip("\"')]\u201d\u2019")
    return bare.endswith(SENTENCE_END) and bare.lower().rstrip(".") not in ABBREVIATIONS


def _display(text: str, style: CaptionStyle) -> str:
    return text.upper() if style.uppercase else text


def row_limit(style: CaptionStyle, width: int, height: int) -> int:
    """Characters per row: the style's limit, lowered to what fits the frame's width (text width is estimated from
    the font size, wider for bold and upper-case text; a row always has room for at least 8 characters)."""
    px = style.font_size * height
    advance = (0.62 if style.bold else 0.55) * (1.1 if style.uppercase else 1.0) * px
    usable = width * (1 - 2 * SIDE_MARGIN)
    return max(8, min(style.max_chars, int(usable / advance)))


def _rows(words: list[Word], style: CaptionStyle, limit: int) -> list[list[Word]] | None:
    """Greedy rows of at most `limit` characters; None if they need more than `style.max_lines` rows. A single word
    longer than a row sits alone on its row."""
    rows: list[list[Word]] = [[]]
    length = 0
    for w in words:
        n = len(_display(w.text, style))
        if rows[-1] and length + 1 + n > limit:
            rows.append([])
            length = 0
        length += n + (1 if rows[-1] else 0)
        rows[-1].append(w)
    return rows if len(rows) <= style.max_lines else None


def make_lines(words: list[Word], style: CaptionStyle, width: int, height: int) -> list[Line]:
    """Group one clip's words into lines (see the module docstring for the rules)."""
    limit = row_limit(style, width, height)
    lines: list[Line] = []
    current: list[Word] = []

    def close() -> None:
        if current:
            rows = _rows(current, style, limit)
            assert rows is not None
            lines.append(Line(tuple(current), tuple(tuple(r) for r in rows)))
            current.clear()

    for w in words:
        if current:
            fits = _rows([*current, w], style, limit) is not None
            if (
                ends_sentence(current[-1].text)
                or w.start - current[-1].end > PAUSE_BREAK
                or w.end - current[0].start > MAX_LINE_SECONDS
                or not fits
            ):
                close()
        current.append(w)
    close()
    return lines


# --- the ASS document -------------------------------------------------------------------------


def ass_text(text: str) -> str:
    """`text` safe inside an ASS `Dialogue` text field. Braces would open an override block and are written as the
    escapes libass understands (`\\{`, `\\}`); a backslash is replaced by a look-alike (it starts `\\N`, `\\h` ...);
    newlines and control characters become spaces. Commas, colons and unicode need nothing: the text is the last field
    and the file is UTF-8."""
    out = []
    for ch in text:
        if ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ch == "\\":
            out.append("⧵")  # reverse solidus operator
        elif ch in "\r\n\t" or ord(ch) < 32:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def ass_color(rgb: str, alpha: int = 0) -> str:
    """`#RRGGBB` as ASS's `&HAABBGGRR` (alpha 0 is opaque, 255 transparent)."""
    r, g, b = rgb[1:3], rgb[3:5], rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    """`H:MM:SS.cc` (centiseconds, the precision ASS has)."""
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def font_family(path: str) -> str:
    """The family name of a TrueType font file (its `name` table), or the file's name without extension if it can't
    be read. libass finds the font in `fontsdir` by this name."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
        count = struct.unpack(">H", data[4:6])[0]
        for i in range(count):
            tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * i : 28 + 16 * i])
            if tag != b"name":
                continue
            table = data[offset : offset + length]
            _, n, string_offset = struct.unpack(">HHH", table[:6])
            best = None
            for j in range(n):
                platform, _, _, name_id, size, off = struct.unpack(">HHHHHH", table[6 + 12 * j : 18 + 12 * j])
                if name_id != 1:
                    continue
                raw = table[string_offset + off : string_offset + off + size]
                value = raw.decode("utf-16-be" if platform in (0, 3) else "latin-1", "replace")
                if platform == 3 or best is None:
                    best = value
            if best:
                return best
    except (OSError, struct.error, ValueError):
        pass
    return os.path.splitext(os.path.basename(path))[0]


def _event(start: float, end: float, text: str, margin_v: int = 0) -> str:
    return f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,{margin_v},,{text}"


def _line_text(line: Line, current: int | None, style: CaptionStyle) -> str:
    """The ASS text of a line with word number `current` (an index into `line.words`) highlighted; None for none."""
    plain, light = ass_color(style.primary), ass_color(style.highlight)
    rows = []
    index = 0
    for row in line.rows:
        shown = []
        for w in row:
            body = ass_text(_display(w.text, style))
            shown.append(f"{{\\1c{light}&}}{body}{{\\1c{plain}&}}" if index == current else body)
            index += 1
        rows.append(" ".join(shown))
    return "\\N".join(rows)


def events(lines: list[Line], style: CaptionStyle) -> list[tuple[float, float, str]]:
    """`(start, end, text)` for every event: one per word (highlighted) and one for the hold after each line. Lines
    never overlap: a line's events stop where the next line starts."""
    out: list[tuple[float, float, str]] = []
    for n, line in enumerate(lines):
        limit = lines[n + 1].start if n + 1 < len(lines) else float("inf")
        for i, word in enumerate(line.words):
            end = line.words[i + 1].start if i + 1 < len(line.words) else word.end
            end = min(max(end, word.start + MIN_WORD_SECONDS), limit)
            if end > word.start:
                out.append((word.start, end, _line_text(line, i, style)))
        hold_start = min(line.end, limit)
        hold_end = min(line.end + HOLD_SECONDS, limit)
        if hold_end - hold_start > 0.02:
            out.append((hold_start, hold_end, _line_text(line, None, style)))
    return out


def _alignment(position: str) -> int:
    return {"top": 8, "middle": 5, "bottom": 2}[position]


def document(evs: list[tuple[float, float, str]], style: CaptionStyle, width: int, height: int, font: str) -> str:
    """The ASS file for `evs` at the output size, in `font` (a family name)."""
    px = max(8, round(style.font_size * height))
    outline = max(1, round(style.outline_width * height))
    shadow = round(style.shadow * height)
    if style.position == "middle":
        margin_v = 0
    elif style.position == "top":
        margin_v = round(max(style.margin_v, SAFE_MARGIN) * height)
    else:
        margin_v = round(max(style.margin_v, SAFE_MARGIN * 0.5) * height)
    side = round(SIDE_MARGIN * width)
    border = 3 if style.box else 1
    # An opaque box: each differently coloured run of a line gets its own box and translucent ones would show the
    # overlaps as darker patches.
    back = ass_color(style.outline, 0x60)
    outline_colour = ass_color(style.outline)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Caption,{font.replace(',', ' ')},{px},{ass_color(style.primary)},{ass_color(style.primary)},"
        f"{outline_colour},{back},{-1 if style.bold else 0},0,0,0,100,100,0,0,{border},{outline},{shadow},"
        f"{_alignment(style.position)},{side},{side},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return head + "".join(_event(s, e, t) + "\n" for s, e, t in evs)


@dataclass(frozen=True)
class Built:
    """What `build` made: the ASS document (None when there is nothing to caption) and the report."""

    ass: str | None
    report: dict[str, Any]


def build(
    project: Project,
    req: CaptionRequest,
    load: Callable[[str], dict[str, Any] | None],
    width: int,
    height: int,
    font_path: str,
) -> Built:
    """Collect the words, break them into lines and write the ASS document. The report is the `captions` entry of the
    render result: `{style, clips_with_transcript, clips_without, words, lines, events}`."""
    style = resolve_style(req)
    groups, report = collect(project, req, load)
    lines = [line for words in groups for line in make_lines(words, style, width, height)]
    lines.sort(key=lambda line: line.start)
    evs = events(lines, style)
    report = {"style": style.name, **report, "lines": len(lines), "events": len(evs)}
    if not evs:
        return Built(None, report)
    return Built(document(evs, style, width, height, font_family(font_path)), report)


# --- ffmpeg ---------------------------------------------------------------------------------


def filter_escape(text: str) -> str:
    """`text` as one filter option value that survives both levels of ffmpeg filtergraph parsing (the option
    separator `:` and the graph's own `[ ] , ;` and quotes), whatever characters a path has."""
    once = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "".join("\\" + c if c in "\\'[],;" else c for c in once)


def ass_filter(ass_path: str, fonts_dir: str, in_label: str, out_label: str) -> str:
    """The graph part burning the ASS file at `ass_path` into `[in_label]`, with fonts read from `fonts_dir`."""
    return f"[{in_label}]ass=filename={filter_escape(ass_path)}:fontsdir={filter_escape(fonts_dir)}[{out_label}]"
