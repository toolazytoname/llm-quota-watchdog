import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import quota_watchdog as q


class QuotaScaleTests(unittest.TestCase):
    def test_glm_weekly_bucket_is_kept_near_reset(self):
        now = q.now_utc()
        response = {
            "data": {
                "limits": [
                    {"unit": 3, "number": 5, "usage": 2000, "currentValue": 486,
                     "nextResetTime": int((now + datetime.timedelta(hours=4)).timestamp() * 1000)},
                    {"unit": 6, "number": 1, "usage": 10000, "currentValue": 9960,
                     "nextResetTime": int((now + datetime.timedelta(minutes=12)).timestamp() * 1000)},
                ]
            }
        }
        with mock.patch.object(q, "http_get", return_value=response):
            windows = q.glm_quota("not-a-real-key")
        self.assertEqual(set(windows), {"5h", "7d"})
        self.assertAlmostEqual(windows["5h"][0], 24.3)
        self.assertAlmostEqual(windows["7d"][0], 99.6)

    def test_default_layout_is_a_full_width_usage_chart(self):
        self.assertIn('id="chart-guide"', q.PAGE_TEMPLATE)
        self.assertIn('class="usage-axis"', q.PAGE_TEMPLATE)
        self.assertNotIn('class="capacity-axis"', q.PAGE_TEMPLATE)
        self.assertIn('<body class="d-comfy">', q.PAGE_TEMPLATE)
        self.assertNotIn('id="set-cols"', q.PAGE_TEMPLATE)

    def test_capacity_uses_three_tiers_without_changing_track_width(self):
        self.assertEqual(q.quota_capacity_info({}), (0, ""))
        self.assertEqual(q.quota_capacity_info({"quota_factor": 1}), (1, "1×"))
        self.assertEqual(q.quota_capacity_info({"quota_factor": 6})[0], 2)
        self.assertEqual(q.quota_capacity_info({"quota_factor": 20})[0], 3)
        self.assertEqual(q.quota_capacity_info({"quota_factor": 200})[0], 3)

    def test_window_specific_labels_override_generic_label(self):
        account = {
            "quota_factor": 6,
            "quota_label": "generic",
            "quota_labels": {"5h": "12,000 credits / 5小时", "7d": "60,000 credits / 周"},
        }
        tier, label = q.quota_capacity_info(account, "5h")
        self.assertEqual(tier, 2)
        self.assertEqual(label, "12,000 credits / 5小时")

    def test_cross_provider_index_drives_tier_without_repeating_native_label(self):
        tier, label = q.quota_capacity_info({
            "quota_factor": 20,
            "capacity_index": 6,
            "quota_label": "100 units / 周",
        }, "7d")
        self.assertEqual(tier, 2)
        self.assertEqual(label, "100 units / 周")
        self.assertEqual(q.quota_capacity_info({"capacity_index": 6}), (2, "跨平台≈6×"))

    def test_capacity_is_rendered_beside_a_full_width_track(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=8))
        entry = {
            "windows": {"7d": {"pct": 42.5, "reset": "2026-08-13T00:00:00+00:00"}},
            "health": "ok",
        }
        html = q.card_html(
            cfg,
            {
                "type": "codex",
                "label": "Codex Pro",
                "quota_factor": 20,
                "quota_label": "Pro 20×",
            },
            entry,
            "ok",
        )
        self.assertIn('<div class="win-scale">', html)
        self.assertNotIn('class="win-scale" style=', html)
        self.assertIn("额度规模</span><strong>Pro 20×", html)
        self.assertEqual(html.count('class="capacity-step on"'), 3)

    def test_nonzero_usage_keeps_a_visible_minimum_fill(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=8))
        rendered = q.window_html(cfg, "7天", 0.2, None)
        self.assertIn('style="width:max(3px, 0.2%)"', rendered)
        empty = q.window_html(cfg, "7天", 0, None)
        self.assertIn('style="width:0"', empty)

    def test_window_exposes_machine_readable_reset_time(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=8))
        reset = datetime.datetime(2026, 8, 9, 12, 30, tzinfo=datetime.timezone.utc)
        rendered = q.window_html(cfg, "7天", 25, reset)
        self.assertIn('data-reset-at="2026-08-09T12:30:00+00:00"', rendered)
        unknown = q.window_html(cfg, "7天", 25, None)
        self.assertIn('data-reset-at=""', unknown)

    def test_longest_quota_window_is_rendered_first(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=8))
        cfg["monthly_snapshot"] = {
            "Kimi Coding": {
                "pct": 31,
                "reset": "2026-08-31",
                "updated": "2026-08-07",
            }
        }
        entry = {
            "windows": {
                "5h": {"pct": 42, "reset": "2026-08-07T13:00:00+08:00"},
                "7d": {"pct": 36, "reset": "2026-08-13T09:00:00+08:00"},
            },
            "health": "ok",
        }
        rendered = q.card_html(cfg, {"type": "kimi", "label": "Kimi Coding"}, entry, "ok")
        self.assertLess(rendered.index('data-short="月度"'), rendered.index('data-short="7天"'))
        self.assertLess(rendered.index('data-short="7天"'), rendered.index('data-short="5小时"'))

    def test_accounts_are_grouped_by_provider_stably(self):
        accounts = [
            {"type": "codex", "label": "Codex Plus"},
            {"type": "glm", "label": "GLM Lite"},
            {"type": "codex", "label": "Codex Pro"},
            {"type": "kimi", "label": "Kimi"},
            {"type": "glm", "label": "GLM Pro"},
        ]
        grouped = q.group_accounts_by_provider(accounts)
        self.assertEqual(
            [account["label"] for account in grouped],
            ["Codex Plus", "Codex Pro", "GLM Lite", "GLM Pro", "Kimi"],
        )

    def test_account_rows_expose_drag_handles(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = datetime.timezone(datetime.timedelta(hours=8))
        rendered = q.card_html(
            cfg,
            {"type": "codex", "label": "Codex Pro"},
            {"windows": {}, "health": "ok"},
            "ok",
        )
        self.assertIn('data-provider="codex"', rendered)
        self.assertIn('class="drag-handle"', rendered)
        self.assertIn('aria-label="拖动账号调整顺序"', rendered)

    def test_saved_order_migrates_to_provider_grouping_once(self):
        self.assertIn("return {v: 6", q.PAGE_TEMPLATE)
        self.assertIn("orderCustomized: false", q.PAGE_TEMPLATE)
        self.assertIn("if (!S.orderCustomized)", q.PAGE_TEMPLATE)
        self.assertIn("S.order = names.slice()", q.PAGE_TEMPLATE)
        self.assertIn("S.orderCustomized = true", q.PAGE_TEMPLATE)

    def test_expiry_first_is_the_default_sort(self):
        self.assertIn("sort: 'expiry'", q.PAGE_TEMPLATE)
        self.assertIn('<option value="expiry">快到期且未用完（默认）</option>', q.PAGE_TEMPLATE)
        self.assertIn('<option value="waste">按浪费速度</option>', q.PAGE_TEMPLATE)
        self.assertIn("function expiryRank(card)", q.PAGE_TEMPLATE)
        self.assertIn("var w = card.querySelector('.win')", q.PAGE_TEMPLATE)
        self.assertIn("a frequently-resetting 5h allowance must not outrank", q.PAGE_TEMPLATE)
        self.assertIn("function compareExpiryRank(a, b)", q.PAGE_TEMPLATE)
        self.assertIn("if (remaining <= 0.001) return {tier: 2", q.PAGE_TEMPLATE)
        self.assertIn("S.sort === 'expiry'", q.PAGE_TEMPLATE)
        self.assertIn("function wasteScore(card)", q.PAGE_TEMPLATE)
        self.assertIn("function groupedOrder(compare)", q.PAGE_TEMPLATE)
        self.assertIn("if (ap === bp) return compare(a, b)", q.PAGE_TEMPLATE)
        self.assertIn("remaining / hoursLeft", q.PAGE_TEMPLATE)
        self.assertIn("b.disabled = spec[2] || S.sort !== 'custom'", q.PAGE_TEMPLATE)

    def test_pointer_drag_does_not_capture_a_disabled_target(self):
        self.assertIn(".card.dragging { opacity: .45; pointer-events: none; }", q.PAGE_TEMPLATE)
        self.assertNotIn("setPointerCapture", q.PAGE_TEMPLATE)
        self.assertIn("window.addEventListener('blur', function(){ finishDrag(); })", q.PAGE_TEMPLATE)


class TimeModeTests(unittest.TestCase):
    def setUp(self):
        self.tz = datetime.timezone(datetime.timedelta(hours=8))
        self.cfg = dict(q.DEFAULTS)
        self.cfg["_tz"] = self.tz
        self.cfg["mode"] = "time"
        self.cfg["thresholds"] = dict(q.THRESH)
        self.cfg["accounts"] = [
            {
                "type": "grok",
                "label": "Grok SuperGrok",
                "sub": "Grok Bot + Cursor",
                "started_at": "2026-08-15",
                "expires_at": "2026-09-15",
                "period": "monthly",
            }
        ]

    def test_monthly_cycle_keeps_the_start_day(self):
        started = datetime.datetime(2026, 1, 31, tzinfo=self.tz)
        now = datetime.datetime(2026, 3, 2, 12, 0, tzinfo=self.tz)
        start, end = q.cycle_bounds(started, now, "monthly")
        self.assertEqual(start.date(), datetime.date(2026, 2, 28))
        self.assertEqual(end.date(), datetime.date(2026, 3, 31))

    def test_monthly_cycle_rolls_after_the_boundary(self):
        started = datetime.datetime(2026, 8, 15, tzinfo=self.tz)
        now = datetime.datetime(2026, 9, 15, 0, 0, tzinfo=self.tz)
        start, end = q.cycle_bounds(started, now, "monthly")
        self.assertEqual(start.date(), datetime.date(2026, 9, 15))
        self.assertEqual(end.date(), datetime.date(2026, 10, 15))

    def test_parse_local_date_accepts_clock_time_without_seconds(self):
        dt = q.parse_local_date(self.cfg, "2026-08-19T20:12")
        self.assertEqual(dt.hour, 20)
        self.assertEqual(dt.minute, 12)
        self.assertEqual(dt.tzinfo, self.tz)
        spaced = q.parse_local_date(self.cfg, "2026-08-19 20:12")
        self.assertEqual(spaced, dt)

    def test_time_window_reports_elapsed_and_remaining_days(self):
        now = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            tw = q.account_time_window(self.cfg, self.cfg["accounts"][0])
        self.assertFalse(tw["expired"])
        # start 8/15 00:00+8 → end 9/15 00:00+8; now 8/20 20:00+8
        self.assertAlmostEqual(tw["elapsed_days"], 5 + 20 / 24, places=3)
        self.assertAlmostEqual(tw["remaining_days"], 25 + 4 / 24, places=3)
        self.assertAlmostEqual(tw["elapsed_pct"], (5 + 20 / 24) / 31 * 100, places=2)
        self.assertAlmostEqual(tw["elapsed_days"] + tw["remaining_days"], 31, places=3)

    def test_expired_window_is_full_and_overdue(self):
        acct = {
            "type": "cursor",
            "label": "Cursor Pro",
            "started_at": "2026-06-01",
            "expires_at": "2026-07-01",
        }
        now = datetime.datetime(2026, 7, 10, 0, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            tw = q.account_time_window(self.cfg, acct)
        self.assertTrue(tw["expired"])
        self.assertEqual(tw["elapsed_pct"], 100)
        self.assertGreater(tw["overdue_days"], 8)
        self.assertEqual(tw["remaining_days"], 0)

    def test_card_is_a_time_track_not_a_quota_bar(self):
        now = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            html = q.card_html(self.cfg, self.cfg["accounts"][0], {"health": "ok"}, "ok")
        self.assertIn('data-track="time"', html)
        self.assertIn('data-short="本月周期"', html)
        self.assertIn("已过 5.8 天 · 还剩 25.2 天", html)
        self.assertIn('style="width:18.82%"', html)
        self.assertIn('class="time-marker"', html)
        self.assertNotIn('class="time-ticks"', html)
        self.assertIn("到期", html)
        self.assertIn("本地计时", html)
        self.assertNotIn("5小时", html)
        self.assertIn('data-started="2026-08-15"', html)
        self.assertIn('data-expires="2026-09-15"', html)
        self.assertIn('class="mini-btn time-edit-btn"', html)
        self.assertIn('>改日期</button>', html)
        self.assertIn('class="used-up-box"', html)
        self.assertIn("用完了", html)
        self.assertIn('id="time-edit"', q.PAGE_TEMPLATE)
        self.assertIn('id="add-time"', q.PAGE_TEMPLATE)
        self.assertIn('id="set-times"', q.PAGE_TEMPLATE)
        self.assertIn("function applyTimes()", q.PAGE_TEMPLATE)
        self.assertIn("function sweepUsedUp()", q.PAGE_TEMPLATE)
        self.assertIn("fetch('/dates'", q.PAGE_TEMPLATE)
        self.assertIn("保存到服务器", q.PAGE_TEMPLATE)

    def test_used_up_releases_after_bound_reset(self):
        acc = {
            "type": "grok", "label": "Grok",
            "expires_at": "2026-08-19T20:12:00",
            "used_up": True, "used_up_until": "2026-08-19T20:12:00",
        }
        now = datetime.datetime(2026, 8, 19, 13, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            changed = q.release_stale_used_up(self.cfg, [acc])
        self.assertTrue(changed)
        self.assertNotIn("used_up", acc)
        self.assertNotIn("used_up_until", acc)

    def test_used_up_stays_before_bound_reset(self):
        acc = {
            "type": "grok", "label": "Grok",
            "expires_at": "2026-08-19T20:12:00",
            "used_up": True, "used_up_until": "2026-08-19T20:12:00",
        }
        now = datetime.datetime(2026, 8, 19, 10, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            changed = q.release_stale_used_up(self.cfg, [acc])
        self.assertFalse(changed)
        self.assertTrue(acc["used_up"])

    def test_used_up_releases_even_if_monthly_cycle_rolled(self):
        acc = {
            "type": "time", "label": "Kimi K3",
            "started_at": "2026-07-22", "expires_at": "2026-08-22",
            "period": "monthly", "used_up": True,
            "used_up_until": "2026-08-22T00:00:00+08:00",
        }
        now = datetime.datetime(2026, 8, 23, 4, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            changed = q.release_stale_used_up(self.cfg, [acc])
        self.assertTrue(changed)
        self.assertNotIn("used_up", acc)

    def test_used_up_toggle_does_not_wipe_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({"accounts": [{
                    "type": "time", "label": "Kimi K3",
                    "started_at": "2026-07-22", "expires_at": "2026-08-22",
                    "period": "monthly",
                }]}, f)
            cfg = q.load_config(path)
            rec = q.normalize_time_record(cfg, {"label": "Kimi K3", "used_up": True})
            out = q.apply_time_record(path, rec)
            self.assertTrue(out["account"]["used_up"])
            with open(path) as f:
                acc = json.load(f)["accounts"][0]
            self.assertEqual(acc["started_at"], "2026-07-22")
            self.assertEqual(acc["expires_at"], "2026-08-22")
            self.assertTrue(acc["used_up"])
            self.assertTrue(acc.get("used_up_until"))
            rec = q.normalize_time_record(cfg, {"label": "Kimi K3", "used_up": False})
            q.apply_time_record(path, rec)
            with open(path) as f:
                acc = json.load(f)["accounts"][0]
            self.assertNotIn("used_up", acc)
            self.assertNotIn("used_up_until", acc)

    def test_used_up_without_until_releases_after_expiry(self):
        acc = {
            "type": "grok", "label": "Grok",
            "expires_at": "2026-08-19T20:12:00",
            "used_up": True,
        }
        now = datetime.datetime(2026, 8, 19, 13, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            changed = q.release_stale_used_up(self.cfg, [acc])
        self.assertTrue(changed)
        self.assertNotIn("used_up", acc)

    def test_used_up_without_until_stamps_deadline_while_active(self):
        acc = {
            "type": "grok", "label": "Grok",
            "expires_at": "2026-08-19T20:12:00",
            "used_up": True,
        }
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            changed = q.release_stale_used_up(self.cfg, [acc])
        self.assertTrue(changed)
        self.assertTrue(acc["used_up"])
        self.assertEqual(acc["used_up_until"], "2026-08-19T20:12:00")

    def test_used_up_check_after_expiry_does_not_stick(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({"timezone_offset_hours": 8, "accounts": [{
                    "type": "grok", "label": "Grok",
                    "expires_at": "2026-08-10T20:12:00",
                }]}, f)
            now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
            with mock.patch.object(q, "now_utc", return_value=now):
                cfg = q.load_config(path)
                rec = q.normalize_time_record(cfg, {"label": "Grok", "used_up": True})
                out = q.apply_time_record(path, rec)
            self.assertFalse(out["account"].get("used_up"))

    def test_load_user_config_persists_released_used_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({"timezone_offset_hours": 8, "accounts": [{
                    "type": "grok", "label": "Grok",
                    "expires_at": "2026-08-10T20:12:00",
                    "used_up": True,
                    "used_up_until": "2026-08-10T20:12:00",
                }]}, f)
            now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
            with mock.patch.object(q, "now_utc", return_value=now), \
                 mock.patch.object(q, "blob_token", return_value=""):
                user = q.load_user_config(path)
            self.assertFalse(user["accounts"][0].get("used_up"))
            with open(path) as f:
                saved = json.load(f)["accounts"][0]
            self.assertFalse(saved.get("used_up"))
            self.assertNotIn("used_up_until", saved)

    def test_monthly_period_without_start_rewinds_one_month(self):
        acct = {"type": "time", "label": "Kimi K3",
                "expires_at": "2026-08-22", "period": "monthly"}
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            tw = q.account_time_window(self.cfg, acct)
        self.assertEqual(tw["start"].date(), datetime.date(2026, 7, 22))
        self.assertEqual(tw["end"].date(), datetime.date(2026, 8, 22))
        self.assertAlmostEqual(tw["elapsed_days"] + tw["remaining_days"], 31, places=2)

    def test_used_up_sorts_behind_active_cards(self):
        used = {"type": "grok", "label": "Grok", "expires_at": "2026-08-19T20:12:00",
                "used_up": True}
        later = {"type": "cursor", "label": "Cursor Ultra", "started_at": "2026-08-13",
                 "expires_at": "2026-09-13", "period": "monthly"}
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            ordered = sorted([used, later], key=lambda a: q._time_sort_key(self.cfg, a))
        self.assertEqual([a["label"] for a in ordered], ["Cursor Ultra", "Grok"])

    def test_blob_store_id_parses_rw_token(self):
        with mock.patch.dict(os.environ, {
            "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_LIDhdLSkb1wiD9ne_secret",
            "BLOB_STORE_ID": "",
        }, clear=False):
            self.assertEqual(q.blob_store_id(), "LIDhdLSkb1wiD9ne")
            self.assertIn("lidhdlskb1wid9ne.private.blob.vercel-storage.com",
                          q.blob_private_url())

    def test_blob_store_id_prefers_env(self):
        with mock.patch.dict(os.environ, {
            "BLOB_STORE_ID": "store_Abc123",
            "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_other_secret",
        }, clear=False):
            self.assertEqual(q.blob_store_id(), "Abc123")

    def test_accounts_public_strips_credentials(self):
        rows = q.accounts_public([{
            "type": "glm", "label": "GLM Pro",
            "api_key": "secret", "api_key_file": ".glm-key",
            "expires_at": "2026-09-01", "used_up": True,
        }])
        self.assertEqual(rows[0]["expires_at"], "2026-09-01")
        self.assertTrue(rows[0]["used_up"])
        self.assertNotIn("api_key", rows[0])
        self.assertNotIn("api_key_file", rows[0])

    def test_merge_store_accounts_overlay_wins(self):
        user = {"accounts": [
            {"type": "time", "label": "Grok", "expires_at": "2026-08-01"},
        ]}
        q.merge_store_accounts(user, [
            {"label": "Grok", "expires_at": "2026-08-19T20:12:00", "used_up": True},
            {"label": "New", "expires_at": "2026-09-01"},
        ])
        by = {a["label"]: a for a in user["accounts"]}
        self.assertEqual(by["Grok"]["expires_at"], "2026-08-19T20:12:00")
        self.assertTrue(by["Grok"]["used_up"])
        self.assertEqual(by["New"]["expires_at"], "2026-09-01")

    def test_apply_time_record_updates_dates_and_preserves_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({
                    "mode": "time",
                    "accounts": [{
                        "type": "glm",
                        "label": "GLM Pro",
                        "api_key_file": ".glm-key",
                        "started_at": "2026-01-01",
                    }],
                }, f)
            cfg = q.load_config(path)
            rec = q.normalize_time_record(cfg, {
                "label": "GLM Pro",
                "started_at": "2026-08-13",
                "expires_at": "2026-09-13",
                "period": "monthly",
            })
            out = q.apply_time_record(path, rec)
            self.assertTrue(out["ok"])
            with open(path) as f:
                saved = json.load(f)
            acc = saved["accounts"][0]
            self.assertEqual(acc["api_key_file"], ".glm-key")
            self.assertEqual(acc["expires_at"], "2026-09-13")
            self.assertEqual(acc["period"], "monthly")

    def test_apply_time_record_appends_a_new_time_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            rec = q.normalize_time_record(self.cfg, {
                "label": "Claude Pro",
                "expires_at": "2026-12-01",
            })
            q.apply_time_record(path, rec)
            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["accounts"][0]["type"], "time")
            self.assertEqual(saved["accounts"][0]["label"], "Claude Pro")
            self.assertEqual(saved["accounts"][0]["expires_at"], "2026-12-01")

    def test_apply_time_record_refuses_to_delete_a_key_bearing_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump({"accounts": [{
                    "type": "time", "label": "Grok",
                    "api_key_file": ".secret",
                }]}, f)
            with self.assertRaisesRegex(ValueError, "凭据"):
                q.apply_time_record(path, {"label": "Grok", "delete": True})
        self.assertIn("white-space: normal", q.PAGE_TEMPLATE)
        self.assertNotIn(".title > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", q.PAGE_TEMPLATE)

    def test_account_list_keeps_grok_and_cursor_and_skips_auth_dir(self):
        self.cfg["accounts"] = [
            {"type": "grok", "label": "Grok SuperGrok", "started_at": "2026-08-15"},
            {"type": "cursor", "label": "Cursor Pro", "started_at": "2026-08-15"},
        ]
        self.cfg["cliproxyapi_auth_dir"] = "/tmp/this-dir-should-not-be-opened"
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.listdir", return_value=["claude.json"]) as listdir:
            labels = [a["label"] for a in q.account_list(self.cfg)]
        self.assertEqual(labels, ["Grok SuperGrok", "Cursor Pro"])
        listdir.assert_not_called()

    def test_watchdog_in_time_mode_never_calls_providers(self):
        now = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now), \
             mock.patch.object(q, "fetch_one") as fetch, \
             mock.patch.object(q, "http_get") as http_get, \
             mock.patch.object(q, "http_post_json") as http_post, \
             mock.patch.object(q, "refresh_monthly_from_web") as monthly, \
             mock.patch.object(q, "load_state", return_value={}), \
             mock.patch.object(q, "save_state"), \
             mock.patch.object(q, "push") as push, \
             mock.patch.object(q, "log"):
            q.cmd_watchdog(self.cfg, True)
        fetch.assert_not_called()
        http_get.assert_not_called()
        http_post.assert_not_called()
        monthly.assert_not_called()
        push.assert_called_once()
        title, body = push.call_args[0][1], push.call_args[0][2]
        self.assertEqual(title, "每日套餐报告")
        self.assertIn("Grok SuperGrok", body)
        self.assertIn("还剩 25.2 天", body)

    def test_expiry_push_uses_account_expires_at(self):
        now = datetime.datetime(2026, 9, 12, 4, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now), \
             mock.patch.object(q, "load_state", return_value={}), \
             mock.patch.object(q, "save_state"), \
             mock.patch.object(q, "push") as push, \
             mock.patch.object(q, "log"):
            q.cmd_watchdog(self.cfg, False)
        push.assert_called_once()
        title, body = push.call_args[0][1], push.call_args[0][2]
        self.assertEqual(title, "套餐提醒")
        self.assertIn("【套餐到期】Grok SuperGrok 套餐还有 3 天到期（2026-09-15）", body)

    def test_cycle_end_is_pushed_only_when_there_is_no_hard_expiry(self):
        acct = {
            "type": "time",
            "label": "Rolling",
            "started_at": "2026-08-15",
            "period": "monthly",
        }
        self.cfg["accounts"] = [acct]
        now = datetime.datetime(2026, 9, 12, 4, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now), \
             mock.patch.object(q, "load_state", return_value={}), \
             mock.patch.object(q, "save_state"), \
             mock.patch.object(q, "push") as push, \
             mock.patch.object(q, "log"):
            q.cmd_watchdog(self.cfg, False)
        body = push.call_args[0][2]
        self.assertIn("【快到期】Rolling 本周期还剩 3 天（2026-09-15 结束）", body)

    def test_mixed_dashboard_keeps_polling_quota_accounts_only(self):
        cfg = dict(q.DEFAULTS)
        cfg["_tz"] = self.tz
        cfg["mode"] = "quota"
        cfg["accounts"] = [
            {"type": "glm", "label": "GLM Coding", "api_key": "secret"},
            {"type": "grok", "label": "Grok SuperGrok", "started_at": "2026-08-15",
             "expires_at": "2026-09-15"},
        ]
        fetched = []

        def fake_fetch(_cfg, acct):
            fetched.append(acct["label"])
            return {"windows": {"7d": (10, None)}}

        with mock.patch.object(q, "fetch_one", side_effect=fake_fetch):
            results = q.collect(cfg)
        self.assertEqual(fetched, ["GLM Coding"])
        self.assertIn("GLM Coding", results)
        self.assertNotIn("Grok SuperGrok", results)

    def test_lone_expiry_still_gets_a_now_marker(self):
        acct = {"type": "grok", "label": "Grok", "expires_at": "2026-08-19T20:12:00"}
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            tw = q.account_time_window(self.cfg, acct)
            html = q.card_html(self.cfg, acct, {"health": "ok"}, "ok")
        self.assertFalse(tw["expired"])
        self.assertIsNotNone(tw["elapsed_pct"])
        self.assertGreater(tw["elapsed_pct"], 0)
        self.assertIn('class="time-marker"', html)
        self.assertIn('title="现在"', html)

    def test_time_mode_page_sorts_soonest_expiry_first(self):
        self.cfg["accounts"] = [
            {"type": "cursor", "label": "Cursor Ultra", "started_at": "2026-08-13",
             "expires_at": "2026-09-13", "period": "monthly"},
            {"type": "grok", "label": "Grok", "expires_at": "2026-08-19T20:12:00"},
            {"type": "time", "label": "Kimi K3", "expires_at": "2026-08-22"},
        ]
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(q, "now_utc", return_value=now):
            ordered = sorted(self.cfg["accounts"], key=lambda a: q._time_sort_key(self.cfg, a))
        self.assertEqual([a["label"] for a in ordered], ["Grok", "Kimi K3", "Cursor Ultra"])

    def test_time_mode_page_axis_and_summary(self):
        now = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.timezone.utc)
        page_state = {"accounts": {}}
        with mock.patch.object(q, "now_utc", return_value=now), \
             mock.patch.object(q, "auth_health_map") as health:
            html = q.render_page(self.cfg, page_state, q.account_list(self.cfg))
        health.assert_not_called()
        self.assertIn('data-mode="time"', html)
        self.assertIn(">时间进度<", html)
        self.assertIn("最近到期 Grok SuperGrok 还有 25.2 天", html)
        self.assertIn('data-track="time"', html)


if __name__ == "__main__":
    unittest.main()
