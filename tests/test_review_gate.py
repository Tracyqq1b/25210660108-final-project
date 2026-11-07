"""Approval gate and independent review:

- S6 refuses to execute without an approval record for the exact proposal.
- S6 refuses when the approval references a different proposal hash.
- Executed fills must tie back to the approval and match the proposal.
- The S4 reviewer fails proposals whose numbers do not reproduce from
  source data.
"""

import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import controller, review, stages  # noqa: E402


def run_to(ctx, store, day, up_to):
    for stage in controller.STAGES:
        if stage == up_to:
            return
        ctx.store.begin_stage(day, stage)
        artifact = controller._STAGE_FNS[stage](ctx, day)
        digest = store.save_artifact(day, stage, artifact)
        store.complete_stage(day, stage, digest)


class ApprovalGateTest(WorkflowTestCase):
    DAY = "2025-09-02"  # a day with a drift breach (QQQ underweight)

    def test_no_execution_without_approval(self):
        run_to(self.ctx(), self.store, self.DAY, "S5")
        with self.assertRaises(RuntimeError):
            stages.s6_execute(self.ctx(), self.DAY)

    def test_execution_refused_on_proposal_mismatch(self):
        run_to(self.ctx(), self.store, self.DAY, "S5")
        self.store.record_approval(self.DAY, "deadbeefdeadbeef", "approved")
        artifact = stages.s5_approval(self.ctx(), self.DAY, auto=True)
        digest = self.store.save_artifact(self.DAY, "S5", artifact)
        self.store.complete_stage(self.DAY, "S5", digest)
        # overwrite the stored S3 artifact with a tampered proposal so the
        # hash recorded in S5 no longer matches a fresh hash of S3 content
        proposal = self.store.stage_artifact(self.DAY, "S3")
        proposal["trades"] = proposal["trades"] + [
            {"symbol": "AAPL", "side": "BUY", "qty": 999,
             "est_price": 1.0, "est_notional": 999.0, "reason": "tampered"}]
        self.store.save_artifact(self.DAY, "S3", proposal)
        with self.assertRaises(RuntimeError):
            stages.s6_execute(self.ctx(), self.DAY)

    def test_fills_match_approved_proposal(self):
        controller.run_day(self.ctx(), self.DAY)
        proposal = self.store.stage_artifact(self.DAY, "S3")
        approval = self.store.stage_artifact(self.DAY, "S5")
        trades = self.store.get_trades(self.DAY)
        self.assertEqual(len(trades), len(proposal["trades"]))
        for trade, planned in zip(trades, proposal["trades"]):
            self.assertEqual(trade["symbol"], planned["symbol"])
            self.assertEqual(trade["side"], planned["side"])
            self.assertEqual(trade["qty"], planned["qty"])
            self.assertEqual(trade["approval_id"], approval["approval_id"])

    def test_review_fails_tampered_prices(self):
        run_to(self.ctx(), self.store, self.DAY, "S4")
        proposal = self.store.stage_artifact(self.DAY, "S3")
        self.assertTrue(proposal["trades"], "expected a breach day")
        proposal["trades"][0]["est_price"] *= 2.0  # tamper
        digest = self.store.save_artifact(self.DAY, "S3", proposal)
        self.store.complete_stage(self.DAY, "S3", digest)
        result = review.review(self.ctx(), self.DAY)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any(not c["pass"] for c in result["checks"]))

    def test_review_passes_untampered(self):
        run_to(self.ctx(), self.store, self.DAY, "S4")
        result = review.review(self.ctx(), self.DAY)
        self.assertEqual(result["status"], "pass",
                         str([c for c in result["checks"] if not c["pass"]]))


if __name__ == "__main__":
    unittest.main()
