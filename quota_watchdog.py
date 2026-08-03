#!/usr/bin/env python3
"""
llm-quota-watchdog — quota dashboard + smart alerts for LLM coding-plan subscriptions.

Supported providers:
  - Claude Pro/Max   (via CLIProxyAPI OAuth auth file -> api.anthropic.com/api/oauth/usage)
  - Codex Plus/Pro   (via CLIProxyAPI OAuth auth file -> chatgpt.com/backend-api/wham/usage)
  - Kimi for Coding  (via API key -> api.kimi.com/coding/v1/usages)

Subcommands:
  watchdog             check quotas, push alerts only when rules trigger
  watchdog --summary   always push the full daily summary (plus any alerts)
  page                 generate a static HTML dashboard
  check-auth           optional: push when CLIProxyAPI auth-file health changes
                        (only does anything if cliproxyapi_management_key_file is
                        set in config; run this from its own low-frequency cron
                        line, e.g. daily — it is not part of the hourly watchdog
                        loop on purpose, this check doesn't need to be frequent)

Python 3.8+, stdlib only. No third-party dependencies.
"""
import argparse
import calendar
import datetime
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"

DEFAULTS = {
    "bark_url": "",                 # e.g. https://api.day.app/YOUR_KEY/
    "ntfy_url": "",                 # e.g. https://ntfy.sh/your-topic (optional)
    "cliproxyapi_auth_dir": "~/.cli-proxy-api",
    "cliproxyapi_management_key_file": "",   # optional: enables auth-file health checks
    "cliproxyapi_management_url": "http://127.0.0.1:8317/v0/management/auth-files",
    "accounts": [],                 # manual accounts, e.g. kimi (see config.example.json)
    "relaxed_accounts": [],         # labels that only get nearly-used-up alerts
    "plan_expiry": {},              # {"Kimi Coding": "2026-08-22"}
    "monthly_snapshot": {},         # {"Kimi Coding": {"pct": 21.4, "reset": "2026-08-22", "updated": "2026-07-29"}}
    "thresholds": {},
    "timezone_offset_hours": 8,
    "state_file": "./quota-watchdog-state.json",
    "page_out_dir": "./www",
    "log_file": "./quota-watchdog.log",
}

THRESH = {
    "high_5h": 80,                  # nearly-used-up: 5h window %
    "high_week": 90,                # nearly-used-up: weekly window %
    "fast_margin": 15,              # burning-too-fast: usage ahead of time pace (points)
    "waste_mid_elapsed": 50,        # mid-cycle waste: elapsed >= this %
    "waste_margin": 30,             # ...and usage behind by this many points
    "waste_hours_left": 26,         # near-reset waste: reset within this many hours
    "waste_pct": 60,                # ...and usage <= this %
    "refill_drop": 30,              # refill detection: drop of this many points
    "expiry_alert_days": [7, 3, 1],
}

# ---------------------------------------------------------------- utils

def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    th = dict(THRESH)
    th.update(cfg.get("thresholds") or {})
    cfg["thresholds"] = th
    cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=cfg["timezone_offset_hours"]))
    for k in ("state_file", "page_out_dir", "log_file"):
        cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def log(cfg, msg):
    try:
        with open(cfg["log_file"], "a") as f:
            f.write(datetime.datetime.now().isoformat() + " " + msg + "\n")
    except OSError:
        pass


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=20))


def pct_of(det):
    try:
        limit = float(det.get("limit", 0))
        used = float(det.get("used", 0))
        if limit > 0:
            return used / limit * 100
    except (TypeError, ValueError):
        pass
    return None


def fmt_pct(pct):
    return "?" if pct is None else ("%d%%" % round(pct))


