"""Tests for quota_watchdog.push."""

from unittest.mock import patch

from quota_watchdog.push import push


class TestPush:
    def test_no_channels(self):
        cfg = {"bark_url": "", "ntfy_url": "", "log_file": "/dev/null"}
        push(cfg, "title", "body")  # should not raise

    @patch("urllib.request.urlopen")
    def test_bark_only(self, mock_urlopen):
        cfg = {"bark_url": "https://api.day.app/FAKEKEY/", "log_file": "/dev/null"}
        push(cfg, "额度提醒", "测试消息")
        mock_urlopen.assert_called_once()
        url_str = mock_urlopen.call_args[0][0]
        assert "api.day.app" in url_str
        assert "%E9%A2%9D%E5%BA%A6" in url_str  # URL-encoded Chinese

    @patch("urllib.request.urlopen")
    def test_bark_failure_logged(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("network error")
        cfg = {"bark_url": "https://api.day.app/FAKEKEY2/", "log_file": "/dev/null"}
        push(cfg, "title", "body")  # should not raise
