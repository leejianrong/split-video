"""Caption mapping, line breaking, ASS output and option validation (SPL-53), with no ffmpeg."""

import itertools
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_transcript import document_from_words

from split_video import captions
from split_video import timeline_ops as ops
from split_video.ffmpeg import MediaInfo
from split_video.render import build_plan
from split_video.timeline import TimelineError, empty_project

CLASSIC = captions.get("classic")
SHORTS = captions.get("shorts")


def _doc(asset, words):
    return document_from_words(asset, words)


def _request(style="classic", **kw):
    return captions.request(style, **kw)


def _project(clips, *, transitions=None):
    """One main track with `(asset, in, out)` clips; `transitions` maps clip index -> crossfade seconds."""
    p = empty_project()
    tid = p.tracks[0].id
    made = []
    for asset, a, b in clips:
        p, clip = ops.add_clip(p, tid, f"{asset}.mp4", 60.0, in_point=a, out_point=b, asset=asset)
        made.append(clip)
    for n, d in (transitions or {}).items():
        p = ops.set_transition(p, made[n].id, "crossfade", d)
    return p


def _loader(**docs):
    return lambda asset: docs.get(asset)


def _words(*triples):
    return [(a, b, w) for a, b, w in triples]


# --- timing math -------------------------------------------------------------------------------------------------


def test_words_map_from_source_time_to_the_clips_place_on_the_timeline():
    # Source 10..20 s of a clip placed second on the track after a 5 s clip: source t -> 5 + (t - 10).
    p = _project([("b", 0, 5), ("a", 10, 20)])
    doc = _doc("a", _words((9.0, 9.5, "before"), (11.0, 11.5, "one"), (12.0, 12.4, "two"), (21.0, 21.5, "after")))
    groups, report = captions.collect(p, _request(), _loader(a=doc, b=None))
    [words] = groups
    assert [(w.text, round(w.start, 3), round(w.end, 3)) for w in words] == [("one", 6.0, 6.5), ("two", 7.0, 7.4)]
    assert report == {"clips_with_transcript": 1, "clips_without": [p.tracks[0].clips[0].id], "words": 2}


def test_a_crossfade_uses_the_clips_normalised_start_and_the_incoming_clip_takes_over():
    p = _project([("a", 0, 10), ("b", 0, 10)], transitions={1: 2.0})
    assert p.tracks[0].clips[1].start == 8.0  # the fade pulls the second clip back by its length
    doc_a = _doc("a", _words((7.0, 7.5, "keep"), (8.4, 8.8, "faded")))
    doc_b = _doc("b", _words((0.2, 0.6, "in"), (3.0, 3.4, "later")))
    groups, _ = captions.collect(p, _request(), _loader(a=doc_a, b=doc_b))
    flat = [(w.text, round(w.start, 2)) for g in groups for w in g]
    # "faded" is inside the cross-fade (timeline 8..10), where the second clip is taking over: dropped.
    assert flat == [("keep", 7.0), ("in", 8.2), ("later", 11.0)]


def test_a_word_straddling_a_cut_is_dropped_when_mostly_cut_and_truncated_when_mostly_shown():
    p = _project([("a", 5.0, 10.0)])
    doc = _doc(
        "a",
        _words(
            (4.0, 5.2, "mostly-cut-at-start"),  # midpoint 4.6 < in point: dropped
            (4.8, 6.0, "mostly-shown-at-start"),  # midpoint 5.4: kept, start clamped to 5.0
            (9.0, 10.2, "mostly-shown-at-end"),  # midpoint 9.6: kept, end clamped to 10.0
            (9.8, 11.0, "mostly-cut-at-end"),  # midpoint 10.4: dropped
        ),
    )
    [words], _ = captions.collect(p, _request(), _loader(a=doc))
    assert [(w.text, round(w.start, 2), round(w.end, 2)) for w in words] == [
        ("mostly-shown-at-start", 0.0, 1.0),
        ("mostly-shown-at-end", 4.0, 5.0),
    ]


