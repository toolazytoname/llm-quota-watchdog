#!/usr/bin/env python3
"""
llm-quota-watchdog — quota dashboard + smart alerts for LLM coding-plan subscriptions.

Supported providers:
  - Claude Pro/Max   (via CLIProxyAPI OAuth auth file -> api.anthropic.com/api/oauth/usage)
  - Codex Plus/Pro   (via CLIProxyAPI OAuth auth file -> chatgpt.com/backend-api/wham/usage)
  - Kimi for Coding  (via API key -> api.kimi.com/coding/v1/usages)
  - GLM Coding Plan  (via API key -> open.bigmodel.cn/api/monitor/usage/quota/limit)

Subcommands:
  watchdog             check quotas, push alerts only when rules trigger
  watchdog --summary   always push the full daily summary (plus any alerts)
  page                 generate a static HTML dashboard
  page --account LBL   only re-query that account, serve the rest from the page
                        cache (repeatable) — this is what the per-card refresh
                        button on the dashboard is meant to call
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
import math
import os
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.7.2"

DEFAULTS = {
    "bark_url": "",                 # e.g. https://api.day.app/YOUR_KEY/
    "bark_url_file": "",            # ...or keep the key out of config.json, in a chmod-600 file
    "ntfy_url": "",                 # e.g. https://ntfy.sh/your-topic (optional)
    "ntfy_url_file": "",
    "cliproxyapi_auth_dir": "~/.cli-proxy-api",
    "cliproxyapi_management_key_file": "",   # optional: enables auth-file health checks
    "cliproxyapi_management_url": "http://127.0.0.1:8317/v0/management/auth-files",
    "accounts": [],                 # manual accounts, e.g. kimi (see config.example.json)
    "relaxed_accounts": [],         # labels that only get nearly-used-up alerts
    "plan_expiry": {},              # {"Kimi Coding": "2026-08-22"}
    "monthly_snapshot": {},         # {"Kimi Coding": {"pct": 21.4, "reset": "2026-08-22", "updated": "2026-07-29"}}
    "monthly_live_file": "./quota-monthly-live.json",  # auto-refreshed monthly snapshots (see refresh_monthly_from_web); merged over monthly_snapshot at load time, never written back to config.json
    "thresholds": {},
    "timezone_offset_hours": 8,
    "state_file": "./quota-watchdog-state.json",
    "page_state_file": "./quota-page-state.json",
    "page_out_dir": "./www",
    "log_file": "./quota-watchdog.log",
    "page_title": "大模型额度监控",
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
    "high_month": 60,               # nearly-used-up: auto-refreshed monthly quota %
    "expiry_alert_days": [7, 3, 1],
}

# ---------------------------------------------------------------- utils

def load_config(path):
    cfg = dict(DEFAULTS)
    user = {}
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        cfg.update(user)
    th = dict(THRESH)
    th.update(cfg.get("thresholds") or {})
    cfg["thresholds"] = th
    # a config.json written before the page cache existed won't name one; keep it
    # next to the alert state instead of in whatever directory cron started in
    if "page_state_file" not in user:
        cfg["page_state_file"] = os.path.join(
            os.path.dirname(cfg["state_file"]) or ".", "quota-page-state.json")
    if "monthly_live_file" not in user:
        cfg["monthly_live_file"] = os.path.join(
            os.path.dirname(cfg["state_file"]) or ".", "quota-monthly-live.json")
    cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=cfg["timezone_offset_hours"]))
    for k in ("state_file", "page_state_file", "page_out_dir", "log_file", "monthly_live_file"):
        cfg[k] = os.path.expanduser(cfg[k])
    # auto-refreshed monthly snapshots (see refresh_monthly_from_web) take priority
    # over the manually-typed-in ones with the same label
    cfg["monthly_snapshot"] = dict(cfg.get("monthly_snapshot") or {})
    cfg["monthly_snapshot"].update(load_json_file(cfg["monthly_live_file"], {}))
    return cfg


def read_secret(cfg, inline_key, file_key):
    """Config values that may be secrets can live inline or in a chmod-600 file;
    the file wins when both are set."""
    path = (cfg.get(file_key) or "").strip()
    if path:
        try:
            with open(os.path.expanduser(path)) as f:
                return f.read().strip()
        except OSError as e:
            log(cfg, "cannot read %s (%s): %s" % (file_key, path, e))
    return (cfg.get(inline_key) or "").strip()


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


def http_post_json(url, headers, body):
    req = urllib.request.Request(url, headers=headers, method="POST",
                                  data=json.dumps(body).encode())
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


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json_file(path, data):
    """Atomic write: the page state is touched by both the cron run and any
    on-demand refresh trigger, and a half-written file would lose every
    account's cached quota."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_state(cfg):
    return load_json_file(cfg["state_file"], {})


def save_state(cfg, state):
    save_json_file(cfg["state_file"], state)


def load_page_state(cfg):
    state = load_json_file(cfg["page_state_file"], {})
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def save_page_state(cfg, state):
    save_json_file(cfg["page_state_file"], state)


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


def kimi_monthly_quota(web_token):
    """Kimi's coding-plan usage API (kimi_quota above) has no monthly-total
    field — only a 5h/7d rolling window. The actual monthly quota only shows
    up on the web console, behind a browser-login JWT instead of the coding
    API key, via the GetSubscription RPC. Raises on any failure (network,
    expired token, unexpected shape) so the caller can tell "no monthly data"
    apart from "token needs replacing"."""
    r = http_post_json(
        "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscription",
        {"Authorization": "Bearer " + web_token, "Content-Type": "application/json"},
        {})
    balances = r.get("balances") or []
    sub = next((b for b in balances if b.get("type") == "SUBSCRIPTION"), None) or balances[0]
    pct = float(sub["amountUsedRatio"]) * 100
    reset = parse_ts(sub.get("expireTime"))
    return pct, reset


def glm_quota(api_key):
    """GLM Coding Plan (Zhipu). The Authorization header carries the API key
    directly — NO "Bearer " prefix, unlike Claude/Codex. Response data.limits[]
    has one entry per window; each window's reset is a millisecond epoch in
    nextResetTime. GLM's current Coding Plan response identifies its 5-hour
    and weekly buckets as (unit, number) = (3, 5) and (6, 1). Prefer those
    stable identifiers: time-to-reset alone misclassifies a weekly bucket as
    5-hour during its final six hours. Unknown future shapes fall back to the
    reset-time heuristic. 'percentage' is an integer we recompute from
    used/total for sub-integer precision.
    """
    r = http_get("https://open.bigmodel.cn/api/monitor/usage/quota/limit", {
        "Authorization": api_key,
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    })
    out = {}
    now = now_utc()
    for item in ((r.get("data") or {}).get("limits") or []):
        if not isinstance(item, dict):
            continue
        reset = None
        reset_ms = item.get("nextResetTime")
        if isinstance(reset_ms, (int, float)) and reset_ms > 0:
            reset = datetime.datetime.fromtimestamp(reset_ms / 1000, tz=datetime.timezone.utc)
        # 'usage' is confusingly the total quota; 'currentValue' is used
        pct = None
        try:
            total = float(item.get("usage") or 0)
            if total > 0:
                pct = float(item.get("currentValue") or 0) / total * 100
        except (TypeError, ValueError):
            pct = None
        if pct is None and item.get("percentage") is not None:
            try:
                pct = float(item["percentage"])
            except (TypeError, ValueError):
                pass
        if pct is None:
            continue
        # Prefer GLM's explicit bucket identifiers. In particular, a weekly
        # window can have <6h left immediately before reset, which previously
        # made it collide with the real 5h window and disappear from the page.
        unit, number = item.get("unit"), item.get("number")
        if unit == 3 and number == 5:
            label = "5h"
        elif unit == 6 and number == 1:
            label = "7d"
        # Unknown future shapes: classify by time-to-reset (<6h / <8d).
        elif reset is not None:
            hrs = (reset - now).total_seconds() / 3600
            if hrs < 6:
                label = "5h"
            elif hrs < 192:
                label = "7d"
            else:
                continue
        else:
            label = "5h" if unit == 3 else "7d"
        if label not in out:
            out[label] = (pct, reset)
    return out


def classify_error(e):
    """401/403 means the credential itself went bad, which deserves a louder
    badge than a network blip that will fix itself."""
    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
        return "token_expired"
    return "error"


