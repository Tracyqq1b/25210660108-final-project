"""Report rendering: numbers come only from persisted artifacts, and both
day shapes (HOLD / REBALANCE) plus the monthly rules report render."""

import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import controller, render, rules_eval  # noqa: E402


class ReportTest(WorkflowTestCase):
    def test_rebalance_day_report(self):
        controller.run_day(self.ctx(), "2025-09-02")
        controller.run_day(self.ctx(), "2025-09-03")
        text = render.render_day(self.store, "2025-09-02")
        self.assertIn("# 日终风险报告 2025-09-02", text)
        self.assertIn("REBALANCE", text)
        self.assertIn("QQQ", text)
        self.assertIn("独立复核(S4):**pass**", text)
        self.assertIn("人工审批(S5):**approved**", text)
        self.assertIn("新挂待执行订单", text)  # fills land on the next day
        self.assertIn("审计链", text)
        self.assertIn("S6:", text)  # artifact hash chain entry

        next_day = render.render_day(self.store, "2025-09-03")
        self.assertIn("批准日参考价", next_day)  # T+1 fill reconciliation

    def test_hold_day_report(self):
        ctx = self.ctx()
        hold_day = None
        for day in ["2025-09-02", "2025-09-03", "2025-09-04",
                    "2025-09-05", "2025-09-08"]:
            controller.run_day(ctx, day)
            s2 = self.store.stage_artifact(day, "S2")
            if s2 and not s2["breach"]:
                hold_day = day
                break
        if hold_day is None:
            self.skipTest("no clean HOLD day in probe window")
        text = render.render_day(self.store, hold_day)
        self.assertIn("无越界", text)
        self.assertIn("**HOLD**", text)

    def test_unrun_day_returns_none(self):
        self.assertIsNone(render.render_day(self.store, "2025-09-02"))

    def test_month_report(self):
        ctx = self.ctx()
        for day in ["2025-09-02", "2025-09-03", "2025-09-04"]:
            controller.run_day(ctx, day)
        artifact = rules_eval.evaluate(ctx, "2025-09")
        digest = self.store.save_artifact(artifact["run_date"], "S7",
                                          artifact)
        self.store.begin_stage(artifact["run_date"], "S7")
        self.store.complete_stage(artifact["run_date"], "S7", digest)
        text = render.render_month(self.store, "2025-09")
        self.assertIn("# 月度规则评估报告 2025-09", text)
        self.assertIn("baseline_buy_hold", text)
        self.assertIn("current_rules_v1", text)


if __name__ == "__main__":
    unittest.main()
