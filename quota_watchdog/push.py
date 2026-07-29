"""Push notification channels: Bark, ntfy."""

import urllib.parse
import urllib.request
from typing import Optional

from quota_watchdog.config import log


def push(cfg: dict, title: str, body: str) -> None:
    """Send a push notification via configured channels (Bark and/or ntfy)."""
    sent = False
    bark: Optional[str] = (cfg.get("bark_url") or "").strip().rstrip("/")
    if bark:
        url = (bark + "/" + urllib.parse.quote(title, safe="") + "/"
               + urllib.parse.quote(body, safe="") + "?group=QuotaWatchdog")
        try:
            urllib.request.urlopen(url, timeout=15)
            sent = True
        except Exception as e:
            log(cfg, "bark error: " + str(e))
    ntfy: Optional[str] = (cfg.get("ntfy_url") or "").strip()
    if ntfy:
        try:
            req = urllib.request.Request(
                ntfy, data=body.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8").decode("latin-1", "ignore") or "quota",
                    "Tags": "chart_with_upwards_trend",
                })
            urllib.request.urlopen(req, timeout=15)
            sent = True
        except Exception as e:
            log(cfg, "ntfy error: " + str(e))
    if not sent:
        log(cfg, "push skipped (no channel configured or all failed): " + title)