def account_list(cfg):
    """Every account to show, in display order.

    Explicitly configured accounts come first (config order — that's also the
    page's default card order), then any CLIProxyAPI auth file not already
    claimed by one of them. Claiming is matched on the auth filename, so
    configuring one of your two Codex accounts by hand still auto-discovers the
    other instead of silently dropping it.
    """
    accounts = []
    claimed = set()
    for acc in cfg.get("accounts") or []:
        if acc.get("type") not in ("claude", "codex", "kimi", "glm"):
            continue
        entry = dict(acc)
        entry["label"] = acc.get("label") or acc["type"]
        if acc.get("auth_file"):
            entry["auth_file"] = os.path.expanduser(acc["auth_file"])
            claimed.add(os.path.basename(entry["auth_file"]))
        accounts.append(entry)

    auth_dir = os.path.expanduser(cfg.get("cliproxyapi_auth_dir") or "")
    if auth_dir and os.path.isdir(auth_dir):
        for fn in sorted(os.listdir(auth_dir)):
            if not fn.endswith(".json") or fn in claimed:
                continue
            path = os.path.join(auth_dir, fn)
            try:
                with open(path) as f:
                    head = json.load(f)
            except Exception as e:
                log(cfg, "skipping unreadable auth file %s: %s" % (fn, e))
                continue
            if head.get("type") not in ("claude", "codex") or head.get("disabled"):
                continue
            # label defaults to the filename minus .json; set an explicit
            # account entry in config.json if you want a prettier card title
            accounts.append({"type": head["type"], "label": fn[:-5], "auth_file": path})
    return accounts


def fetch_one(cfg, acct):
    """Query one account's quota. Never raises — a dead account comes back as
    {"error": ..., "error_kind": ...} so it can't blank out everyone else's card.
    Returns None when the account isn't usable at all (no key configured)."""
    t = acct.get("type")
    try:
        if t in ("kimi", "glm"):
            key = acct.get("api_key") or ""
            if not key and acct.get("api_key_file"):
                kf = os.path.expanduser(acct["api_key_file"])
                if os.path.exists(kf):
                    with open(kf) as f:
                        key = f.read().strip()
            if not key:
                return None  # no key configured -> skip silently (no error card)
            windows = (kimi_quota if t == "kimi" else glm_quota)(key)
        elif t in ("claude", "codex") and acct.get("auth_file"):
            fn = claude_quota if t == "claude" else codex_quota
            windows = fn(os.path.expanduser(acct["auth_file"]))
        else:
            return None
    except Exception as e:
        return {"error": str(e)[:160], "error_kind": classify_error(e)}
    if not windows:
        # GLM at least answers 200 with an error body when the key is bad. Taking
        # that as success would replace real numbers with a blank green card, so
        # "no windows at all" counts as a failed fetch and the cache is kept.
        return {"error": "响应里没有任何额度窗口（key 失效或接口有变？）", "error_kind": "error"}
    return {"windows": windows}


def collect(cfg):
    """Query every account live. Returns {label: windows|{"error": msg}}.

    This is the watchdog's path on purpose: alerts have to fire on fresh numbers,
    and it only runs hourly. The page reads its own cache instead (see cmd_page)
    so that clicking refresh on one card doesn't re-poll every provider.
    """
    results = {}
    for acct in account_list(cfg):
        r = fetch_one(cfg, acct)
        if r is None:
            continue
        results[acct["label"]] = r["windows"] if "windows" in r else {"error": r["error"]}
    return results



# ---------------------------------------------------------------- push

def push(cfg, title, body):
    sent = False
    bark = read_secret(cfg, "bark_url", "bark_url_file").rstrip("/")
    if bark:
        url = (bark + "/" + urllib.parse.quote(title, safe="") + "/"
               + urllib.parse.quote(body, safe="") + "?group=QuotaWatchdog")
        try:
            urllib.request.urlopen(url, timeout=15)
            sent = True
        except Exception as e:
            log(cfg, "bark error: " + str(e))
    ntfy = read_secret(cfg, "ntfy_url", "ntfy_url_file")
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


def refresh_monthly_from_web(cfg, state):
    """Auto-refresh monthly quota for accounts with monthly_web_token_file
    (currently just Kimi — see kimi_monthly_quota), independent of the 5h/7d
    windows pipeline in cmd_watchdog: auth here is a separate web-login token,
    and a stale/expired token shouldn't blank out that account's whole card,
    just its monthly row. Returns a list of alert strings; mutates state and
    writes cfg["monthly_live_file"] as a side effect."""
    alerts = []
    th = cfg["thresholds"]
    live = dict(load_json_file(cfg["monthly_live_file"], {}))
    today = now_utc().astimezone(cfg["_tz"]).date().isoformat()

    for acct in cfg.get("accounts") or []:
        tok_file = acct.get("monthly_web_token_file")
        if not tok_file:
            continue
        label = acct.get("label") or acct["type"]
        errkey = "monthly_token_error|" + label
        try:
            with open(os.path.expanduser(tok_file)) as f:
                token = f.read().strip()
            pct, reset = kimi_monthly_quota(token)
        except Exception as e:
            if not state.get(errkey):
                alerts.append("【Token失效】%s 网页月度额度 token 已失效（%s），"
                               "请重新登录 kimi.com 换取新 token" % (label, str(e)[:120]))
                state[errkey] = True
            continue
        state.pop(errkey, None)

        live[label] = {"pct": round(pct, 2),
                        "reset": reset.date().isoformat() if reset else None,
                        "updated": today, "source": "auto"}

        key = "monthly_high|" + label
        if pct >= th["high_month"] and not state.get(key):
            eta = ""
            if reset is not None:
                days = (reset - now_utc()).total_seconds() / 86400
                if days > 0:
                    eta = "，%.1f 天后重置" % days
            alerts.append("【快用完】%s 月度额度已用 %s（≥%d%%）%s"
                           % (label, fmt_pct(pct), th["high_month"], eta))
            state[key] = True
        elif pct < th["high_month"] - 15:
            state.pop(key, None)

        pkey = "monthly_prev|" + label
        prev = state.get(pkey)
        state[pkey] = pct
        if prev is not None and prev - pct >= th["refill_drop"]:
            alerts.append("【满血复活】%s 月度额度已重置，当前已用 %s" % (label, fmt_pct(pct)))

    save_json_file(cfg["monthly_live_file"], live)
    cfg["monthly_snapshot"].update(live)
    return alerts


def cmd_watchdog(cfg, summary_mode):
    th = cfg["thresholds"]
    results = collect(cfg)

    state = load_state(cfg)
    alerts = []
    alerts.extend(refresh_monthly_from_web(cfg, state))
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

def reset_left(ts):
    """Just the countdown ("2小时后"). Rendered into a data attribute too, so the
    page's summary line can quote it without re-parsing the full string."""
    if ts is None:
        return ""
    delta = ts - now_utc()
    hours = delta.total_seconds() / 3600
    if hours < 0:
        return "即将重置"
    if hours < 1:
        return "%d分钟后" % round(delta.total_seconds() / 60)
    if hours < 24:
        return "%d小时后" % round(hours)
    return "%d天后" % round(hours / 24)


def fmt_reset_page(cfg, ts):
    if ts is None:
        return "重置时间未知"
    bj = ts.astimezone(cfg["_tz"])
    return "%d月%d日 %02d:%02d 重置（%s）" % (bj.month, bj.day, bj.hour, bj.minute, reset_left(ts))



def fill_class(pct):
    """The bar fill is neutral until a window is actually near its ceiling;
    colour is a signal, not decoration, so a healthy 40% is the same grey as 5%."""
    if pct is None:
        return ""
    if pct >= 90:
        return "crit"
    if pct >= 75:
        return "high"
    return ""


def quota_capacity_info(acct, window=None):
    """Return a three-step capacity tier and its human-readable label.

    ``quota_factor`` records the real within-provider plan multiplier, while
    ``capacity_index`` is an explicitly approximate cross-provider index.
    Provider units are not directly comparable (Codex messages, GLM credits,
    Kimi internal units), so the cross-provider index is allowed to be a rough
    operator estimate and is labelled as such in the UI. Usage tracks always
    remain full-width and represent only percentage consumed; plan size is
    encoded separately as a small / medium / large three-step marker. The
    configured multiplier remains visible as text, so the marker is a scan aid
    rather than a claim that unlike provider units are precisely comparable.

    Accounts without a cross-provider index fall back to their provider factor;
    accounts with neither simply omit the tier marker.
    """
    factors = acct.get("quota_factors") or {}
    indexes = acct.get("capacity_indexes") or {}
    labels = acct.get("quota_labels") or {}
    factor_raw = factors.get(window, acct.get("quota_factor"))
    index_raw = indexes.get(window, acct.get("capacity_index"))
    label = str(labels.get(window, acct.get("quota_label") or ""))

    def positive_number(raw):
        if raw in (None, ""):
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    index = positive_number(index_raw)
    factor = positive_number(factor_raw)
    scale = index if index is not None else factor
    if scale is None:
        return 0, label
    tier = 1 if scale <= 1 else (2 if scale <= 6 else 3)
    if index is not None and not label:
        label = "跨平台≈%g×" % index
    elif not label:
        label = "%g×" % factor
    return tier, label