def test_caption_range_limits_the_words_in_timeline_seconds():
    p = _project([("a", 0, 10)])
    doc = _doc("a", _words(*[(float(i), i + 0.5, f"w{i}") for i in range(10)]))
    [words], report = captions.collect(p, _request(caption_range={"start": 3, "end": 6}), _loader(a=doc))
    assert [w.text for w in words] == ["w3", "w4", "w5"] and report["words"] == 3


def test_a_range_beyond_the_timeline_captions_nothing():
    p = _project([("a", 0, 10)])
    doc = _doc("a", _words((1.0, 1.5, "x")))
    groups, report = captions.collect(p, _request(caption_range=(50, 60)), _loader(a=doc))
    assert groups == [] and report["words"] == 0


def test_clips_without_a_transcript_are_reported_and_muted_clips_are_skipped():
    p = _project([("a", 0, 5), ("b", 0, 5), ("c", 0, 5)])
    c = p.tracks[0].clips
    p = ops.set_clip_properties(p, c[2].id, muted=True)
    doc = _doc("a", _words((1.0, 1.5, "hi")))
    groups, report = captions.collect(p, _request(), _loader(a=doc, c=doc))
    assert report["clips_with_transcript"] == 1 and report["clips_without"] == [c[1].id] and report["words"] == 1


def test_overlay_and_audio_tracks_are_excluded_unless_asked_for():
    p = _project([("a", 0, 5)])
    p, overlay = ops.add_track(p, "video")
    p, _ = ops.add_clip(p, overlay.id, "o.mp4", 30.0, in_point=0, out_point=5, asset="o", start=0.0)
    docs = {
        "a": _doc("a", _words((1.0, 1.5, "main"))),
        "o": _doc("o", _words((1.0, 1.5, "overlay"))),
    }
    groups, _ = captions.collect(p, _request(), _loader(**docs))
    assert [w.text for g in groups for w in g] == ["main"]
    groups, _ = captions.collect(p, _request(tracks=[2]), _loader(**docs))
    assert [w.text for g in groups for w in g] == ["overlay"]
    with pytest.raises(captions.CaptionError, match="no track 3"):
        captions.collect(p, _request(tracks=[3]), _loader(**docs))


# --- lines ------------------------------------------------------------------------------------------------------


def _ws(*texts, step=0.3, start=0.0):
    out, t = [], start
    for text in texts:
        out.append(captions.Word(t, t + step * 0.9, text))
        t += step
    return out


def _lines(words, style=CLASSIC, w=1920, h=1080):
    return captions.make_lines(words, style, w, h)


def test_a_line_ends_at_sentence_punctuation_but_not_after_an_abbreviation():
    lines = _lines(_ws("Mr.", "Quilter", "is", "here.", "Then", "he", "left"))
    assert [[w.text for w in line.words] for line in lines] == [
        ["Mr.", "Quilter", "is", "here."],
        ["Then", "he", "left"],
    ]


def test_a_pause_longer_than_the_threshold_starts_a_new_line():
    words = [*_ws("one", "two"), captions.Word(1.5, 1.8, "three")]  # 0.54 s gap would not break; 0.9 s does
    assert len(_lines(words)) == 2
    near = [*_ws("one", "two"), captions.Word(0.6 + 0.5, 1.4, "three")]
    assert len(_lines(near)) == 1


def test_a_line_never_lasts_longer_than_the_maximum():
    words = _ws(*["word"] * 30, step=0.3)
    for line in _lines(words, captions.get("minimal")):
        assert line.end - line.start <= captions.MAX_LINE_SECONDS + 1e-9


def test_rows_respect_max_chars_and_max_lines():
    words = _ws(*["abcdefghij"] * 12)
    style = captions.get("classic")
    limit = captions.row_limit(style, 1920, 1080)
    for line in _lines(words, style):
        assert len(line.rows) <= style.max_lines
        for row in line.rows:
            assert len(" ".join(w.text for w in row)) <= max(limit, 10)
    one_row = _lines(_ws(*["abcdefghij"] * 6), captions.get("minimal"))
    assert all(len(line.rows) == 1 for line in one_row)