def load_state(cfg):
    if os.path.exists(cfg["state_file"]):
        try:
            with open(cfg["state_file"]) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(cfg, state):
    with open(cfg["state_file"], "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- providers
# each returns {"5h": (pct, reset_dt|None), "7d": (pct, reset_dt|None)}

def claude_quota(auth_file):
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


def codex_quota(auth_file):
    with open(auth_file) as f:
        d = json.load(f)
    r = http_get("https://chatgpt.com/backend-api/wham/usage", {
        "Authorization": "Bearer " + d["access_token"],
        "ChatGPT-Account-Id": d["account_id"],
        "User-Agent": "codex_cli_rs/0.114.0",
    })
    rl = r.get("rate_limit") or {}
    out = {}
    for key, fallback in (("primary_window", "5h"), ("secondary_window", "7d")):
        w = rl.get(key)
        if not w:
            continue
        reset = parse_ts(w.get("reset_at"))
        if reset is None and w.get("reset_after_seconds"):
            reset = now_utc() + datetime.timedelta(seconds=w["reset_after_seconds"])
        # label by actual window length: >6h to reset means weekly window
        if reset is not None:
            label = "5h" if (reset - now_utc()).total_seconds() < 6 * 3600 else "7d"
        else:
            label = fallback
        out[label] = (w.get("used_percent"), reset)
    return out


def kimi_quota(api_key):
    r = http_get("https://api.kimi.com/coding/v1/usages", {
        "Authorization": "Bearer " + api_key,
    })
    out = {}
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


def collect(cfg):
    """Discover accounts and query every provider. Returns {label: windows|{"error": msg}}.

    For auto-discovered CLIProxyAPI accounts, label is the auth filename without
    the ".json" suffix — this lets cmd_page match it back against the
    management-API health check (which reports filenames) without a second
    directory scan.
    """
    results = {}

    # 1) auto-discover Claude/Codex OAuth files from the CLIProxyAPI auth dir
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
                label = fn[:-5]  # strip .json
                results[label] = (claude_quota if ptype == "claude" else codex_quota)(path)
            except Exception as e:
                results[fn[:-5]] = {"error": str(e)[:120]}

    # 2) manual accounts (kimi, or explicit claude/codex with custom labels)
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


# ---------------------------------------------------------------- push

def push(cfg, title, body):
    sent = False
    bark = (cfg.get("bark_url") or "").strip().rstrip("/")
    if bark:
        url = (bark + "/" + urllib.parse.quote(title, safe="") + "/"
               + urllib.parse.quote(body, safe="") + "?group=QuotaWatchdog")
        try:
            urllib.request.urlopen(url, timeout=15)
            sent = True
        except Exception as e:
            log(cfg, "bark error: " + str(e))
    ntfy = (cfg.get("ntfy_url") or "").strip()
    if ntfy:
        try:
            req = urllib.request.Request(
                ntfy, data=body.encode("utf-8"),
                headers={"Title": title.encode("utf-8").decode("latin-1", "ignore") or "quota",
                         "Tags": "chart_with_upwards_trend"})
            urllib.request.urlopen(req, timeout=15)
            sent = True
        except Exception as e:
            log(cfg, "ntfy error: " + str(e))
    if not sent:
        log(cfg, "push skipped (no channel configured or all failed): " + title)


# ---------------------------------------------------------------- watchdog

WIN_SECONDS = {"5h": 5 * 3600, "7d": 7 * 86400}

def _pace(pct, start, reset):
    if pct is None or start is None or reset is None:
        return None
    total = (reset - start).total_seconds()
    if total <= 0:
        return None
    elapsed = (now_utc().timestamp() - start.timestamp()) / total * 100
    elapsed = min(max(elapsed, 0), 100)
    diff = pct - elapsed
    verdict = "偏快" if diff > 5 else ("偏慢" if diff < -5 else "正常")
    return elapsed, verdict

def pace_info(pct, reset, win):
    """Compare usage pct against fair pace (elapsed time fraction of the window)."""
    if win not in WIN_SECONDS or reset is None:
        return None
    return _pace(pct, reset - datetime.timedelta(seconds=WIN_SECONDS[win]), reset)

def pace_note(pct, reset, win):
    pi = pace_info(pct, reset, win)
    if pi is None:
        return ""
    return " · 时间进度%d%% %s" % (round(pi[0]), pi[1])

def month_before(dt):
    """dt shifted back one calendar month, clamping the day for short months."""
    y, m = dt.year, dt.month - 1
    if m == 0:
        m, y = 12, y - 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)

def monthly_pace(pct, reset):
    """Same as pace_info but for a manually-snapshotted monthly quota, whose
    window is defined as "one calendar month ending at reset"."""
    if reset is None:
        return None
    return _pace(pct, month_before(reset), reset)