def window_html(cfg, label, pct, reset, note="", elapsed=None,
                capacity_tier=0, quota_label=""):
    """One quota bar. Its data attributes drive summary and client-side sorts.
    The title keeps reset time and pace reachable in the mini density, which
    hides both to stay one row tall."""
    pct_txt = "未知" if pct is None else ("%.2f%%" % pct if pct < 10 else "%.1f%%" % pct)
    fill_width = "0" if pct is None or pct <= 0 else "max(3px, %.1f%%)" % min(pct, 100)
    marker_html = ""
    if elapsed is not None:
        marker_html = '<div class="time-marker" style="left:%.1f%%"></div>' % max(min(elapsed, 100), 0)
    # colour the pace verdict word only, not the whole meta line — fast burns
    # amber, slow burns a muted blue, on-track stays dim grey
    note_cls = ""
    if note:
        if "偏快" in note:
            note_cls = " pace-fast"
        elif "偏慢" in note:
            note_cls = " pace-slow"
    note_html = '<span class="note%s">%s</span>' % (note_cls, html.escape(note)) if note else ""
    reset_txt = fmt_reset_page(cfg, reset)
    reset_iso = reset.astimezone(datetime.timezone.utc).isoformat() if reset else ""
    fc = fill_class(pct)
    fill_cls = ("fill " + fc).strip()
    # the percentage picks up the same state colour as its bar, so a near-ceiling
    # window reads amber at a glance from the number alone
    pct_cls = ("pct " + fc).strip()
    capacity_html = ""
    if quota_label:
        steps_html = ""
        if capacity_tier:
            tier_name = {1: "小档", 2: "中档", 3: "大档"}.get(capacity_tier, "容量档位")
            steps = "".join(
                '<i class="capacity-step%s"></i>' % (" on" if n <= capacity_tier else "")
                for n in range(1, 4)
            )
            steps_html = ('<span class="capacity-steps" role="img" aria-label="额度规模%s">%s</span>'
                          % (tier_name, steps))
        capacity_html = ('<span class="capacity"><span>额度规模</span><strong>%s</strong>%s</span>'
                         % (html.escape(quota_label), steps_html))
    title_txt = "%s / %s%s%s" % (
        label, reset_txt, " / " + note if note else "",
        " / 总量 " + quota_label if quota_label else "")
    return """
    <div class="win" data-pct="%s" data-short="%s" data-reset-short="%s" data-reset-at="%s" data-capacity="%s" title="%s">
      <div class="win-head"><span>%s</span><span class="%s">%s</span></div>
      <div class="win-scale">
        <div class="bar"><div class="%s" style="width:%s"></div>%s</div>
      </div>
      <div class="meta"><span class="reset">%s</span>%s</div>
      %s
    </div>""" % ("" if pct is None else "%.1f" % pct, html.escape(label),
                 html.escape(reset_left(reset)),
                 html.escape(reset_iso), html.escape(quota_label), html.escape(title_txt),
                 html.escape(label), pct_cls, pct_txt,
                 fill_cls, fill_width, marker_html,
                 html.escape(reset_txt), note_html, capacity_html)



