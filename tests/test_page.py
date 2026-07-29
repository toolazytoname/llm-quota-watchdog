"""Tests for quota_watchdog.page."""

import datetime

from quota_watchdog.page import bar_color, fmt_reset_page, pace_info


class TestPaceInfo:
    def test_none_input(self):
        assert pace_info(None, datetime.datetime.now(datetime.timezone.utc), "5h") is None
        assert pace_info(50.0, None, "5h") is None
        assert pace_info(50.0, datetime.datetime.now(datetime.timezone.utc), "unknown") is None

    def test_normal_pace(self):
        """A recently reset window should show low elapsed time."""
        now = datetime.datetime.now(datetime.timezone.utc)
        reset = now + datetime.timedelta(hours=5)  # just started
        pi = pace_info(10.0, reset, "5h")
        assert pi is not None
        elapsed, verdict = pi
        assert elapsed >= 0

    def test_fast_pace(self):
        """Usage far ahead of time should be '偏快'."""
        now = datetime.datetime.now(datetime.timezone.utc)
        reset = now + datetime.timedelta(hours=4, minutes=30)  # 30min into 5h window
        pi = pace_info(90.0, reset, "5h")  # 30min elapsed but 90% used
        assert pi is not None
        assert pi[1] == "偏快"

    def test_slow_pace(self):
        """Usage far behind time should be '偏慢'."""
        reset = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4, minutes=30)
        pi = pace_info(1.0, reset, "5h")  # 90% elapsed but only 1% used
        assert pi is not None
        assert pi[1] == "偏慢"


class TestBarColor:
    def test_none(self):
        assert bar_color(None) == "#555"

    def test_green(self):
        assert bar_color(30) == "#3fb950"

    def test_yellow(self):
        assert bar_color(70) == "#d4a72c"
        assert bar_color(60) == "#d4a72c"

    def test_red(self):
        assert bar_color(85) == "#e5534b"
        assert bar_color(100) == "#e5534b"


class TestFmtResetPage:
    def test_none(self):
        cfg = {"_tz": datetime.timezone(datetime.timedelta(hours=8))}
        assert fmt_reset_page(cfg, None) == "重置时间未知"

    def test_future_reset(self):
        cfg = {"_tz": datetime.timezone(datetime.timedelta(hours=8))}
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        result = fmt_reset_page(cfg, future)
        assert "重置" in result
        assert "小时后" in result
