"""Tests for quota_watchdog.utils."""

import datetime

import pytest

from quota_watchdog.utils import fmt_pct, fmt_reset_short, now_utc, parse_ts, pct_of


class TestNowUtc:
    def test_returns_aware_datetime(self):
        dt = now_utc()
        assert dt.tzinfo is not None
        assert dt.tzinfo.utcoffset(dt).total_seconds() == 0  # UTC


class TestParseTs:
    def test_none_input(self):
        assert parse_ts(None) is None

    def test_empty_input(self):
        assert parse_ts("") is None

    def test_iso_with_z(self):
        dt = parse_ts("2026-07-29T12:00:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.day == 29
        assert dt.hour == 12
        assert dt.minute == 0

    def test_iso_with_offset(self):
        dt = parse_ts("2026-07-29T14:00:00+02:00")
        assert dt is not None
        assert dt.hour == 14

    def test_invalid_string(self):
        assert parse_ts("not-a-date") is None


class TestPctOf:
    def test_normal_calculation(self):
        det = {"limit": 100, "used": 45}
        result = pct_of(det)
        assert result is not None
        assert result == pytest.approx(45.0)

    def test_zero_limit(self):
        det = {"limit": 0, "used": 50}
        assert pct_of(det) is None

    def test_missing_keys(self):
        det = {}
        assert pct_of(det) is None

    def test_non_numeric(self):
        det = {"limit": "abc", "used": "def"}
        assert pct_of(det) is None


class TestFmtPct:
    def test_normal(self):
        assert fmt_pct(42.3) == "42%"

    def test_none(self):
        assert fmt_pct(None) == "?"

    def test_rounding(self):
        assert fmt_pct(99.9) == "100%"

    def test_zero(self):
        assert fmt_pct(0) == "0%"


class TestFmtResetShort:
    def test_none(self):
        cfg = {"_tz": datetime.timezone(datetime.timedelta(hours=8))}
        assert fmt_reset_short(cfg, None) == ""

    def test_with_ts(self):
        cfg = {"_tz": datetime.timezone(datetime.timedelta(hours=8))}
        ts = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.timezone.utc)
        result = fmt_reset_short(cfg, ts)
        assert "重置" in result
        assert "7/30" in result
        assert "22:00" in result  # UTC+8