def fmt_reset_short(cfg, ts):
    if ts is None:
        return ""
    bj = ts.astimezone(cfg["_tz"])
    return "（重置 %d/%d %02d:%02d）" % (bj.month, bj.day, bj.hour, bj.minute)


def cmd_watchdog(cfg, summary_mode):
    th = cfg["thresholds"]
    results = collect(cfg)

    state = load_state(cfg)
    alerts = []
    now = now_utc()
    today = now.astimezone(cfg["_tz"]).date().isoformat()
    relaxed = set(cfg.get("relaxed_accounts") or [])

    for acct, q in results.items():
        if "error" in q:
            alerts.append(acct + " 查询失败: " + q["error"][:60])
            continue
        for win, (pct, reset) in q.items():
            if pct is None:
                continue
            # refill detection: big drop means the window reset (good news, all accounts)
            pkey = "prev|%s|%s" % (acct, win)
            prev = state.get(pkey)
            state[pkey] = pct
            if prev is not None and prev - pct >= th["refill_drop"] and reset is not None:
                rkey = "refill|%s|%s|%s" % (acct, win, reset.isoformat())
                if not state.get(rkey):
                    alerts.append("【满血复活】%s %s额度已重置，当前已用 %s"
                                  % (acct, "5h" if win == "5h" else "周", fmt_pct(pct)))
                    state[rkey] = True
            # nearly-used-up (all accounts, with reset countdown for sprint planning)
            limit = th["high_5h"] if win == "5h" else th["high_week"]
            key = acct + "|" + win + "|high"
            if pct >= limit and not state.get(key):
                eta = ""
                if reset is not None:
                    hrs = (reset - now).total_seconds() / 3600
                    if hrs > 0:
                        eta = "，%.1f小时后重置可继续" % hrs if hrs < 24 else "，%.1f天后重置" % (hrs / 24)
                alerts.append("【快用完】%s %s窗口已用 %s（≥%d%%）%s" % (acct, win, fmt_pct(pct), limit, eta))
                state[key] = True
            elif pct < limit - 15:
                state.pop(key, None)
            # everything below is skipped for relaxed accounts
            if acct in relaxed:
                continue
            # pacing alerts (weekly windows)
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
                if (elapsed >= th["waste_mid_elapsed"] and pct <= elapsed - th["waste_margin"]
                        and not state.get(w1key)):
                    alerts.append("【赶紧用】%s 周窗口时间已过 %d%%，额度才用 %s，别浪费了"
                                  % (acct, round(elapsed), fmt_pct(pct)))
                    state[w1key] = True
                w2key = "%s|%s|waste2|%s" % (acct, win, rst)
                if 0 < hours_left <= th["waste_hours_left"] and pct <= th["waste_pct"] and not state.get(w2key):
                    alerts.append("【赶紧用】%s 周额度才用 %s，%d 小时后重置，不用就浪费了"
                                  % (acct, fmt_pct(pct), round(hours_left)))
                    state[w2key] = True

    # plan expiry reminders
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

    save_state(cfg, state)

    summary = build_summary(cfg, results, state, today)
    log(cfg, "summary: " + summary.replace("\n", " | "))
    if alerts:
        push(cfg, "额度提醒", "\n".join(alerts) + "\n——\n" + summary)
    elif summary_mode:
        push(cfg, "每日额度报告", summary)


