"""Tests for quota_watchdog.providers."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from quota_watchdog.providers import claude_quota, codex_quota, collect, http_get, kimi_quota


class TestHttpGet:
    def test_success(self):
        """Verify http_get calls urllib and returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"key": "value"}'

        with patch("urllib.request.urlopen", return_value=mock_response) as mock:
            result = http_get("https://example.com/api", {"Authorization": "Bearer test"})
            assert result == {"key": "value"}
            mock.assert_called_once()


class TestClaudeQuota:
    def test_returns_windows(self):
        """Verify claude_quota parses API response correctly."""
        api_response = {
            "five_hour": {"utilization": 45.0, "resets_at": "2026-07-29T17:00:00Z"},
            "seven_day": {"utilization": 72.3, "resets_at": "2026-07-31T00:00:00Z"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"access_token": "tok_abc"}, f)
            auth_path = f.name
        try:
            with patch("quota_watchdog.providers.http_get", return_value=api_response):
                result = claude_quota(auth_path)
                assert "5h" in result
                assert "7d" in result
                pct_5h, reset_5h = result["5h"]
                assert pct_5h == 45.0
                assert reset_5h is not None
                assert reset_5h.hour == 17
                pct_7d, reset_7d = result["7d"]
                assert pct_7d == 72.3
        finally:
            os.unlink(auth_path)

    def test_missing_fields(self):
        """Gracefully handle missing API fields."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"access_token": "tok_abc"}, f)
            auth_path = f.name
        try:
            with patch("quota_watchdog.providers.http_get", return_value={}):
                result = claude_quota(auth_path)
                pct_5h, _ = result["5h"]
                pct_7d, _ = result["7d"]
                assert pct_5h is None
                assert pct_7d is None
        finally:
            os.unlink(auth_path)


class TestCodexQuota:
    def test_returns_windows(self):
        api_response = {
            "rate_limit": {
                "primary_window": {"used_percent": 30.0, "reset_at": "2026-07-29T16:00:00Z"},
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"access_token": "tok_abc", "account_id": "acc_123"}, f)
            auth_path = f.name
        try:
            with patch("quota_watchdog.providers.http_get", return_value=api_response):
                result = codex_quota(auth_path)
                assert "5h" in result or "7d" in result
        finally:
            os.unlink(auth_path)

    def test_no_windows(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"access_token": "tok_abc", "account_id": "acc_123"}, f)
            auth_path = f.name
        try:
            with patch("quota_watchdog.providers.http_get", return_value={}):
                result = codex_quota(auth_path)
                assert result == {}
        finally:
            os.unlink(auth_path)


class TestKimiQuota:
    def test_returns_windows(self):
        api_response = {
            "usage": {"used": 50, "limit": 200, "resetTime": "2026-08-01T00:00:00Z"},
            "limits": [
                {
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"used": 10, "limit": 100, "resetTime": "2026-07-29T18:00:00Z"},
                }
            ],
        }
        with patch("quota_watchdog.providers.http_get", return_value=api_response):
            result = kimi_quota("sk-test-key")
            assert "7d" in result
            pct_7d, _ = result["7d"]
            assert pct_7d == pytest.approx(25.0)
            assert "5h" in result
            pct_5h, _ = result["5h"]
            assert pct_5h == pytest.approx(10.0)


class TestCollect:
    def test_no_auth_dir_empty(self):
        cfg = {
            "cliproxyapi_auth_dir": "",
            "accounts": [],
        }
        result = collect(cfg)
        assert result == {}

    def test_manual_kimi_account(self):
        cfg = {
            "cliproxyapi_auth_dir": "",
            "accounts": [
                {"type": "kimi", "label": "My Kimi", "api_key": "sk-fake-key"},
            ],
        }
        with patch("quota_watchdog.providers.http_get") as mock_get:
            mock_get.return_value = {
                "usage": {"used": 10, "limit": 100, "resetTime": "2026-08-01T00:00:00Z"},
            }
            result = collect(cfg)
            assert "My Kimi" in result
            assert "error" not in result["My Kimi"]

    def test_account_error_caught(self):
        cfg = {
            "cliproxyapi_auth_dir": "",
            "accounts": [
                {"type": "kimi", "label": "Bad Kimi", "api_key": ""},
            ],
        }
        result = collect(cfg)
        assert "Bad Kimi" not in result  # no key, silently skipped
