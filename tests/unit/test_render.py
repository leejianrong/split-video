from split_video import timeline_ops as ops
from split_video.ffmpeg import MediaInfo
from split_video.render import build_plan
from split_video.timeline import empty_project

INFO = MediaInfo(10, 320, 240, True)


def _main(durations, sources=None):
    p = empty_project()
    tid = p.tracks[0].id
    for i, d in enumerate(durations):
        p, _ = ops.add_clip(p, tid, f"{i}.mp4", d)
    return p, tid


def _infos(p, info=INFO):
    return {c.source: info for t in p.tracks for c in t.clips}


def test_cuts_use_concat():
    p, _ = _main([5, 4])
    plan = build_plan(p, _infos(p), 320, 240)
    assert "concat=n=2:v=1:a=0" in plan.graph and "concat=n=2:v=0:a=1" in plan.graph
    assert "xfade" not in plan.graph and plan.audio_label == "aj1"


def test_crossfade_offset_accounts_for_prior_overlap():
    p, _ = _main([10, 8, 6])
    clips = p.tracks[0].clips
    p = ops.set_transition(p, clips[1].id, "crossfade", 2)
    p = ops.set_transition(p, clips[2].id, "crossfade", 1)
    graph = build_plan(p, _infos(p), 320, 240).graph
    assert "xfade=transition=fade:duration=2.000:offset=8.000" in graph
    assert "xfade=transition=fade:duration=1.000:offset=15.000" in graph  # 10 + 8 - 2 - 1
    assert graph.count("acrossfade") == 2


def test_missing_audio_is_replaced_by_silence():
    p, _ = _main([5])
    graph = build_plan(p, _infos(p, MediaInfo(5, 320, 240, False)), 320, 240).graph
    assert "anullsrc" in graph and "[0:a]" not in graph


def test_overlay_clip_is_positioned_scaled_and_windowed():
    p, _ = _main([10])
    p, overlay = ops.add_track(p, "video")
    p, clip = ops.add_clip(p, overlay.id, "pip.mp4", 3.0, start=2.0)
    p = ops.set_clip_properties(p, clip.id, x=0.8, y=0.2, scale=0.25, opacity=0.5)
    plan = build_plan(p, _infos(p), 320, 240)
    assert "[1:v]setpts=PTS-STARTPTS+2.000/TB" in plan.graph
    assert "scale=80:-2" in plan.graph and "colorchannelmixer=aa=0.5" in plan.graph
    assert "overlay=x=256-w/2:y=48-h/2:enable='between(t,2.000,5.000)'" in plan.graph
    assert plan.video_label == "vo0" and [c.source for c in plan.clips] == ["0.mp4", "pip.mp4"]


def test_overlays_stack_in_track_order():
    p, _ = _main([10])
    p, t1 = ops.add_track(p, "video")
    p, t2 = ops.add_track(p, "video")
    p, _ = ops.add_clip(p, t2.id, "top.mp4", 2.0, start=0.0)
    p, _ = ops.add_clip(p, t1.id, "low.mp4", 2.0, start=0.0)
    plan = build_plan(p, _infos(p), 320, 240)
    assert [c.source for c in plan.clips] == ["0.mp4", "low.mp4", "top.mp4"]


def test_no_main_clips_renders_over_a_black_canvas():
    p = empty_project()
    p, overlay = ops.add_track(p, "video")
    p, _ = ops.add_clip(p, overlay.id, "pip.mp4", 3.0, start=1.0)
    plan = build_plan(p, _infos(p), 320, 240)
    assert "color=c=black" in plan.graph and "vbase" not in plan.graph and "asilent" in plan.graph


def _audio_graph(p):
    return build_plan(p, _infos(p), 320, 240)


def test_main_clip_gain_and_mute_are_applied_before_the_join():
    p, tid = _main([5, 4])
    a, b = p.tracks[0].clips
    p = ops.set_clip_properties(p, a.id, gain=2.0)
    p = ops.set_clip_properties(p, b.id, muted=True)
    graph = _audio_graph(p).graph
    assert "volume=2[a0]" in graph and "volume=0[a1]" in graph


def test_audio_track_clip_is_delayed_scaled_and_mixed():
    p, tid = _main([10])
    p, music = ops.add_track(p, "audio")
    p, clip = ops.add_clip(p, music.id, "m.wav", 8.0, start=2.5)
    p = ops.set_clip_properties(p, clip.id, gain=0.5)
    plan = _audio_graph(p)
    assert "volume=0.5,adelay=2500|2500[ax0]" in plan.graph
    assert "[a0][ax0]amix=inputs=2:normalize=0" in plan.graph and plan.audio_label == "amixed"


def test_muted_and_audio_less_clips_add_nothing_to_the_mix():
    p, tid = _main([10])
    p, music = ops.add_track(p, "audio")
    p, a = ops.add_clip(p, music.id, "m1.wav", 3.0, start=0.0)
    p, b = ops.add_clip(p, music.id, "m2.wav", 3.0, start=4.0)
    p = ops.set_clip_properties(p, a.id, muted=True)
    infos = _infos(p)
    infos["m2.wav"] = MediaInfo(3, None, None, False)
    plan = build_plan(p, infos, 320, 240)
    assert "amix" not in plan.graph and plan.audio_label == "a0"


def test_overlay_video_clip_sound_is_mixed_in():
    p, tid = _main([10])
    p, overlay = ops.add_track(p, "video")
    p, _ = ops.add_clip(p, overlay.id, "pip.mp4", 3.0, start=1.0)
    plan = _audio_graph(p)
    assert "[1:a]" in plan.graph and "adelay=1000|1000[ax0]" in plan.graph
