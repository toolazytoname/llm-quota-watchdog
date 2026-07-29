"""Provider-specific quota API clients (Claude, Codex, Kimi)."""

import datetime
import json
import os
import urllib.request
from typing import Any, Callable, Optional, Union

from quota_watchdog.utils import now_utc, parse_ts, pct_of


def http_get(url: str, headers: dict[str, str]) -> Any:
    """HTTP GET returning parsed JSON. Raises on non-2xx or JSON decode error."""
    req = urllib.request.Request(url, headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=20))


# ---------------------------------------------------------------------------
# Provider quota fetchers
# Each returns dict[str, tuple[Optional[float], Optional[datetime.datetime]]]
# mapping window label ("5h"|"7d") to (usage_pct, reset_timestamp).
# ---------------------------------------------------------------------------


def claude_quota(auth_file: str) -> dict[str, tuple[Optional[float], Optional[datetime.datetime]]]:
    """Fetch Claude Pro/Max quota from anthropic OAuth usage endpoint."""
    with open(auth_file) as f:
        d = json.load(f)
    r = http_get("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": "Bearer " + d["access_token"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.1.44 (external, sdk-cli)",
    })
    fh, sd = r.get("five_hour") or {}, r.get("seven_day") or {}
    return {
        "5h": (fh.get("utilization"), parse_ts(fh.get("resets_at"))),
        "7d": (sd.get("utilization"), parse_ts(sd.get("resets_at"))),
    }


def codex_quota(auth_file: str) -> dict[str, tuple[Optional[float], Optional[datetime.datetime]]]:
    """Fetch Codex Plus/Pro quota from ChatGPT wham/usage endpoint."""
    with open(auth_file) as f:
        d = json.load(f)
    r = http_get("https://chatgpt.com/backend-api/wham/usage", {
        "Authorization": "Bearer " + d["access_token"],
        "ChatGPT-Account-Id": d["account_id"],
        "User-Agent": "codex_cli_rs/0.114.0",
    })
    rl = r.get("rate_limit") or {}
    out: dict[str, tuple[Optional[float], Optional[datetime.datetime]]] = {}
    for key, fallback in (("primary_window", "5h"), ("secondary_window", "7d")):
        w = rl.get(key)
        if not w:
            continue
        reset = parse_ts(w.get("reset_at"))
        if reset is None and w.get("reset_after_seconds"):
            reset = now_utc() + datetime.timedelta(seconds=w["reset_after_seconds"])
        if reset is not None:
            label = "5h" if (reset - now_utc()).total_seconds() < 6 * 3600 else "7d"
        else:
            label = fallback  # type: ignore[assignment]
        out[label] = (w.get("used_percent"), reset)
    return out


def kimi_quota(api_key: str) -> dict[str, tuple[Optional[float], Optional[datetime.datetime]]]:
    """Fetch Kimi for Coding quota from Kimi API."""
    r = http_get("https://api.kimi.com/coding/v1/usages", {
        "Authorization": "Bearer " + api_key,
    })
    out: dict[str, tuple[Optional[float], Optional[datetime.datetime]]] = {}
    u = r.get("usage") or {}
    out["7d"] = (pct_of(u), parse_ts(u.get("resetTime")))
    for item in (r.get("limits") or []):
        if not isinstance(item, dict):
            continue
        win = item.get("window") or {}
        det = item.get("detail") or {}
        if win.get("duration") == 300 and win.get("timeUnit") == "TIME_UNIT_MINUTE":
            out["5h"] = (pct_of(det), parse_ts(det.get("resetTime")))
    return out


def collect(cfg: dict) -> dict[str, Union[dict, dict[str, tuple[Optional[float], Optional[datetime.datetime]]]]]:
    """Discover accounts and query every provider.

    Returns dict mapping label -> either a window dict (same shape as
    provider return values) or ``{"error": "..."}``.
    """
    results: dict[str, Union[dict, dict[str, tuple[Optional[float], Optional[datetime.datetime]]]]] = {}

    auth_dir = os.path.expanduser(cfg.get("cliproxyapi_auth_dir") or "")
    if auth_dir and os.path.isdir(auth_dir):
        for fn in sorted(os.listdir(auth_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(auth_dir, fn)
            try:
                with open(path) as f:
                    head = json.load(f)
                ptype = head.get("type")
                if ptype not in ("claude", "codex") or head.get("disabled"):
                    continue
                label = fn[:-5]
                fetcher: Callable[[str], dict] = claude_quota if ptype == "claude" else codex_quota
                results[label] = fetcher(path)
            except Exception as e:
                results[fn[:-5]] = {"error": str(e)[:120]}

    for acc in cfg.get("accounts") or []:
        label = acc.get("label") or acc.get("type", "?")
        try:
            t = acc.get("type")
            if t == "kimi":
                key = acc.get("api_key") or ""
                if not key and acc.get("api_key_file"):
                    with open(os.path.expanduser(acc["api_key_file"])) as f:
                        key = f.read().strip()
                if key:
                    results[label] = kimi_quota(key)
            elif t in ("claude", "codex") and acc.get("auth_file"):
                fn = claude_quota if t == "claude" else codex_quota
                results[label] = fn(os.path.expanduser(acc["auth_file"]))
        except Exception as e:
            results[label] = {"error": str(e)[:120]}
    return results
