from split_video.silence import SilenceInterval, parse_silencedetect_output

SAMPLE_STDERR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'liveshow.mp4':
  Duration: 00:03:20.00, start: 0.000000, bitrate: 128 kb/s
[silencedetect @ 0x55d1b2a3f2c0] silence_start: 12.3456
[silencedetect @ 0x55d1b2a3f2c0] silence_end: 15.6789 | silence_duration: 3.3333
[silencedetect @ 0x55d1b2a3f2c0] silence_start: 90.0
[silencedetect @ 0x55d1b2a3f2c0] silence_end: 94.5 | silence_duration: 4.5
"""


def test_parses_multiple_complete_intervals():
    intervals = parse_silencedetect_output(SAMPLE_STDERR, total_duration=200.0)
    assert intervals == [
        SilenceInterval(12.3456, 15.6789),
        SilenceInterval(90.0, 94.5),
    ]


def test_no_silence_lines_returns_empty_list():
    assert parse_silencedetect_output("nothing relevant here\n", total_duration=200.0) == []


def test_unterminated_silence_at_end_of_stream_uses_total_duration():
    text = (
        "[silencedetect @ 0x0] silence_start: 12.0\n"
        "[silencedetect @ 0x0] silence_end: 14.0 | silence_duration: 2.0\n"
        "[silencedetect @ 0x0] silence_start: 190.0\n"
    )
    intervals = parse_silencedetect_output(text, total_duration=200.0)
    assert intervals == [
        SilenceInterval(12.0, 14.0),
        SilenceInterval(190.0, 200.0),
    ]