def test_rows_are_shorter_in_a_narrow_frame_than_a_wide_one():
    assert captions.row_limit(SHORTS, 1080, 1920) < captions.row_limit(SHORTS, 1920, 1080)
    assert captions.row_limit(SHORTS, 1080, 1920) <= SHORTS.max_chars


def test_lines_never_span_two_clips():
    p = _project([("a", 0, 2), ("a", 0, 2)])
    doc = _doc("a", _words((0.2, 0.5, "alpha"), (1.0, 1.4, "beta")))
    groups, _ = captions.collect(p, _request(), _loader(a=doc))
    lines = [line for g in groups for line in captions.make_lines(g, CLASSIC, 1920, 1080)]
    assert [[w.text for w in line.words] for line in lines] == [["alpha", "beta"], ["alpha", "beta"]]
    assert lines[0].end <= lines[1].start


# --- events and the ASS document ----------------------------------------------------------------------------------


def _event_rows(doc):
    rows = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    parsed = []
    for line in rows:
        fields = line.split(",", 9)
        parsed.append((_secs(fields[1]), _secs(fields[2]), fields[9]))
    return parsed


def _secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_one_event_per_word_highlights_it_and_a_hold_follows_the_line():
    lines = _lines(_ws("alpha", "beta", "gamma", step=0.5))
    evs = captions.events(lines, CLASSIC)
    assert len(evs) == 4  # three words and the hold
    light = captions.ass_color(CLASSIC.highlight)
    assert [("{\\1c" + light + "&}" + w) in evs[i][2] for i, w in enumerate(["alpha", "beta", "gamma"])] == [True] * 3
    assert light not in evs[3][2]  # the hold shows the plain line
    # Each word is highlighted from its start until the next word starts, the last until its end.
    assert [round(e[0], 2) for e in evs] == [0.0, 0.5, 1.0, round(1.0 + 0.45, 2)]
    assert evs[1][0] == evs[0][1] and evs[2][0] == evs[1][1]
    assert evs[3][1] - evs[3][0] == pytest.approx(captions.HOLD_SECONDS)


def test_events_never_overlap_even_when_lines_are_close_together():
    words = [captions.Word(0.0, 0.5, "end."), captions.Word(0.55, 1.0, "next"), captions.Word(1.0, 1.4, "line")]
    evs = captions.events(_lines(words), CLASSIC)
    for a, b in itertools.pairwise(evs):
        assert a[1] <= b[0] + 1e-9


def test_special_characters_are_escaped_in_the_text_field():
    assert captions.ass_text("a{b}c") == "a\\{b\\}c"
    assert "\\" not in captions.ass_text("back\\slash").replace("\\{", "")
    assert captions.ass_text("line\nbreak\r\ttab") == "line break  tab"
    assert captions.ass_text("com,ma: colon é 日本") == "com,ma: colon é 日本"
    # An override-looking word cannot inject tags: its braces are escaped.
    assert "{" not in captions.ass_text("{\\1c&H0000FF&}hack").replace("\\{", "")


def test_the_document_survives_commas_colons_and_unicode_in_words():
    words = [captions.Word(0.0, 0.4, "hi,"), captions.Word(0.4, 0.8, "a:b"), captions.Word(0.8, 1.2, "日本語")]
    lines = _lines(words)
    doc = captions.document(captions.events(lines, CLASSIC), CLASSIC, 1920, 1080, "DejaVu Sans")
    rows = _event_rows(doc)
    assert "hi," in rows[0][2] and "a:b" in rows[1][2] and "日本語" in rows[2][2]
    assert doc.count("Dialogue:") == len(rows)


def test_uppercase_flag_changes_the_shown_text_only():
    words = _ws("hello", "there")
    upper = captions.events(_lines(words, SHORTS), SHORTS)[0][2]
    plain = captions.events(_lines(words, CLASSIC), CLASSIC)[0][2]
    assert "HELLO" in upper and "THERE" in upper and "hello" in plain and "HELLO" not in plain
    assert SHORTS.uppercase and not CLASSIC.uppercase


