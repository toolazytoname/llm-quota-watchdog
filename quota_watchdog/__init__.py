"""llm-quota-watchdog — quota dashboard + smart alerts for LLM coding-plan subscriptions."""

__version__ = "1.1.0"
VERSION = __version__

# Window duration constants (seconds)
WIN_SECONDS: dict[str, int] = {"5h": 5 * 3600, "7d": 7 * 86400}

DEFAULT_THRESHOLDS = {
    "high_5h": 80,
    "high_week": 90,
    "fast_margin": 15,
    "waste_mid_elapsed": 50,
    "waste_margin": 30,
    "waste_hours_left": 26,
    "waste_pct": 60,
    "refill_drop": 30,
    "expiry_alert_days": [7, 3, 1],
}

DEFAULTS = {
    "bark_url": "",
    "ntfy_url": "",
    "cliproxyapi_auth_dir": "~/.cli-proxy-api",
    "accounts": [],
    "relaxed_accounts": [],
    "plan_expiry": {},
    "monthly_snapshot": {},
    "thresholds": {},
    "timezone_offset_hours": 8,
    "state_file": "./quota-watchdog-state.json",
    "page_out_dir": "./www",
    "log_file": "./quota-watchdog.log",
}
