"""CLI entry point for llm-quota-watchdog."""

import argparse
import datetime
import json
import os
from typing import Any, Optional

from quota_watchdog import VERSION
from quota_watchdog.config import load_config, log
from quota_watchdog.page import cmd_page, pace_info
from quota_watchdog.providers import collect
from quota_watchdog.push import push
from quota_watchdog.utils import fmt_pct, fmt_reset_short, now_utc, parse_ts


def build_summary(cfg: dict, results: dict, state: dict, today: str) -> str:
    """Build a plain-text summary string from collected quota data."""
    lines: list[str] = []
    for acct, q in results.items():
        if isinstance(q, dict) and "error" in q:
            lines.append(acct + ": 查询失败")
            continue
        parts: list[str] = []
        for win, (pct, reset) in q.items():  # type: ignore[misc]
            label = "5h" if win == "5h" else "周"
            parts.append("%s %s%s%s" % (label, fmt_pct(pct),
                                        fmt_reset_short(cfg, reset),
                                        _pace_note(pct, reset, win)))
        lines.append("%s: %s" % (acct, " · ".join(parts)))
    now = now_utc()
    for name, date_str in (cfg.get("plan_expiry") or {}).items():
        exp = parse_ts(str(date_str) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
        if exp:
            days_left = (exp.date() - now.astimezone(cfg["_tz"]).date()).days
            lines.append("%s 套餐: %d 天后到期（%s）" % (name, max(days_left, 0), date_str))
    return "\n".join(lines)


def _pace_note(pct: Optional[float], reset: Optional[datetime.datetime],
               win: str) -> str:
    """Short pace annotation for text summaries."""
    pi = pace_info(pct, reset, win)
    if pi is None:
        return ""
    return " · 时间进度%d%% %s" % (round(pi[0]), pi[1])


def cmd_watchdog(cfg: dict, summary_mode: bool) -> None:
    """Check quotas, evaluate alert rules, and push notifications."""
    th = cfg["thresholds"]
    results = collect(cfg)

    state: dict[str, Any] = {}
    if os.path.exists(cfg["state_file"]):
        try:
            with open(cfg["state_file"]) as f:
                state = json.load(f)
        except Exception:
            state = {}

    alerts: list[str] = []
    now = now_utc()
    today = now.astimezone(cfg["_tz"]).date().isoformat()
    relaxed = set(cfg.get("relaxed_accounts") or [])

    for acct, q in results.items():
        if isinstance(q, dict) and "error" in q:
            alerts.append(acct + " 查询失败: " + str(q["error"])[:60])
            continue
        for win, (pct, reset) in q.items():  # type: ignore[misc]
            if pct is None:
                continue
            pkey = "prev|%s|%s" % (acct, win)
            prev: Optional[float] = state.get(pkey)
            state[pkey] = pct
            if prev is not None and prev - pct >= th["refill_drop"] and reset is not None:
                rkey = "refill|%s|%s|%s" % (acct, win, reset.isoformat())
                if not state.get(rkey):
                    alerts.append("【满血复活】%s %s额度已重置，当前已用 %s"
                                  % (acct, "5h" if win == "5h" else "周", fmt_pct(pct)))
                    state[rkey] = True
            limit = th["high_5h"] if win == "5h" else th["high_week"]
            key = acct + "|" + win + "|high"
            if pct >= limit and not state.get(key):
                alerts.append("【快用完】%s %s窗口已用 %s（≥%d%%）" % (acct, win, fmt_pct(pct), limit))
                state[key] = True
            elif pct < limit - 15:
                state.pop(key, None)
            if acct in relaxed:
                continue
            if win != "5h" and reset is not None:
                start = reset - datetime.timedelta(days=7)
                total = (reset - start).total_seconds()
                if total <= 0:
                    continue
                elapsed = min(max((now - start).total_seconds() / total * 100, 0), 100)
                hours_left = (reset - now).total_seconds() / 3600
                rst = reset.isoformat()
                fkey = "%s|%s|fast|%s" % (acct, win, rst)
                if pct >= elapsed + th["fast_margin"] and elapsed <= 80 and not state.get(fkey):
                    alerts.append("【用太快】%s 周窗口时间才过 %d%%，额度已用 %s，按这个速度撑不到重置"
                                  % (acct, round(elapsed), fmt_pct(pct)))
                    state[fkey] = True
                w1key = "%s|%s|waste1|%s" % (acct, win, rst)
                if (elapsed >= th["waste_mid_elapsed"]
                        and pct <= elapsed - th["waste_margin"]
                        and not state.get(w1key)):
                    alerts.append("【赶紧用】%s 周窗口时间已过 %d%%，额度才用 %s，别浪费了"
                                  % (acct, round(elapsed), fmt_pct(pct)))
                    state[w1key] = True
                w2key = "%s|%s|waste2|%s" % (acct, win, rst)
                if (0 < hours_left <= th["waste_hours_left"]
                        and pct <= th["waste_pct"]
                        and not state.get(w2key)):
                    alerts.append("【赶紧用】%s 周额度才用 %s，%d 小时后重置，不用就浪费了"
                                  % (acct, fmt_pct(pct), round(hours_left)))
                    state[w2key] = True

    for name, date_str in (cfg.get("plan_expiry") or {}).items():
        exp = parse_ts(str(date_str) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
        if exp is None:
            continue
        days_left = (exp.date() - now.astimezone(cfg["_tz"]).date()).days
        for d in th["expiry_alert_days"]:
            ekey = "expiry|%s|%s|%d" % (name, date_str, d)
            if days_left <= d and not state.get(ekey):
                alerts.append("【套餐到期】%s 套餐还有 %d 天到期（%s）" % (name, max(days_left, 0), date_str))
                state[ekey] = True
                break

    with open(cfg["state_file"], "w") as f:
        json.dump(state, f)

    summary = build_summary(cfg, results, state, today)
    log(cfg, "summary: " + summary.replace("\n", " | "))
    if alerts:
        push(cfg, "额度提醒", "\n".join(alerts) + "\n——\n" + summary)
    elif summary_mode:
        push(cfg, "每日额度报告", summary)


def main() -> None:
    """Parse CLI args and dispatch to the appropriate command."""
    ap = argparse.ArgumentParser(
        description="llm-quota-watchdog: LLM coding-plan quota dashboard + alerts")
    ap.add_argument("command", choices=["watchdog", "page"])
    ap.add_argument("--summary", action="store_true",
                    help="watchdog: always push the full summary")
    ap.add_argument("--config",
                    default=os.environ.get("QUOTA_WATCHDOG_CONFIG", "./config.json"))
    ap.add_argument("--version", action="version",
                    version="%(prog)s " + VERSION)
    args = ap.parse_args()

    cfg = load_config(os.path.expanduser(args.config))
    if args.command == "watchdog":
        cmd_watchdog(cfg, args.summary)
    else:
        cmd_page(cfg)
