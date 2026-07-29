"""Tests for quota_watchdog.app."""

import json
import os
import tempfile

from quota_watchdog.app import build_summary
from quota_watchdog.config import load_config


class TestBuildSummary:
    def setup_method(self):
        self.cfg = load_config("/nonexistent/config.json")

    def test_empty_results(self):
        summary = build_summary(self.cfg, {}, {}, "2026-07-29")
        assert summary == ""

    def test_account_with_error(self):
        results = {"My Claude": {"error": "token expired"}}
        summary = build_summary(self.cfg, results, {}, "2026-07-29")
        assert "查询失败" in summary

    def test_account_with_windows(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        reset_5h = now + datetime.timedelta(hours=2)
        reset_7d = now + datetime.timedelta(days=3)
        results = {
            "My Claude": {
                "5h": (45.0, reset_5h),
                "7d": (72.5, reset_7d),
            }
        }
        summary = build_summary(self.cfg, results, {}, "2026-07-29")
        assert "My Claude" in summary
        assert "45%" in summary
        assert "72%" in summary

    def test_plan_expiry(self):
        cfg = dict(self.cfg)
        cfg["plan_expiry"] = {"Kimi Coding": "2026-08-22"}
        results = {}
        summary = build_summary(cfg, results, {}, "2026-07-29")
        assert "Kimi Coding" in summary
        assert "到期" in summary


class TestCli:
    def test_version_flag(self):
        """Verify --version exits with version string."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "quota_watchdog", "--version"],
            capture_output=True, text=True,
            cwd="/tmp/llm-quota-watchdog",
        )
        assert result.returncode == 0
        assert "1.1.0" in result.stdout

    def test_cli_no_config_help(self):
        """Verify CLI shows help with no args."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "quota_watchdog"],
            capture_output=True, text=True,
            cwd="/tmp/llm-quota-watchdog",
        )
        # Should show error about required command argument
        assert result.returncode != 0 or "usage:" in result.stdout or "usage:" in result.stderr

    def test_page_command_creates_html(self):
        """Verify the page command creates an HTML file."""
        import subprocess
        import sys
        # Create minimal config with empty auth dir and tmpdir for output
        outdir = tempfile.mkdtemp()
        config_data = {
            "page_out_dir": outdir,
            "cliproxyapi_auth_dir": "",
            "accounts": [],
        }
        fd, cfg_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(config_data, f)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "quota_watchdog", "page", "--config", cfg_path],
                capture_output=True, text=True,
                cwd="/tmp/llm-quota-watchdog",
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            html_path = os.path.join(outdir, "index.html")
            assert os.path.exists(html_path)
            with open(html_path) as f:
                html = f.read()
            assert "大模型额度监控" in html or "llm-quota-watchdog" in html
        finally:
            os.unlink(cfg_path)
            import shutil
            shutil.rmtree(outdir, ignore_errors=True)
