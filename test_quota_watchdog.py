import datetime
import unittest

import quota_watchdog as q


class QuotaScaleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
