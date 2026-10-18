from split_video.ffmpeg import MediaInfo
from split_video.render import build_filter_graph
from split_video.timeline import Clip


def _clip(i, dur, transition="cut", td=0.0):
    return Clip(
        id=str(i), source=f"{i}.mp4", in_point=0.0, out_point=dur, transition=transition, transition_duration=td
    )


def test_cuts_use_concat():
    graph, v, a = build_filter_graph([_clip(0, 5), _clip(1, 4)], [MediaInfo(5, 320, 240, True)] * 2, 320, 240)
    assert "concat=n=2:v=1:a=0" in graph and "concat=n=2:v=0:a=1" in graph
    assert "xfade" not in graph and (v, a) == ("vj1", "aj1")


def test_crossfade_offset_accounts_for_prior_overlap():
    clips = [_clip(0, 10), _clip(1, 8, "crossfade", 2), _clip(2, 6, "crossfade", 1)]
    graph, *_ = build_filter_graph(clips, [MediaInfo(10, 320, 240, True)] * 3, 320, 240)
    assert "xfade=transition=fade:duration=2.000:offset=8.000" in graph
    assert "xfade=transition=fade:duration=1.000:offset=15.000" in graph  # 10 + 8 - 2 - 1
    assert graph.count("acrossfade") == 2


def test_missing_audio_is_replaced_by_silence():
    graph, *_ = build_filter_graph([_clip(0, 5)], [MediaInfo(5, 320, 240, False)], 320, 240)
    assert "anullsrc" in graph and "[0:a]" not in graph