def build_summary(cfg, results, state, today):
    lines = []
    for acct, q in results.items():
        if "error" in q:
            lines.append(acct + ": 查询失败")
            continue
        parts = []
        for win, (pct, reset) in q.items():
            label = "5h" if win == "5h" else "周"
            parts.append("%s %s%s%s" % (label, fmt_pct(pct), fmt_reset_short(cfg, reset), pace_note(pct, reset, win)))
        lines.append("%s: %s" % (acct, " · ".join(parts)))
    now = now_utc()
    for name, date_str in (cfg.get("plan_expiry") or {}).items():
        exp = parse_ts(str(date_str) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
        if exp:
            days_left = (exp.date() - now.astimezone(cfg["_tz"]).date()).days
            lines.append("%s 套餐: %d 天后到期（%s）" % (name, max(days_left, 0), date_str))
    return "\n".join(lines)


# ---------------------------------------------------------------- auth health (optional)

def auth_health_map(cfg):
    """Query CLIProxyAPI's own local management API for auth-file health.

    Optional feature: returns None (meaning "not configured / unavailable") unless
    cliproxyapi_management_key_file is set. Users who don't run CLIProxyAPI, or
    don't want this dependency, simply never set that config key and every call
    site here degrades to "no health info" instead of failing.
    """
    key_file = cfg.get("cliproxyapi_management_key_file")
    if not key_file:
        return None
    try:
        mk = open(os.path.expanduser(key_file)).read().strip()
        url = cfg.get("cliproxyapi_management_url") or DEFAULTS["cliproxyapi_management_url"]
        r = http_get(url, {"Authorization": "Bearer " + mk})
    except Exception as e:
        log(cfg, "auth health check failed: " + str(e))
        return None
    bad = set()
    for f in r.get("files") or []:
        if f.get("disabled"):
            continue
        if f.get("status") == "error" or f.get("unavailable"):
            bad.add(f.get("name"))
    return bad


def cmd_check_auth(cfg):
    """Push only when the set of unhealthy auth files changes since last run.
    Meant to run on its own low-frequency cron line (daily is plenty) — this is
    deliberately NOT wired into the hourly cmd_watchdog loop."""
    bad_set = auth_health_map(cfg)
    if bad_set is None:
        log(cfg, "check-auth skipped: cliproxyapi_management_key_file not configured, "
                  "or the check itself failed (see previous log line)")
        return
    current_key = ",".join(sorted(bad_set)) if bad_set else "NONE"

    state = load_state(cfg)
    prev_key = state.get("auth_health_prev", "NONE")
    if current_key == prev_key:
        return
    state["auth_health_prev"] = current_key
    save_state(cfg, state)

    if current_key == "NONE":
        push(cfg, "CPA 账号恢复正常", "所有账号已恢复 active")
    else:
        push(cfg, "CPA 账号异常", " ".join(sorted(bad_set)))
    log(cfg, "auth health changed: " + current_key)


# ---------------------------------------------------------------- page

def fmt_reset_page(cfg, ts):
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


def bar_color(pct):
    if pct is None:
        return "#555"
    if pct >= 85:
        return "#e5534b"
    if pct >= 60:
        return "#d4a72c"
    return "#3fb950"


def window_html(cfg, label, pct, reset, note="", elapsed=None):
    pct_txt = "未知" if pct is None else ("%.2f%%" % pct if pct < 10 else "%.1f%%" % pct)
    width = 0 if pct is None else max(min(pct, 100), 0.5)
    marker_html = ""
    if elapsed is not None:
        marker_html = '<div class="time-marker" style="left:%.1f%%"></div>' % max(min(elapsed, 100), 0)
    note_html = '<div class="note">%s</div>' % html.escape(note) if note else ""
    return """
    <div class="win">
      <div class="win-head"><span>%s</span><span class="pct" style="color:%s">%s</span></div>
      <div class="bar"><div class="fill" style="width:%.1f%%;background:%s"></div>%s</div>
      <div class="reset">%s</div>
      %s
    </div>""" % (html.escape(label), bar_color(pct), pct_txt, width, bar_color(pct), marker_html,
                 html.escape(fmt_reset_page(cfg, reset)), note_html)


BADGE = {
    "ok": ("🟢", "正常"),
    "token_expired": ("🔴", "Token 异常"),
    "error": ("🟡", "查询异常"),
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>额度监控</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; padding: 16px; max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .updated { color: #7d8590; font-size: 12px; margin-bottom: 10px; }
  .banner { font-size: 13px; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; background: #161b22; border: 1px solid #30363d; color: #3fb950; }
  .banner.bad { color: #e5534b; border-color: #e5534b; }
  .controls { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; font-size: 13px; }
  .btn, .mini-btn { display: inline-block; background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 4px 10px; text-decoration: none; font-size: 12px; }
  .btn:hover, .mini-btn:hover { background: #30363d; }
  .controls select { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 3px 6px; font-size: 12px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
  .card-actions { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: normal; }
  .badge { color: #7d8590; }
  .win { margin-bottom: 14px; }
  .win:last-child { margin-bottom: 0; }
  .win-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 5px; }
  .pct { font-weight: 600; }
  .bar { position: relative; background: #30363d; border-radius: 6px; height: 10px; overflow: hidden; }
  .fill { height: 100%; border-radius: 6px; transition: width .3s; }
  .time-marker { position: absolute; top: 0; bottom: 0; width: 2px; background: #e6edf3; opacity: .85; }
  .reset { color: #7d8590; font-size: 12px; margin-top: 5px; }
  .note { color: #7d8590; font-size: 11px; margin-top: 3px; font-style: italic; }
  .err { color: #e5534b; font-size: 13px; }
  .expiry { color: #d4a72c; font-size: 12px; margin-top: 10px; }
</style>
</head>
<body>
<h1>📊 大模型额度监控</h1>
<div class="updated" id="updated">数据更新于 @UPDATED@ · llm-quota-watchdog</div>
<div id="banner-wrap">@BANNER@</div>
<div class="controls">
  <a class="btn refresh-link" href="/refresh">🔄 全部刷新</a>
  <label>自动刷新
    <select id="auto-refresh" onchange="setAutoRefresh(this.value)">
      <option value="0">关闭</option>
      <option value="300">5分钟</option>
      <option value="900">15分钟</option>
      <option value="1800">30分钟</option>
      <option value="3600">1小时</option>
      <option value="10800">3小时</option>
    </select>
  </label>
</div>
<div id="cards">@CARDS@</div>
<script>
(function(){
  var sel = document.getElementById('auto-refresh');
  var saved = localStorage.getItem('quotaAutoRefresh') || '0';
  sel.value = saved;
  var timer = null;

  function patchFromHtml(htmlText, account){
    var doc = new DOMParser().parseFromString(htmlText, 'text/html');
    var upd = doc.getElementById('updated');
    var ban = doc.getElementById('banner-wrap');
    if (upd) document.getElementById('updated').innerHTML = upd.innerHTML;
    if (ban) document.getElementById('banner-wrap').innerHTML = ban.innerHTML;
    if (account) {
      var sel2 = '[data-account="' + CSS.escape(account) + '"]';
      var newCard = doc.querySelector(sel2);
      var oldCard = document.querySelector(sel2);
      if (newCard && oldCard) oldCard.outerHTML = newCard.outerHTML;
    } else {
      var newCards = doc.getElementById('cards');
      var oldCards = document.getElementById('cards');
      if (newCards && oldCards) oldCards.innerHTML = newCards.innerHTML;
    }
  }

  function doRefresh(url, account){
    return fetch(url, {credentials: 'same-origin'})
      .then(function(r){ return r.text(); })
      .then(function(text){ patchFromHtml(text, account); })
      .catch(function(){ location.href = url; });
  }

  document.addEventListener('click', function(e){
    var el = e.target.closest && e.target.closest('.refresh-link');
    if (!el) return;
    e.preventDefault();
    if (el.dataset.busy) return;
    el.dataset.busy = '1';
    var prevText = el.textContent;
    el.textContent = '刷新中…';
    doRefresh(el.getAttribute('href'), el.dataset.account || null).finally(function(){
      if (document.body.contains(el)) { el.textContent = prevText; delete el.dataset.busy; }
    });
  });

  function arm(v){
    if (timer) clearTimeout(timer);
    v = parseInt(v, 10) || 0;
    if (v > 0) {
      timer = setTimeout(function(){
        doRefresh('/refresh', null).then(function(){ arm(v); });
      }, v * 1000);
    }
  }
  arm(saved);

  window.setAutoRefresh = function(v){
    localStorage.setItem('quotaAutoRefresh', v);
    arm(v);
  };
})();
</script>
</body>
</html>"""
# NOTE: "全部刷新" / per-card refresh links and the auto-refresh dropdown are
# pure frontend — they just fetch() /refresh (optionally ?account=<label>) and
# patch the DOM in place (falling back to a full navigation if fetch fails).
# This script does not ship a server for that endpoint; if you don't run one,
# the buttons simply 404 and the static page itself is unaffected. See the
# README for a minimal example of such a refresh trigger.


def cmd_page(cfg):
    results = collect(cfg)
    bad_set = auth_health_map(cfg)

    cards = []
    unhealthy = []
    healthy = 0
    for name, q in results.items():
        if bad_set is not None and (name + ".json") in bad_set:
            health = "token_expired"
        elif "error" in q:
            health = "error"
        elif bad_set is None:
            health = None
        else:
            health = "ok"
        if health is not None:
            if health == "ok":
                healthy += 1
            else:
                unhealthy.append(name)

        rows = []
        if "error" in q:
            rows.append('<div class="err">查询失败: %s</div>' % html.escape(str(q["error"])[:80]))
        else:
            for win in ("5h", "7d"):
                if win not in q:
                    continue
                pct, reset = q[win]
                pi = pace_info(pct, reset, win)
                note = "" if pi is None else "时间进度 %.0f%% · 节奏%s" % pi
                rows.append(window_html(cfg, "5小时用量" if win == "5h" else "7天用量", pct, reset, note,
                                         elapsed=(pi[0] if pi else None)))
            # optional manual monthly snapshot, shown last (widest time span)
            snap = (cfg.get("monthly_snapshot") or {}).get(name)
            if snap:
                reset_ts = parse_ts(str(snap.get("reset")) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
                pi = monthly_pace(snap.get("pct"), reset_ts)
                note = "手动更新于 %s" % snap.get("updated", "?")
                if pi:
                    note += " · 时间进度 %.0f%% · 节奏%s" % pi
                rows.append(window_html(cfg, "月度总配额", snap.get("pct"), reset_ts, note,
                                         elapsed=(pi[0] if pi else None)))
        expiry_html = ""
        exp = (cfg.get("plan_expiry") or {}).get(name)
        if exp:
            exp_ts = parse_ts(str(exp) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
            if exp_ts:
                days = (exp_ts.date() - now_utc().astimezone(cfg["_tz"]).date()).days
                expiry_html = '<div class="expiry">📅 套餐周期 %d 天后重置（%s）</div>' % (max(days, 0), exp)

        badge_html = ""
        if health is not None:
            dot, htext = BADGE[health]
            badge_html = '<span class="badge">%s %s</span>' % (dot, html.escape(htext))
        refresh_link = "/refresh?account=" + urllib.parse.quote(name)
        name_esc = html.escape(name)
        cards.append(
            '<div class="card" data-account="%s"><h2><span>%s</span><span class="card-actions">%s'
            '<a class="mini-btn refresh-link" href="%s" data-account="%s">刷新</a></span></h2>%s%s</div>'
            % (name_esc, name_esc, badge_html, refresh_link, name_esc, "".join(rows), expiry_html))

    if unhealthy:
        banner_html = '<div class="banner bad">⚠️ %s 异常，其余正常</div>' % html.escape("、".join(unhealthy))
    elif bad_set is not None:
        banner_html = '<div class="banner">✅ %d/%d 账号正常</div>' % (healthy, len(results))
    else:
        banner_html = ""

    out_dir = cfg["page_out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    page = (PAGE_TEMPLATE
            .replace("@UPDATED@", now_utc().astimezone(cfg["_tz"]).strftime("%m月%d日 %H:%M"))
            .replace("@BANNER@", banner_html)
            .replace("@CARDS@", "".join(cards)))
    tmp = os.path.join(out_dir, ".index.html.tmp")
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, os.path.join(out_dir, "index.html"))
    log(cfg, "page generated: " + os.path.join(out_dir, "index.html"))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="llm-quota-watchdog: LLM coding-plan quota dashboard + alerts")
    ap.add_argument("command", choices=["watchdog", "page", "check-auth"])
    ap.add_argument("--summary", action="store_true", help="watchdog: always push the full summary")
    ap.add_argument("--config", default=os.environ.get("QUOTA_WATCHDOG_CONFIG", "./config.json"))
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = ap.parse_args()

    cfg = load_config(os.path.expanduser(args.config))
    if args.command == "watchdog":
        cmd_watchdog(cfg, args.summary)
    elif args.command == "page":
        cmd_page(cfg)
    else:
        cmd_check_auth(cfg)


if __name__ == "__main__":
    main()