# (css class, label). A healthy account renders as a bare dim dot with no text:
# on a page where everything is fine, the status column should be silent rather
# than five green badges competing with the numbers you actually came to read.
BADGE = {
    "ok": ("ok", "正常"),
    "token_expired": ("bad", "Token 异常"),
    "error": ("warn", "查询异常"),
    "unknown": ("idle", "未知"),
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@TITLE@</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Two themes share this one stylesheet: a polished dark (A) and a warm light
     (C). The page defaults to the OS preference and lets the visitor pin one.
     Variables live on :root for the dark default, are flipped by a
     prefers-color-scheme media query, and body.theme-dark / body.theme-light
     override either way for an explicit choice (a class on body beats the
     :root media query for body's subtree via inheritance). */
  :root {
    --bg: #0a0b0d; --bg-2: #131518;
    --card: #131518; --card-border: #1e2024;
    --card-shadow: inset 0 1px 0 rgba(255,255,255,.03), 0 8px 24px rgba(0,0,0,.35);
    --card-alert-border: #3a2a18; --card-alert-bg: var(--card);
    --ink: #e6e7ea; --ink-2: #9ca3af; --ink-3: #6b7280;
    --bar-track: #303640; --bar-track-border: rgba(255,255,255,.075); --bar-marker: #d7dbe1;
    --capacity-track: #191c21; --capacity-on: #9ca3af;
    --accent: #3ecf8e; --accent-ink: #04130c;
    --warn: #f5a524; --bad: #f87171; --good: #3ecf8e;
    --summary-bg: #131518; --summary-border: #1e2024;
    --ui-font: "Space Grotesk"; --num-weight: 300;
    --mono: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    --radius: 16px;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f7f6f3; --bg-2: #ffffff;
      --card: #ffffff; --card-border: #eceae4;
      --card-shadow: 0 1px 2px rgba(43,42,39,.04);
      --card-alert-border: #e9c9a8; --card-alert-bg: #fdf8f0;
      --ink: #2b2a27; --ink-2: #6b6960; --ink-3: #9b988f;
      --bar-track: #ddd9d0; --bar-track-border: rgba(43,42,39,.09); --bar-marker: #5d5b55;
      --capacity-track: #f4f2ed; --capacity-on: #8d8a82;
      --accent: #4f9d7a; --accent-ink: #ffffff;
      --warn: #d18a3e; --bad: #c0492f; --good: #4f9d7a;
      --summary-bg: #ffffff; --summary-border: #eceae4;
      --ui-font: "Manrope"; --num-weight: 600;
    }
  }
  body.theme-dark {
    --bg: #0a0b0d; --bg-2: #131518; --card: #131518; --card-border: #1e2024;
    --card-shadow: inset 0 1px 0 rgba(255,255,255,.03), 0 8px 24px rgba(0,0,0,.35);
    --card-alert-border: #3a2a18; --card-alert-bg: var(--card);
    --ink: #e6e7ea; --ink-2: #9ca3af; --ink-3: #6b7280;
    --bar-track: #303640; --bar-track-border: rgba(255,255,255,.075); --bar-marker: #d7dbe1;
    --capacity-track: #191c21; --capacity-on: #9ca3af;
    --accent: #3ecf8e; --accent-ink: #04130c; --warn: #f5a524; --bad: #f87171; --good: #3ecf8e;
    --summary-bg: #131518; --summary-border: #1e2024; --ui-font: "Space Grotesk"; --num-weight: 300;
  }
  body.theme-light {
    --bg: #f7f6f3; --bg-2: #ffffff; --card: #ffffff; --card-border: #eceae4;
    --card-shadow: 0 1px 2px rgba(43,42,39,.04);
    --card-alert-border: #e9c9a8; --card-alert-bg: #fdf8f0;
    --ink: #2b2a27; --ink-2: #6b6960; --ink-3: #9b988f;
    --bar-track: #ddd9d0; --bar-track-border: rgba(43,42,39,.09); --bar-marker: #5d5b55;
    --capacity-track: #f4f2ed; --capacity-on: #8d8a82;
    --accent: #4f9d7a; --accent-ink: #ffffff; --warn: #d18a3e; --bad: #c0492f; --good: #4f9d7a;
    --summary-bg: #ffffff; --summary-border: #eceae4; --ui-font: "Manrope"; --num-weight: 600;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--ink); font-family: var(--ui-font), -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; padding: 28px 32px; max-width: 1440px; margin: 0 auto; -webkit-font-smoothing: antialiased; font-variant-numeric: tabular-nums; transition: background .25s, color .25s; }
  header.top { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 6px; }
  h1 { font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
  .updated { color: var(--ink-3); font-size: 11px; font-family: var(--mono); }
  .summary { font-size: 13px; padding: 11px 15px; margin: 14px 0 22px; background: var(--summary-bg); border: 1px solid var(--summary-border); border-radius: 12px; color: var(--ink-2); line-height: 1.5; }
  .summary b { color: var(--ink); font-weight: 500; }
  .summary.warn, .summary.bad { border-color: var(--warn); }
  .summary .hi { color: var(--warn); font-weight: 500; }
  .summary.bad .hi, .summary.bad { border-color: var(--bad); }
  .controls { display: flex; align-items: center; gap: 10px; margin-bottom: 26px; flex-wrap: wrap; }
  .btn, .mini-btn { display: inline-flex; align-items: center; gap: 6px; background: var(--card); color: var(--ink-2); border: 1px solid var(--card-border); border-radius: 10px; padding: 7px 14px; text-decoration: none; font-size: 12.5px; cursor: pointer; font-family: inherit; font-weight: 500; transition: color .15s, border-color .15s, background .15s, transform .1s; }
  .btn:hover, .mini-btn:hover { color: var(--ink); border-color: var(--ink-3); }
  .btn:active, .mini-btn:active { transform: translateY(1px); }
  .btn:focus-visible, .mini-btn:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .btn.primary { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
  .btn.primary:hover { filter: brightness(1.05); color: var(--accent-ink); }
  select, textarea { background: var(--bg); color: var(--ink); border: 1px solid var(--card-border); border-radius: 8px; padding: 5px 9px; font-size: 12px; font-family: inherit; }
  .gear { width: 13px; height: 13px; opacity: .85; }

  /* Default view: a horizontal usage chart. Account labels occupy one fixed
     column and every quota lane starts at the same x coordinate. Track length
     always means 0–100% usage; plan capacity has a separate tier marker. */
  #chart-guide { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 24px; align-items: end; padding: 0 20px 8px; }
  .guide-title { color: var(--ink-3); font-size: 10px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; }
  .axis-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; }
  .usage-axis { height: 28px; border-bottom: 1px solid var(--card-border); display: flex; align-items: flex-end; justify-content: space-between; padding-bottom: 7px; color: var(--ink-3); font-family: var(--mono); font-size: 9.5px; }

  #cards { display: flex; flex-direction: column; gap: 0; align-items: stretch; }
  .card { display: grid; grid-template-columns: 220px minmax(0, 1fr); column-gap: 24px; row-gap: 6px; padding: 18px 20px; border: 0; border-bottom: 1px solid var(--card-border); border-radius: 0; background: transparent; box-shadow: none; position: relative; }
  .card.alert { border-color: var(--card-alert-border); background: transparent; }
  .card.hidden { display: none; }
  .card h2 { grid-column: 1; grid-row: 1; font-size: 14.5px; margin: 0; display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; font-weight: 600; letter-spacing: -.01em; }
  .title { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .title > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .plan { color: var(--ink-3); font-size: 11px; font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .card-actions { display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 400; white-space: nowrap; flex-shrink: 0; }
  .drag-handle { color: var(--ink-3); cursor: grab; font-size: 10px; font-weight: 500; padding: 7px 3px; touch-action: none; user-select: none; }
  .drag-handle:active { cursor: grabbing; }
  .drag-handle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .card.dragging { opacity: .45; pointer-events: none; }
  body.is-dragging, body.is-dragging * { cursor: grabbing !important; }
  .card.drop-before { box-shadow: inset 0 2px 0 var(--accent); }
  .card.drop-after { box-shadow: inset 0 -2px 0 var(--accent); }
  .badge { display: inline-flex; align-items: center; gap: 5px; font-size: 10.5px; color: var(--ink-2); font-weight: 500; }
  .badge .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); }
  .badge.b-warn { color: var(--warn); } .badge.b-warn .dot { background: var(--warn); }
  .badge.b-bad { color: var(--bad); } .badge.b-bad .dot { background: var(--bad); }
  .badge.b-idle { color: var(--ink-3); } .badge.b-idle .dot { background: var(--ink-3); }
  .wins { grid-column: 2; grid-row: 1 / span 4; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; min-width: 0; margin-top: 0; }
  .win { min-width: 0; }
  .win-scale { max-width: 100%; }
  .win-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 8px; }
  .win-head > span:first-child { font-size: 11px; color: var(--ink-3); letter-spacing: .04em; text-transform: uppercase; font-weight: 500; }
  .pct { font-family: var(--ui-font); font-weight: var(--num-weight); color: var(--ink); font-size: 30px; letter-spacing: -.03em; line-height: 1; font-variant-numeric: tabular-nums; }
  .pct.warn { color: var(--warn); }
  .pct.crit { color: var(--bad); }
  .bar { position: relative; width: 100%; height: 8px; border: 1px solid var(--bar-track-border); border-radius: 99px; background: var(--bar-track); box-shadow: inset 0 1px 2px rgba(0,0,0,.16); overflow: visible; }
  .fill { position: absolute; top: -1px; bottom: -1px; left: -1px; height: auto; background: var(--accent); border-radius: 99px; }
  .fill.high { background: var(--warn); }
  .fill.crit { background: var(--bad); }
  .time-marker { position: absolute; z-index: 2; top: -4px; bottom: -4px; width: 2px; background: var(--bar-marker); box-shadow: 0 0 0 1px var(--bg); opacity: .8; border-radius: 2px; }
  .meta { margin-top: 9px; display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; }
  .reset { color: var(--ink-3); font-size: 10.5px; font-family: var(--mono); }
  .note { color: var(--ink-3); font-size: 10.5px; }
  .note.pace-fast { color: var(--warn); }
  .note.pace-slow { color: #6a8caf; }
  .capacity { display: flex; align-items: center; gap: 7px; color: var(--ink-3); font-size: 9.5px; font-family: var(--mono); margin-top: 7px; letter-spacing: .01em; white-space: nowrap; }
  .capacity strong { min-width: 0; color: var(--ink-2); font-weight: 500; overflow: hidden; text-overflow: ellipsis; }
  .capacity-steps { display: grid; grid-template-columns: repeat(3, 13px); gap: 3px; flex: 0 0 auto; }
  .capacity-step { display: block; height: 5px; border: 1px solid var(--card-border); border-radius: 2px; background: var(--capacity-track); }
  .capacity-step.on { border-color: var(--capacity-on); background: var(--capacity-on); }
  .err { color: var(--bad); font-size: 12.5px; }
  .expiry { grid-column: 1; grid-row: 2; color: var(--ink-2); font-size: 10.5px; margin-top: 6px; font-family: var(--mono); }
  .fetched { grid-column: 1; grid-row: 3; color: var(--ink-3); font-size: 10px; margin-top: 2px; font-family: var(--mono); }

  /* density: compact chart — same usage axis, tighter account rows */
  body.d-compact #chart-guide { grid-template-columns: 190px minmax(0, 1fr); gap: 18px; padding-left: 16px; padding-right: 16px; }
  body.d-compact .card { grid-template-columns: 190px minmax(0, 1fr); column-gap: 18px; padding: 13px 16px; }
  body.d-compact .axis-grid, body.d-compact .wins { gap: 18px; }
  body.d-compact .wins { margin-top: 0; }
  body.d-compact .pct { font-size: 21px; }
  body.d-compact .win-head { margin-bottom: 5px; }
  body.d-compact .meta { margin-top: 6px; }

  /* density: mini — one row per account, bars only */
  body.d-mini #chart-guide { display: none; }
  body.d-mini #cards { display: grid; grid-template-columns: 1fr; gap: 8px; }
  body.d-mini .card { display: flex; align-items: center; gap: 14px; padding: 11px 16px; background: var(--card); border: 1px solid var(--card-border); border-radius: var(--radius); box-shadow: var(--card-shadow); }
  body.d-mini .card.alert { border-color: var(--card-alert-border); background: var(--card-alert-bg); }
  body.d-mini .card h2 { display: contents; }
  body.d-mini .title { flex: 0 0 clamp(108px, 17vw, 190px); font-size: 13.5px; }
  body.d-mini .card-actions { order: 9; }
  body.d-mini .wins { flex: 1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px 18px; min-width: 0; margin-top: 0; }
  body.d-mini .win-head { margin-bottom: 3px; }
  body.d-mini .win-head > span:first-child { font-size: 10px; }
  body.d-mini .pct { font-size: 14px; font-weight: 600; }
  body.d-mini .bar { height: 6px; }
  body.d-mini .meta, body.d-mini .expiry, body.d-mini .fetched, body.d-mini .plan, body.d-mini .capacity { display: none; }

  @media (max-width: 700px) {
    body { padding: 20px 16px; }
    .btn, .mini-btn { min-height: 44px; }
    .drag-handle { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; }
    header.top { align-items: flex-start; }
    .updated { max-width: 52%; text-align: right; }
    #chart-guide { grid-template-columns: 1fr; padding: 0 14px 8px; }
    .guide-title { display: none; }
    .axis-grid { grid-template-columns: 1fr; }
    .axis-grid .usage-axis:nth-child(n+2) { display: none; }
    .card { grid-template-columns: 1fr; column-gap: 0; padding: 16px 14px; }
    .card h2, .card .wins, .card > .expiry, .card > .fetched { grid-column: 1; grid-row: auto; }
    .card .wins { grid-template-columns: 1fr; gap: 20px; margin-top: 18px; }
    body.d-compact #chart-guide { grid-template-columns: 1fr; padding-left: 12px; padding-right: 12px; }
    body.d-compact .card { grid-template-columns: 1fr; column-gap: 0; padding: 13px 12px; }
  }

  /* per-item visibility toggles from the settings panel */
  body.hide-badge .badge { display: none; }
  body.hide-sub .plan { display: none; }
  body.hide-reset .reset { display: none; }
  body.hide-pace .note, body.hide-pace .time-marker { display: none; }
  body.hide-capacity .capacity { display: none; }
  body.hide-fetched .fetched { display: none; }
  body.hide-expiry .expiry { display: none; }
  body.hide-summary #summary { display: none; }

  /* privacy/share mode: one-click strip of anything that identifies you
     (emails in subtitles, fetch timestamps, the page timestamp) for clean
     screenshots. Stays on only for this tab; reload drops it. */
  body.share-mode .plan,
  body.share-mode .fetched,
  body.share-mode .updated { display: none !important; }
  body.share-mode #share-toggle { color: var(--accent); border-color: var(--accent); }
  #share-toggle { position: relative; }

  /* settings panel */
  .modal { position: fixed; inset: 0; background: rgba(6,8,10,.6); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 10; }
  .modal[hidden] { display: none; }
  .modal-box { background: var(--bg-2); border: 1px solid var(--card-border); border-radius: 14px; padding: 20px; width: 420px; max-width: 100%; max-height: 85vh; overflow: auto; font-size: 12.5px; box-shadow: 0 12px 40px rgba(0,0,0,.4); }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .modal-head b { font-size: 14px; font-weight: 600; }
  .set-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 9px; color: var(--ink-2); }
  .set-row select { min-width: 150px; }
  .set-sec { color: var(--ink-3); font-size: 11px; margin: 18px 0 8px; border-top: 1px solid var(--card-border); padding-top: 14px; letter-spacing: .04em; text-transform: uppercase; font-weight: 500; }
  .set-sec:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
  .set-acct { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 4px 0; }
  .set-acct label { display: flex; align-items: center; gap: 8px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); }
  .set-acct button { padding: 1px 9px; }
  .set-acct button[disabled] { opacity: .35; cursor: default; }
  #set-show label { display: flex; align-items: center; gap: 8px; padding: 4px 0; color: var(--ink); }
  .set-backup { display: flex; flex-wrap: wrap; gap: 8px; }
  .set-btns { display: flex; gap: 8px; margin-top: 8px; }
  .set-hint { color: var(--ink-3); font-size: 10.5px; margin-top: 12px; line-height: 1.5; }
  .mini-btn.flash { color: var(--good); border-color: var(--good); }
</style>
</head>
<body class="d-comfy">
<header class="top">
  <h1>@TITLE@</h1>
  <span class="updated" id="updated">@UPDATED@ · llm-quota-watchdog</span>
</header>
<div id="summary" class="summary">@SUMMARY@</div>
<div class="controls">
  <a class="btn primary refresh-link" href="/refresh">刷新全部</a>
  <button class="btn" id="share-toggle" title="隐藏邮箱、时间戳等可识别信息，方便截图分享">隐私模式</button>
  <button class="btn" id="open-settings"><svg class="gear" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.2v2M8 12.8v2M1.2 8h2M12.8 8h2M3.3 3.3l1.4 1.4M11.3 11.3l1.4 1.4M3.3 12.7l1.4-1.4M11.3 4.7l1.4-1.4"/></svg>显示设置</button>
</div>
<div id="chart-guide">
  <span class="guide-title">套餐 / 周期从长到短</span>
  <div class="axis-grid" aria-label="每条轨道均表示从零到百分之百的使用比例">
    <div class="usage-axis"><span>使用比例</span><span>100%</span></div>
    <div class="usage-axis" aria-hidden="true"><span>使用比例</span><span>100%</span></div>
    <div class="usage-axis" aria-hidden="true"><span>使用比例</span><span>100%</span></div>
  </div>
</div>
<div id="cards">@CARDS@</div>

<div class="modal" id="settings" hidden>
  <div class="modal-box">
    <div class="modal-head"><b>显示设置</b><button class="mini-btn" id="set-close">关闭</button></div>
    <div class="set-row"><span>主题</span><select id="set-theme">
      <option value="auto">跟随系统</option><option value="dark">深色</option><option value="light">浅色</option>
    </select></div>
    <div class="set-row"><span>密度</span><select id="set-density">
      <option value="comfy">横向图表</option><option value="compact">紧凑图表</option><option value="mini">极简单行</option>
    </select></div>
    <div class="set-row"><span>排序</span><select id="set-sort">
      <option value="expiry">快到期且未用完（默认）</option><option value="waste">按浪费速度</option>
      <option value="usage">按用量高低（告急置顶）</option>
      <option value="custom">自定义顺序</option>
    </select></div>
    <div class="set-row"><span>自动刷新</span><select id="set-refresh">
      <option value="0">关闭</option><option value="300">5分钟</option><option value="900">15分钟</option>
      <option value="1800">30分钟</option><option value="3600">1小时</option><option value="10800">3小时</option>
    </select></div>
    <div class="set-sec">显示哪些账号 · 调整顺序</div>
    <div id="set-accounts"></div>
    <div class="set-sec">显示哪些内容</div>
    <div id="set-show"></div>
    <div class="set-sec">备份与重置</div>
    <div class="set-backup">
      <button class="mini-btn" id="set-export">复制配置</button>
      <button class="mini-btn" id="set-download">下载文件</button>
      <button class="mini-btn" id="set-import">粘贴导入</button>
      <button class="mini-btn" id="set-upload">上传文件</button>
      <input type="file" id="set-file" accept="application/json,.json" hidden>
    </div>
    <div class="set-btns">
      <button class="mini-btn" id="set-reset">恢复默认</button>
    </div>
    <div class="set-hint">设置只存在这个浏览器里（localStorage），不会上传，也不影响别人看到的页面。换电脑时用「下载文件」带走，「上传文件」恢复。</div>
  </div>
</div>

<script>
(function(){
  var KEY = 'quotaSettings';
  var SHOW = [['badge','健康徽章'], ['sub','卡片副标题'], ['reset','重置时间'],
              ['capacity','套餐总量'],
              ['pace','节奏提示与时间刻度'], ['fetched','更新时间'], ['expiry','套餐到期'],
              ['summary','顶部摘要']];
  function defaults(){
    return {v: 5, theme: 'auto', density: 'comfy', sort: 'expiry', order: [], orderCustomized: false, hidden: [],
            show: {badge: true, sub: true, reset: true, capacity: true, pace: true, fetched: true, expiry: true, summary: true},
            autoRefresh: 0};
  }
  var S = defaults(), timer = null;

  function load(){
    var raw = {};
    try { raw = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) {}
    S = defaults();
    ['theme', 'density', 'autoRefresh'].forEach(function(k){
      if (raw[k] !== undefined) S[k] = raw[k];
    });
    // v5 introduces a clearer expiry-first default. Existing browsers adopt it once;
    // after the migration, the visitor's explicit choice is preserved.
    if ((parseInt(raw.v, 10) || 0) >= 5 && raw.sort !== undefined) S.sort = raw.sort;
    // v3 intentionally resets the old saved order once. The server already
    // emits provider-grouped rows, but v1/v2 localStorage silently overrode
    // that DOM order and made same-provider accounts look scattered.
    if ((parseInt(raw.v, 10) || 0) >= 3 && Array.isArray(raw.order)) {
      S.order = raw.order.slice();
      S.orderCustomized = !!raw.orderCustomized;
    }
    if (Array.isArray(raw.hidden)) S.hidden = raw.hidden.slice();
    if (raw.show) SHOW.forEach(function(p){
      if (raw.show[p[0]] !== undefined) S.show[p[0]] = !!raw.show[p[0]];
    });
    // versions before the settings panel kept the interval in its own key
    var legacy = localStorage.getItem('quotaAutoRefresh');
    if (legacy !== null && raw.autoRefresh === undefined) S.autoRefresh = parseInt(legacy, 10) || 0;
  }
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} }

  function cards(){ return [].slice.call(document.querySelectorAll('#cards .card')); }
  function acctOf(c){ return c.getAttribute('data-account'); }
  function maxPct(card){
    var m = -1;
    [].forEach.call(card.querySelectorAll('.win'), function(w){
      var p = parseFloat(w.getAttribute('data-pct'));
      if (!isNaN(p) && p > m) m = p;
    });
    return m;
  }

  function wasteScore(card){
    var best = -1, now = Date.now();
    [].forEach.call(card.querySelectorAll('.win'), function(w){
      var pct = parseFloat(w.getAttribute('data-pct'));
      var resetAt = Date.parse(w.getAttribute('data-reset-at') || '');
      if (isNaN(pct) || isNaN(resetAt)) return;
      var remaining = Math.max(0, 100 - pct);
      var hoursLeft = Math.max(1, (resetAt - now) / 3600000);
      best = Math.max(best, remaining / hoursLeft);
    });
    return best;
  }

  function expiryRank(card){
    // Cards render quota windows longest-first. Sort by that primary window;
    // a frequently-resetting 5h allowance must not outrank a monthly/weekly one.
    var w = card.querySelector('.win');
    if (!w) return {tier: 3, resetAt: Infinity, remaining: 0};
    var pct = parseFloat(w.getAttribute('data-pct'));
    if (isNaN(pct)) return {tier: 3, resetAt: Infinity, remaining: 0};
    var remaining = Math.max(0, 100 - pct);
    if (remaining <= 0.001) return {tier: 2, resetAt: Infinity, remaining: 0};
    var resetAt = Date.parse(w.getAttribute('data-reset-at') || '');
    if (isNaN(resetAt)) return {tier: 1, resetAt: Infinity, remaining: remaining};
    return {tier: 0, resetAt: resetAt, remaining: remaining};
  }

  function compareExpiryRank(a, b){
    return (a.tier - b.tier) || (a.resetAt - b.resetAt) || (b.remaining - a.remaining);
  }

  function compareExpiry(a, b){ return compareExpiryRank(expiryRank(a), expiryRank(b)); }

  function groupedOrder(compare){
    var ordered = cards().slice(), providerBest = Object.create(null);
    ordered.forEach(function(c){
      var provider = c.getAttribute('data-provider') || '';
      if (!providerBest[provider] || compare(c, providerBest[provider]) < 0) providerBest[provider] = c;
    });
    return ordered.sort(function(a, b){
      var ap = a.getAttribute('data-provider') || '', bp = b.getAttribute('data-provider') || '';
      if (ap === bp) return compare(a, b);
      return compare(providerBest[ap], providerBest[bp]);
    });
  }

  function autoOrder(score){ return groupedOrder(function(a, b){ return score(b) - score(a); }); }

  // Follow the server's provider-grouped DOM order until the visitor makes a
  // manual move. After that, new accounts land at the end; vanished ones drop.
  function syncOrder(){
    var names = cards().map(acctOf);
    if (!S.orderCustomized) {
      S.order = names.slice();
      S.hidden = S.hidden.filter(function(n){ return names.indexOf(n) >= 0; });
      return;
    }
    S.order = S.order.filter(function(n){ return names.indexOf(n) >= 0; });
    names.forEach(function(n){ if (S.order.indexOf(n) < 0) S.order.push(n); });
    S.hidden = S.hidden.filter(function(n){ return names.indexOf(n) >= 0; });
  }

  function apply(){
    syncOrder();
    var b = document.body;
    // density + theme + detail toggles are all body classes; toggle each by
    // name so we never clobber share-mode (which the toolbar button owns)
    ['d-comfy', 'd-compact', 'd-mini'].forEach(function(c){ b.classList.remove(c); });
    b.classList.add('d-' + S.density);
    ['theme-dark', 'theme-light'].forEach(function(c){ b.classList.remove(c); });
    if (S.theme === 'dark') b.classList.add('theme-dark');
    else if (S.theme === 'light') b.classList.add('theme-light');
    SHOW.forEach(function(p){ b.classList.toggle('hide-' + p[0], !S.show[p[0]]); });
    var ordered = cards().slice();
    if (S.sort === 'expiry') ordered = groupedOrder(compareExpiry);
    else if (S.sort === 'waste') ordered = autoOrder(wasteScore);
    else if (S.sort === 'usage') ordered = autoOrder(maxPct);
    else ordered.sort(function(a, b){ return S.order.indexOf(acctOf(a)) - S.order.indexOf(acctOf(b)); });
    ordered.forEach(function(c, i){
      var n = acctOf(c);
      c.classList.toggle('hidden', S.hidden.indexOf(n) >= 0);
      // visual order, so reordering never touches the DOM the refresh patches
      c.style.order = i;
    });
    renderSummary();
    arm(S.autoRefresh);
  }

  // summary is computed from the *visible* cards, so hiding an account also
  // stops it from being reported as the one you should worry about
  function renderSummary(){
    var el = document.getElementById('summary');
    if (!el) return;
    var vis = cards().filter(function(c){ return !c.classList.contains('hidden'); });
    if (!vis.length) { el.textContent = '没有显示中的账号，点右上角“显示设置”打开几个'; el.className = 'summary'; return; }
    var ok = 0, unknown = 0, bad = [], worst = null;
    vis.forEach(function(c){
      var h = c.getAttribute('data-health') || '';
      if (h === 'ok') ok++;
      else if (h === 'unknown') unknown++;
      else if (h) bad.push(acctOf(c));
      [].forEach.call(c.querySelectorAll('.win'), function(w){
        var p = parseFloat(w.getAttribute('data-pct'));
        if (isNaN(p)) return;
        if (!worst || p > worst.pct) worst = {pct: p, acct: acctOf(c),
          win: w.getAttribute('data-short') || '', reset: w.getAttribute('data-reset-short') || ''};
      });
    });
    var head;
    if (bad.length) head = bad.length + ' 个异常：' + bad.join('、');
    else if (ok) head = ok + '/' + vis.length + ' 正常';
    else head = '健康检查暂不可用';
    if (worst) head += '，额度最高 ' + worst.acct + ' ' + worst.win + ' ' + Math.round(worst.pct) + '%'
                     + (worst.reset ? '，' + worst.reset + '重置' : '');
    el.textContent = head;
    // an account sitting near its ceiling is worth noticing even with every token healthy
    el.className = 'summary' + (bad.length ? ' bad' : (worst && worst.pct >= 85 ? ' warn' : ''));
  }

  function buildPanel(){
    document.getElementById('set-theme').value = S.theme;
    document.getElementById('set-density').value = S.density;
    document.getElementById('set-sort').value = S.sort;
    document.getElementById('set-refresh').value = String(S.autoRefresh);

    var box = document.getElementById('set-accounts');
    box.innerHTML = '';
    S.order.forEach(function(n, i){
      var row = document.createElement('div');
      row.className = 'set-acct';
      var lab = document.createElement('label');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = S.hidden.indexOf(n) < 0;
      cb.onchange = function(){
        var at = S.hidden.indexOf(n);
        if (cb.checked) { if (at >= 0) S.hidden.splice(at, 1); }
        else if (at < 0) S.hidden.push(n);
        save(); apply();
      };
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(n));
      row.appendChild(lab);
      var btns = document.createElement('span');
      [['↑', -1, i === 0], ['↓', 1, i === S.order.length - 1]].forEach(function(spec){
        var b = document.createElement('button');
        b.className = 'mini-btn';
        b.textContent = spec[0];
        // manual order is meaningless while an automatic sort is active
        b.disabled = spec[2] || S.sort !== 'custom';
        b.onclick = function(){
          var j = i + spec[1];
          var t = S.order[i]; S.order[i] = S.order[j]; S.order[j] = t;
          S.orderCustomized = true;
          save(); apply(); buildPanel();
        };
        btns.appendChild(b);
      });
      row.appendChild(btns);
      box.appendChild(row);
    });

    var showBox = document.getElementById('set-show');
    showBox.innerHTML = '';
    SHOW.forEach(function(p){
      var lab = document.createElement('label');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!S.show[p[0]];
      cb.onchange = function(){ S.show[p[0]] = cb.checked; save(); apply(); };
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(p[1]));
      showBox.appendChild(lab);
    });
  }

  function patchFromHtml(htmlText, account){
    var doc = new DOMParser().parseFromString(htmlText, 'text/html');
    var upd = doc.getElementById('updated');
    var sum = doc.getElementById('summary');
    if (upd) document.getElementById('updated').innerHTML = upd.innerHTML;
    if (sum) document.getElementById('summary').innerHTML = sum.innerHTML;
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
    apply();      // the patch just replaced the nodes we had styled
    buildPanel(); // ...and may have added or removed an account
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

  var modal = document.getElementById('settings');
  document.getElementById('open-settings').onclick = function(){ buildPanel(); modal.hidden = false; };
  document.getElementById('set-close').onclick = function(){ modal.hidden = true; };
  modal.onclick = function(e){ if (e.target === modal) modal.hidden = true; };
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') modal.hidden = true; });

  function bindSelect(id, key, asInt){
    document.getElementById(id).onchange = function(){
      S[key] = asInt ? (parseInt(this.value, 10) || 0) : this.value;
      save(); apply(); buildPanel();
    };
  }
  bindSelect('set-theme', 'theme', false);
  bindSelect('set-density', 'density', false);
  bindSelect('set-sort', 'sort', false);
  bindSelect('set-refresh', 'autoRefresh', true);

  // ---- direct account reordering: pointer drag works with mouse and touch;
  // the focused handle also supports ArrowUp / ArrowDown for keyboard users ----
  var draggingCard = null, dragPointer = null;
  function clearDropMarks(){
    cards().forEach(function(c){ c.classList.remove('drop-before', 'drop-after'); });
  }
  function paintCustomOrder(){
    cards().forEach(function(c){ c.style.order = S.order.indexOf(acctOf(c)); });
  }
  function useCustomSort(){
    if (S.sort !== 'custom') {
      S.sort = 'custom';
      var sel = document.getElementById('set-sort');
      if (sel) sel.value = 'custom';
    }
  }
  function moveAccount(name, targetName, after){
    var from = S.order.indexOf(name);
    if (from < 0 || name === targetName) return;
    S.order.splice(from, 1);
    var at = S.order.indexOf(targetName);
    if (at < 0) { S.order.push(name); return; }
    S.order.splice(at + (after ? 1 : 0), 0, name);
    paintCustomOrder();
  }
  document.addEventListener('pointerdown', function(e){
    var handle = e.target.closest && e.target.closest('.drag-handle');
    if (!handle) return;
    var card = handle.closest('.card');
    if (!card) return;
    e.preventDefault();
    syncOrder(); useCustomSort();
    S.orderCustomized = true;
    draggingCard = card; dragPointer = e.pointerId;
    card.classList.add('dragging');
    document.body.classList.add('is-dragging');
  });
  document.addEventListener('pointermove', function(e){
    if (!draggingCard || e.pointerId !== dragPointer) return;
    e.preventDefault();
    if (e.clientY < 72) window.scrollBy(0, -12);
    else if (e.clientY > window.innerHeight - 72) window.scrollBy(0, 12);
    var hit = document.elementFromPoint(e.clientX, e.clientY);
    var target = hit && hit.closest && hit.closest('#cards .card:not(.hidden)');
    clearDropMarks();
    if (!target || target === draggingCard) return;
    var after = e.clientY >= target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    target.classList.add(after ? 'drop-after' : 'drop-before');
    moveAccount(acctOf(draggingCard), acctOf(target), after);
  }, {passive: false});
  function finishDrag(e){
    if (!draggingCard || (e && e.pointerId !== dragPointer)) return;
    draggingCard.classList.remove('dragging');
    document.body.classList.remove('is-dragging');
    draggingCard = null; dragPointer = null;
    clearDropMarks(); save(); apply(); buildPanel();
  }
  document.addEventListener('pointerup', finishDrag);
  document.addEventListener('pointercancel', finishDrag);
  window.addEventListener('blur', function(){ finishDrag(); });
  document.addEventListener('keydown', function(e){
    var handle = e.target.closest && e.target.closest('.drag-handle');
    if (!handle || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    e.preventDefault();
    syncOrder(); useCustomSort();
    var name = acctOf(handle.closest('.card'));
    var i = S.order.indexOf(name), j = i + (e.key === 'ArrowUp' ? -1 : 1);
    if (i < 0 || j < 0 || j >= S.order.length) return;
    S.orderCustomized = true;
    var target = S.order[j];
    moveAccount(name, target, e.key === 'ArrowDown');
    save(); apply(); buildPanel(); handle.focus();
  });

  // ---- privacy / share mode: session-only, not persisted ----
  var shareBtn = document.getElementById('share-toggle');
  shareBtn.onclick = function(){
    document.body.classList.toggle('share-mode');
    var on = document.body.classList.contains('share-mode');
    shareBtn.textContent = on ? '退出隐私模式' : '隐私模式';
  };

  // ---- backup: copy / download / paste-import / upload-file ----
  function flash(btn, text){
    var prev = btn.textContent;
    btn.textContent = text; btn.classList.add('flash');
    setTimeout(function(){ btn.textContent = prev; btn.classList.remove('flash'); }, 1400);
  }
  function importText(text, btn){
    var obj;
    try { obj = JSON.parse(text); }
    catch (e) { flash(btn, '格式不对'); return false; }
    if (!obj || typeof obj !== 'object') { flash(btn, '格式不对'); return false; }
    try { localStorage.setItem(KEY, JSON.stringify(obj)); }
    catch (e) { flash(btn, '格式不对'); return false; }
    load(); apply(); save(); buildPanel();
    flash(btn, '已导入');
    return true;
  }
  document.getElementById('set-export').onclick = function(){
    var btn = this, text = JSON.stringify(S, null, 2);
    // clipboard is gated behind https / localhost; fall back to a prompt so the
    // button is never a no-op on plain-http or file://
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function(){ flash(btn, '已复制'); }, function(){ window.prompt('复制这段配置：', text); });
    } else { window.prompt('复制这段配置：', text); }
  };
  document.getElementById('set-download').onclick = function(){
    var blob = new Blob([JSON.stringify(S, null, 2)], {type: 'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = 'quota-settings.json'; a.click();
    URL.revokeObjectURL(url);
    flash(this, '已下载');
  };
  document.getElementById('set-import').onclick = function(){
    var btn = this;
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function(t){ if (t) importText(t, btn); })
        .catch(function(){ var t = window.prompt('粘贴一段配置 JSON：'); if (t) importText(t, btn); });
    } else {
      var t = window.prompt('粘贴一段配置 JSON：');
      if (t) importText(t, btn);
    }
  };
  document.getElementById('set-upload').onclick = function(){ document.getElementById('set-file').click(); };
  document.getElementById('set-file').onchange = function(e){
    var f = e.target.files[0], btn = document.getElementById('set-upload');
    if (!f) return;
    var rd = new FileReader();
    rd.onload = function(){ importText(rd.result, btn); };
    rd.readAsText(f);
    e.target.value = '';  // let the same file be picked again later
  };
  document.getElementById('set-reset').onclick = function(){
    S = defaults();
    localStorage.removeItem('quotaAutoRefresh');
    save(); apply(); buildPanel();
    flash(this, '已恢复');
  };

  load();
  apply();
  save();
})();
</script>
</body>
</html>"""
# NOTE: "全部刷新" / per-card refresh links are pure frontend — they just fetch()
# /refresh (optionally ?account=<label>) and patch the DOM in place (falling back
# to a full navigation if fetch fails). This script does not ship a server for
# that endpoint; if you don't run one, the buttons simply 404 and the static page
# itself is unaffected. See the README for a minimal example of such a trigger.
# The settings panel is frontend-only too: it lives in localStorage, so every
# visitor gets their own layout without the generator knowing anything about it.


def refresh_page_state(cfg, page_state, accounts, labels=None):
    """Re-query accounts (all of them unless labels is given) into page_state.

    A failed fetch keeps whatever numbers we had — a card that goes blank every
    time the network hiccups is worse than a card showing slightly stale data
    next to a red badge.
    """
    stamp = now_utc().isoformat()
    for acct in accounts:
        label = acct["label"]
        if labels is not None and label not in labels:
            continue
        r = fetch_one(cfg, acct)
        if r is None:
            continue
        entry = dict(page_state["accounts"].get(label) or {})
        entry["fetched_at"] = stamp
        if "windows" in r:
            entry["windows"] = dict(
                (win, {"pct": pct, "reset": (reset.isoformat() if reset else None)})
                for win, (pct, reset) in r["windows"].items())
            entry["fetch_error"] = None
            entry["last_ok_at"] = stamp
            entry["health"] = "ok"
        else:
            entry["fetch_error"] = r["error"]
            entry["health"] = r["error_kind"]
        page_state["accounts"][label] = entry


def resolve_health(cfg, acct, entry, bad_set):
    """Which badge a card gets, or None for no badge at all.

    OAuth accounts defer to CLIProxyAPI's management API when it's available —
    it knows about refresh failures that a successful usage call wouldn't show.
    If that check was asked for but couldn't run, we say ⚪未知 rather than
    claiming all-clear. People who don't run CLIProxyAPI never configure the
    management key, so they just get the verdict of our own last fetch.
    """
    own = entry.get("health")
    if acct["type"] in ("claude", "codex"):
        if bad_set is not None and os.path.basename(acct.get("auth_file") or "") in bad_set:
            return "token_expired"
        if own == "ok" and bad_set is None and cfg.get("cliproxyapi_management_key_file"):
            return "unknown"
    return own or "unknown"


def card_html(cfg, acct, entry, health):
    label = acct["label"]
    rows = []
    windows = entry.get("windows") or {}
    # Render the longest available quota window first. On desktop these become
    # left-to-right columns; on mobile they remain top-to-bottom in the same
    # order, so the most representative usage horizon always leads.
    snap = (cfg.get("monthly_snapshot") or {}).get(label)
    if snap:
        reset_ts = parse_ts(str(snap.get("reset")) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
        pi = monthly_pace(snap.get("pct"), reset_ts)
        note = ("自动更新于 %s" if snap.get("source") == "auto" else "手动更新于 %s") % snap.get("updated", "?")
        if pi:
            note += " · 时间进度 %.0f%% · 节奏%s" % pi
        capacity_tier, quota_label = quota_capacity_info(acct, "monthly")
        rows.append(window_html(cfg, "月度", snap.get("pct"), reset_ts, note,
                                elapsed=(pi[0] if pi else None), capacity_tier=capacity_tier,
                                quota_label=quota_label))
    for win in ("7d", "5h"):
        w = windows.get(win)
        if not w:
            continue
        pct, reset = w.get("pct"), parse_ts(w.get("reset"))
        pi = pace_info(pct, reset, win)
        note = "" if pi is None else "时间进度 %.0f%% · 节奏%s" % pi
        capacity_tier, quota_label = quota_capacity_info(acct, win)
        rows.append(window_html(cfg, "7天" if win == "7d" else "5小时", pct, reset, note,
                                elapsed=(pi[0] if pi else None), capacity_tier=capacity_tier,
                                quota_label=quota_label))
    if entry.get("fetch_error"):
        # shown alongside stale bars when we have them, alone when we don't
        rows.append('<div class="err">查询失败: %s</div>'
                    % html.escape(str(entry["fetch_error"])[:80]))
    if not rows:
        rows.append('<div class="err">尚无数据，等待首次刷新</div>')

    expiry_html = ""
    exp = (cfg.get("plan_expiry") or {}).get(label)
    if exp:
        exp_ts = parse_ts(str(exp) + "T00:00:00+%02d:00" % cfg["timezone_offset_hours"])
        if exp_ts:
            days = (exp_ts.date() - now_utc().astimezone(cfg["_tz"]).date()).days
            expiry_html = '<div class="expiry">套餐 %d 天后到期（%s）</div>' % (max(days, 0), exp)

    # status dot always shows (colour carries the state); a text label only
    # appears when something's wrong, so a healthy page is quiet dots not noise
    badge_html = ""
    if health is not None:
        cls, htext = BADGE[health]
        label_txt = "" if health == "ok" else html.escape(htext)
        badge_html = '<span class="badge b-%s"><i class="dot"></i>%s</span>' % (cls, label_txt)
    # a window near its ceiling (or a bad token) tints the whole card so it
    # stands out in a grid without re-reading every number
    alert = (health in ("token_expired", "error")) or any(
        (w.get("pct") is not None and w.get("pct") >= 75)
        for w in (entry.get("windows") or {}).values())
    sub_html = ""
    if acct.get("sub"):
        sub_html = '<span class="plan">%s</span>' % html.escape(str(acct["sub"]))
    fetched = parse_ts(entry.get("fetched_at"))
    fetched_txt = ("更新于 " + fetched.astimezone(cfg["_tz"]).strftime("%H:%M")) if fetched else "尚未拉取"
    label_esc = html.escape(label)
    provider_esc = html.escape(str(acct.get("type") or "other"))
    return ('<div class="card%s" data-account="%s" data-provider="%s" data-health="%s">'
            '<h2><span class="title"><span>%s</span>%s</span>'
            '<span class="card-actions">%s<span class="drag-handle" role="button" tabindex="0" title="拖动账号调整顺序" aria-label="拖动账号调整顺序">拖动</span><a class="mini-btn refresh-link" href="%s" data-account="%s">刷新</a>'
            '</span></h2><div class="wins">%s</div>%s<div class="fetched">%s</div></div>'
            % (" alert" if alert else "", label_esc, provider_esc, html.escape(health or ""), label_esc, sub_html,
               badge_html, "/refresh?account=" + urllib.parse.quote(label), label_esc,
               "".join(rows), expiry_html, html.escape(fetched_txt)))


def group_accounts_by_provider(accounts):
    """Keep accounts from the same provider adjacent without changing the
    provider group's first-seen position or the order inside each group."""
    provider_order, grouped = [], {}
    for acct in accounts:
        provider = acct.get("type") or "other"
        if provider not in grouped:
            provider_order.append(provider)
            grouped[provider] = []
        grouped[provider].append(acct)
    return [acct for provider in provider_order for acct in grouped[provider]]