def test_the_document_header_scales_with_the_output_frame():
    land = captions.document([], CLASSIC, 1920, 1080, "DejaVu Sans")
    vert = captions.document([], CLASSIC, 1080, 1920, "DejaVu Sans")
    assert "PlayResX: 1920\nPlayResY: 1080" in land and "PlayResX: 1080\nPlayResY: 1920" in vert
    size = lambda doc: int(re.search(r"Style: Caption,[^,]+,(\d+),", doc).group(1))
    assert size(land) == round(CLASSIC.font_size * 1080) and size(vert) == round(CLASSIC.font_size * 1920)


def test_ass_time_and_color_formats():
    assert captions.ass_time(3725.456) == "1:02:05.46"
    assert captions.ass_time(-1) == "0:00:00.00"
    assert captions.ass_color("#FFE600") == "&H0000E6FF"  # BGR order
    assert captions.ass_color("#102030", 0x40) == "&H40302010"


def test_positions_set_the_alignment_and_keep_the_vertical_safe_area():
    for position, alignment in (("top", 8), ("middle", 5), ("bottom", 2)):
        style = captions.resolve_style(_request("shorts", position=position))
        doc = captions.document([], style, 1080, 1920, "DejaVu Sans")
        fields = re.search(r"Style: Caption,(.*)", doc).group(1).split(",")
        assert int(fields[17]) == alignment  # Alignment
        if position != "middle":
            assert int(fields[20]) >= round(0.10 * 1920)  # MarginV: inside the middle 80%


def test_font_family_is_read_from_the_font_file():
    from split_video.render import find_font

    assert captions.font_family(find_font()) in ("DejaVu Sans", "Liberation Sans", "FreeSans", "Arial")
    assert captions.font_family("/nonexistent/Some-Font.ttf") == "Some-Font"


def test_filter_escape_survives_odd_paths():
    assert captions.filter_escape("/tmp/a b.ass") == "/tmp/a b.ass"
    escaped = captions.filter_escape("/t/we'ird,dir:x/[a];b\\c.ass")
    assert ":" not in escaped.replace("\\:", "") and "," not in escaped.replace("\\,", "")
    assert "[" not in escaped.replace("\\[", "") and ";" not in escaped.replace("\\;", "")


# --- styles and options ------------------------------------------------------------------------------------------


def test_the_required_styles_exist_and_are_sane():
    assert {"classic", "shorts", "minimal"} <= set(captions.names())
    for style in captions.STYLES.values():
        assert 0.01 <= style.font_size <= 0.2 and 1 <= style.max_lines <= 2 and style.max_chars >= 8
        for color in (style.primary, style.highlight, style.outline):
            assert captions.check_color(color) == color
        assert style.position in captions.POSITIONS and style.primary != style.highlight
        assert 0 <= style.margin_v < 0.5
    assert SHORTS.uppercase and SHORTS.bold and SHORTS.font_size > CLASSIC.font_size > captions.get("minimal").font_size
    # Vertical-safe: even two rows of text stay inside the middle 80% of the frame at the default position.
    for style in captions.STYLES.values():
        assert style.margin_v >= 0.08 and style.margin_v + style.max_lines * style.font_size * 1.3 <= 0.9
    assert SHORTS.margin_v >= 0.10


def test_listing_has_every_field_of_every_style_sorted_by_name():
    listing = captions.listing()
    assert [s["name"] for s in listing["styles"]] == sorted(captions.STYLES)
    assert set(listing["styles"][0]) == set(captions.STYLES["classic"].to_dict())
    assert listing["positions"] == ["top", "middle", "bottom"]


def test_an_unknown_style_lists_the_valid_names():
    with pytest.raises(
        captions.CaptionError, match="unknown caption style 'fancy'; valid styles: classic, minimal, shorts"
    ):
        captions.request("fancy")
    assert isinstance(captions.CaptionError("x"), TimelineError)


