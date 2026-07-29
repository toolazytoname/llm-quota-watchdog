"""Config loading and defaults."""

import datetime
import json
import os
from typing import Any

from quota_watchdog import DEFAULT_THRESHOLDS, DEFAULTS


def load_config(path: str) -> dict[str, Any]:
    """Load JSON config, merge with defaults, expand user paths."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    th = dict(DEFAULT_THRESHOLDS)
    th.update(cfg.get("thresholds") or {})
    cfg["thresholds"] = th
    cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=cfg.get("timezone_offset_hours", 8)))
    for k in ("state_file", "page_out_dir", "log_file"):
        cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def log(cfg: dict, msg: str) -> None:
    """Append a timestamped line to the log file."""
    try:
        with open(cfg["log_file"], "a") as f:
            f.write(datetime.datetime.now().isoformat() + " " + msg + "\n")
    except OSError:
        pass
