"""Vercel Function: GET/POST /dates backed by Vercel Blob (or local config)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import quota_watchdog as q  # noqa: E402

CONFIG = os.environ.get("QUOTA_WATCHDOG_CONFIG") or os.path.join(ROOT, "deploy-config.json")


def _send(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        user = q.load_user_config(CONFIG)
        _send(self, 200, {
            "ok": True,
            "source": user.get("_store") or "config",
            "accounts": q.accounts_public(user.get("accounts")),
        })

    def do_POST(self):
        if not q.dates_write_authorized(self.headers):
            return _send(self, 401, {"ok": False, "error": "需要写入密钥"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 8192:
            return _send(self, 413, {"ok": False, "error": "请求太大"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            return _send(self, 400, {"ok": False, "error": "JSON 不对"})
        cfg = q.load_config(CONFIG)
        try:
            rec = q.normalize_time_record(cfg, payload)
            result = q.apply_time_record(CONFIG, rec)
        except ValueError as e:
            return _send(self, 400, {"ok": False, "error": str(e)})
        except Exception as e:
            return _send(self, 500, {"ok": False, "error": str(e)[:160]})
        _send(self, 200, result)