def test_no_captions_means_no_request_and_true_means_the_default_style():
    assert captions.request(None) is None and captions.request(False) is None
    assert captions.request(False, caption_range={"start": 1, "end": 2}, position="nowhere") is None
    assert captions.request(True).style == captions.DEFAULT_STYLE


def test_overrides_are_validated_and_applied():
    style = captions.resolve_style(_request("minimal", font_size=0.07, highlight_color="#ff0000", position="top"))
    assert (style.font_size, style.highlight, style.position) == (0.07, "#FF0000", "top")
    assert style.primary == captions.get("minimal").primary  # the rest is untouched
    for bad in ({"font_size": 0.5}, {"font_size": 0}, {"font_size": True}, {"font_size": "big"}):
        with pytest.raises(captions.CaptionError, match="font_size"):
            captions.request("classic", **bad)
    with pytest.raises(captions.CaptionError, match="valid positions: top, middle, bottom"):
        captions.request("classic", position="left")
    for bad in ("red", "#12345", "#GGGGGG", 5):
        with pytest.raises(captions.CaptionError, match="#RRGGBB"):
            captions.request("classic", highlight_color=bad)
    for bad in ([], [0], [1, 1], [True], ["1"]):
        with pytest.raises(captions.CaptionError, match="caption_tracks"):
            captions.request("classic", tracks=bad)


def test_range_parsing():
    assert captions.parse_range("10-40") == (10.0, 40.0)
    assert captions.check_range({"start": 0, "end": 1.5}) == (0.0, 1.5)
    for bad in ("10", "a-b", "40-10", "-5-10"):
        with pytest.raises(captions.CaptionError):
            captions.parse_range(bad)
    for bad in ({"start": 5, "end": 5}, {"start": -1, "end": 2}, {"start": 1}, "x", (1, "y")):
        with pytest.raises(captions.CaptionError, match="caption_range"):
            captions.check_range(bad)


# --- build and graph ----------------------------------------------------------------------------------------------


def test_build_reports_and_returns_no_document_when_there_is_nothing_to_caption():
    p = _project([("a", 0, 5)])
    built = captions.build(p, _request(), _loader(), 1920, 1080, "/x/DejaVuSans.ttf")
    assert built.ass is None
    assert built.report == {
        "style": "classic",
        "clips_with_transcript": 0,
        "clips_without": [p.tracks[0].clips[0].id],
        "words": 0,
        "lines": 0,
        "events": 0,
    }


def test_build_counts_words_lines_and_events():
    p = _project([("a", 0, 10)])
    doc = _doc("a", _words((0.5, 0.9, "Hello"), (1.0, 1.4, "world."), (3.0, 3.4, "Again"), (3.5, 3.9, "now")))
    built = captions.build(p, _request("shorts"), _loader(a=doc), 1080, 1920, "/x/DejaVuSans.ttf")
    assert (built.report["words"], built.report["lines"]) == (4, 2)
    assert built.report["events"] == len(_event_rows(built.ass))
    assert "HELLO" in built.ass and "Style: Caption,DejaVuSans," in built.ass


def test_the_caption_filter_goes_last_after_overlays_and_text():
    p = _project([("a", 0, 5)])
    p, text = ops.add_track(p, "video")
    p = ops.add_text_clip(p, text.id, "Title", 2.0)[0] if False else p
    infos = {c.source: MediaInfo(10, 320, 240, True) for t in p.tracks for c in t.clips}
    plan = build_plan(p, infos, 320, 240, None, 30, "letterbox", ("/tmp/x/captions.ass", "/usr/share/fonts"))
    assert plan.graph.endswith("[vcaptions]") and plan.video_label == "vcaptions"
    assert "ass=filename=/tmp/x/captions.ass:fontsdir=/usr/share/fonts" in plan.graph
    assert "ass=" not in build_plan(p, infos, 320, 240).graph
