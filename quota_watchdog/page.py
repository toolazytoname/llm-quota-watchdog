"""Static HTML dashboard generation."""

import html
import os
from typing import Optional

from quota_watchdog import WIN_SECONDS
from quota_watchdog.config import log
from quota_watchdog.providers import collect
from quota_watchdog.utils import now_utc, parse_ts

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>额度监控</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; padding: 16px; max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .updated { color: #7d8590; font-size: 12px; margin-bottom: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; }
  .card h2 .plan { color: #7d8590; font-size: 12px; font-weight: normal; margin-left: 6px; }
  .win { margin-bottom: 14px; }
  .win:last-child { margin-bottom: 0; }
  .win-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 5px; }
  .pct { font-weight: 600; }
  .bar { background: #30363d; border-radius: 6px; height: 10px; overflow: hidden; }
  .fill { height: 100%; border-radius: 6px; transition: width .3s; }
  .reset { color: #7d8590; font-size: 12px; margin-top: 5px; }
  .note { color: #7d8590; font-size: 11px; margin-top: 3px; font-style: italic; }
  .err { color: #e5534b; font-size: 13px; }
  .expiry { color: #d4a72c; font-size: 12px; margin-top: 10px; }
</style>
</head>
<body>
<h1>📊 大模型额度监控</h1>
<div class="updated">数据更新于 @UPDATED@ · llm-quota-watchdog v@VERSION@</div>
@CARDS@
</body>
</html>"""


def pace_info(pct: Optional[float], reset: Optional["datetime.datetime"],  # noqa: F821 — forward ref
              win: str) -> Optional[tuple[float, str]]:
    """Compare usage pct against fair pace (elapsed time fraction of the window).

    Returns (elapsed_pct, verdict_label) or None if data is incomplete.
    """
    if pct is None or reset is None or win not in WIN_SECONDS:
        return None
    total = WIN_SECONDS[win]
    start_ts = reset.timestamp() - total
    elapsed = (now_utc().timestamp() - start_ts) / total * 100
    elapsed = min(max(elapsed, 0), 100)
    diff = pct - elapsed
    verdict = "偏快" if diff > 5 else ("偏慢" if diff < -5 else "正常")
    return elapsed, verdict


def fmt_reset_page(cfg: dict, ts: Optional["datetime.datetime"]) -> str:  # noqa: F821
    """Human-readable reset time for the HTML page."""
    if ts is None:
        return "重置时间未知"
    bj = ts.astimezone(cfg["_tz"])
    delta = ts - now_utc()
    hours = delta.total_seconds() / 3600
    if hours < 0:
        left = "即将重置"
    elif hours < 1:
        left = "%d分钟后" % round(delta.total_seconds() / 60)
    elif hours < 24:
        left = "%d小时后" % round(hours)
    else:
        left = "%d天后" % round(hours / 24)
    return "%d月%d日 %02d:%02d 重置（%s）" % (bj.month, bj.day, bj.hour, bj.minute, left)


def bar_color(pct: Optional[float]) -> str:
    """Color for the progress bar based on usage percentage."""
    if pct is None:
        return "#555"
    if pct >= 85:
        return "#e5534b"
    if pct >= 60:
        return "#d4a72c"
    return "#3fb950"


def window_html(cfg: dict, label: str, pct: Optional[float],
                reset: Optional["datetime.datetime"], note: str = "") -> str:  # noqa: F821
    """Render a single window card (progress bar + labels) as HTML."""
    pct_txt = "未知" if pct is None else ("%.2f%%" % pct if pct < 10 else "%.1f%%" % pct)
    width = 0 if pct is None else max(min(pct, 100), 0.5)
    note_html = '<div class="note">%s</div>' % html.escape(note) if note else ""
    return """
    <div class="win">
      <div class="win-head"><span>%s</span><span class="pct" style="color:%s">%s</span></div>
      <div class="bar"><div class="fill" style="width:%.1f%%;background:%s"></div></div>
      <div class="reset">%s</div>
      %s
    </div>""" % (html.escape(label), bar_color(pct), pct_txt, width, bar_color(pct),
                 html.escape(fmt_reset_page(cfg, reset)), note_html)


def cmd_page(cfg: dict) -> None:
    """Generate the static HTML dashboard and write it atomically to disk."""

    from quota_watchdog import VERSION

    results = collect(cfg)
    cards: list[str] = []
    now = now_utc()

    for name, q in results.items():
        rows: list[str] = []
        if "error" in q:
            err_msg = str(q["error"])[:80] if isinstance(q["error"], str) else ""
            rows.append('<div class="err">查询失败: %s</div>' % html.escape(err_msg))
        else:
            snap = (cfg.get("monthly_snapshot") or {}).get(name)
            if snap:
                reset = parse_ts(str(snap.get("reset")) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
                rows.append(window_html(
                    cfg, "月度总配额", snap.get("pct"), reset,
                    "手动更新于 %s" % snap.get("updated", "?"),
                ))
            for win in ("5h", "7d"):
                if win not in q:
                    continue
                pct, reset = q[win]  # type: ignore[misc]
                pi = pace_info(pct, reset, win)
                note = "" if pi is None else "时间进度 %.0f%% · 节奏%s" % pi
                rows.append(window_html(
                    cfg, "5小时用量" if win == "5h" else "7天用量", pct, reset, note,
                ))
        expiry_html = ""
        exp = (cfg.get("plan_expiry") or {}).get(name)
        if exp:
            exp_ts = parse_ts(str(exp) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
            if exp_ts:
                days = (exp_ts.date() - now.astimezone(cfg["_tz"]).date()).days
                expiry_html = '<div class="expiry">📅 套餐周期 %d 天后重置（%s）</div>' % (max(days, 0), exp)
        cards.append('<div class="card"><h2>%s</h2>%s%s</div>' % (html.escape(name), "".join(rows), expiry_html))

    out_dir = cfg["page_out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    page = (PAGE_TEMPLATE
            .replace("@UPDATED@", now.astimezone(cfg["_tz"]).strftime("%m月%d日 %H:%M"))
            .replace("@VERSION@", VERSION)
            .replace("@CARDS@", "".join(cards)))
    tmp = os.path.join(out_dir, ".index.html.tmp")
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, os.path.join(out_dir, "index.html"))
    log(cfg, "page generated: " + os.path.join(out_dir, "index.html"))
