"""Tests for quota_watchdog.config."""

import json
import os
import tempfile

from quota_watchdog.config import load_config, log


class TestLoadConfig:
    def test_defaults_when_no_file(self):
        cfg = load_config("/nonexistent/config.json")
        assert cfg["bark_url"] == ""
        assert cfg["timezone_offset_hours"] == 8
        assert "_tz" in cfg

    def test_merges_with_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"bark_url": "https://bark.example.com/key", "timezone_offset_hours": -5}, f)
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg["bark_url"] == "https://bark.example.com/key"
            assert cfg["timezone_offset_hours"] == -5
            assert cfg["_tz"].utcoffset(None).total_seconds() == -5 * 3600
        finally:
            os.unlink(fname)

    def test_default_thresholds(self):
        cfg = load_config("/nonexistent/config.json")
        th = cfg["thresholds"]
        assert th["high_5h"] == 80
        assert th["high_week"] == 90
        assert th["expiry_alert_days"] == [7, 3, 1]

    def test_custom_thresholds_merge(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"thresholds": {"high_5h": 70}}, f)
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg["thresholds"]["high_5h"] == 70  # overridden
            assert cfg["thresholds"]["high_week"] == 90  # from defaults
        finally:
            os.unlink(fname)

    def test_expands_user_paths(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"state_file": "~/test-state.json"}, f)
            fname = f.name
        try:
            cfg = load_config(fname)
            assert "~" not in cfg["state_file"]
            assert cfg["state_file"] == os.path.expanduser("~/test-state.json")
        finally:
            os.unlink(fname)


class TestLog:
    def test_writes_to_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            log_path = f.name
        try:
            cfg = {"log_file": log_path}
            log(cfg, "test message")
            with open(log_path) as f:
                content = f.read()
            assert "test message" in content
        finally:
            os.unlink(log_path)

    def test_silent_oserror(self):
        cfg = {"log_file": "/nonexistent/deep/path/log.log"}
        log(cfg, "should not crash")  # no exception expected