def render_page(cfg, page_state, accounts):
    """Build the HTML from cached state only — no network calls except the local
    management-API health check."""
    bad_set = auth_health_map(cfg)
    cards, unhealthy, healthy = [], [], 0
    accounts = group_accounts_by_provider(accounts)
    for acct in accounts:
        entry = page_state["accounts"].get(acct["label"]) or {}
        health = resolve_health(cfg, acct, entry, bad_set)
        if health == "ok":
            healthy += 1
        elif health != "unknown":
            unhealthy.append(acct["label"])
        cards.append(card_html(cfg, acct, entry, health))

    # server-rendered fallback; the page's script recomputes this from whichever
    # cards the visitor actually kept visible
    if unhealthy:
        summary = "%d 个异常：%s，其余正常" % (len(unhealthy), "、".join(unhealthy))
    elif healthy:
        summary = "%d/%d 正常" % (healthy, len(accounts))
    else:
        summary = "健康检查暂不可用"

    return (PAGE_TEMPLATE
            .replace("@TITLE@", html.escape(cfg.get("page_title") or DEFAULTS["page_title"]))
            .replace("@UPDATED@", now_utc().astimezone(cfg["_tz"]).strftime("%m月%d日 %H:%M"))
            .replace("@SUMMARY@", html.escape(summary))
            .replace("@CARDS@", "".join(cards)))


