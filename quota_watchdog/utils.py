"""Shared utilities: date helpers, math, formatting."""

import datetime
from typing import Optional


def now_utc() -> datetime.datetime:
    """Return the current UTC datetime with timezone info."""
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(s: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp string; return None on failure."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def pct_of(det: dict) -> Optional[float]:
    """Compute used/limit * 100 from a detail dict. Returns None if limit <= 0."""
    try:
        limit = float(det.get("limit", 0))
        used = float(det.get("used", 0))
        if limit > 0:
            return used / limit * 100
    except (TypeError, ValueError):
        pass
    return None


def fmt_pct(pct: Optional[float]) -> str:
    """Format a percentage for display."""
    return "?" if pct is None else "%d%%" % round(pct)


def fmt_reset_short(cfg: dict, ts: Optional[datetime.datetime]) -> str:
    """Short reset string used in alerts (Chinese localized)."""
    if ts is None:
        return ""
    bj = ts.astimezone(cfg["_tz"])
    return "（重置 %d/%d %02d:%02d）" % (bj.month, bj.day, bj.hour, bj.minute)
