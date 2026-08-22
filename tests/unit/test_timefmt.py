from split_video.timefmt import format_timestamp


def test_short_file_uses_minutes_and_seconds():
    assert format_timestamp(0, total_duration=200.0) == "0:00"
    assert format_timestamp(65, total_duration=200.0) == "1:05"
    assert format_timestamp(199.6, total_duration=200.0) == "3:20"


def test_long_file_uses_hours_minutes_and_seconds():
    assert format_timestamp(90, total_duration=3700.0) == "0:01:30"
    assert format_timestamp(3661, total_duration=3700.0) == "1:01:01"


def test_rounds_to_the_nearest_second():
    assert format_timestamp(59.6, total_duration=200.0) == "1:00"