def cmd_page(cfg, labels=None):
    accounts = account_list(cfg)
    page_state = load_page_state(cfg)
    refresh_page_state(cfg, page_state, accounts, labels)
    save_page_state(cfg, page_state)

    out_dir = cfg["page_out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, ".index.html.tmp")
    with open(tmp, "w") as f:
        f.write(render_page(cfg, page_state, accounts))
    os.replace(tmp, os.path.join(out_dir, "index.html"))
    log(cfg, "page generated: %s (refreshed: %s)"
        % (os.path.join(out_dir, "index.html"), ",".join(labels) if labels else "all"))



# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="llm-quota-watchdog: LLM coding-plan quota dashboard + alerts")
    ap.add_argument("command", choices=["watchdog", "page", "check-auth"])
    ap.add_argument("--summary", action="store_true", help="watchdog: always push the full summary")
    ap.add_argument("--account", action="append", metavar="LABEL",
                    help="page: only re-query this account (repeatable); "
                         "everything else is served from the page cache")
    ap.add_argument("--config", default=os.environ.get("QUOTA_WATCHDOG_CONFIG", "./config.json"))
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = ap.parse_args()

    cfg = load_config(os.path.expanduser(args.config))
    if args.command == "watchdog":
        cmd_watchdog(cfg, args.summary)
    elif args.command == "page":
        cmd_page(cfg, args.account)
    else:
        cmd_check_auth(cfg)


if __name__ == "__main__":
    main()
