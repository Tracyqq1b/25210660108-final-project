"""Async approval flow: an actionable proposal pauses the day at S5
(status 'waiting', approval document written), and the day resumes only
after a recorded decision via the approve path."""

import os
import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import controller  # noqa: E402


class AsyncApprovalTest(WorkflowTestCase):
    DAY = "2025-09-02"  # drift-breach day

    def _decide(self, decision):
        proposal = self.store.stage_artifact(self.DAY, "S3")
        proposal_hash = self.store.save_artifact(self.DAY, "S3", proposal)
        return self.store.record_approval(
            self.DAY, proposal_hash, decision,
            "operator decision via approve command")

    def test_proposal_pauses_day_and_writes_document(self):
        _, paused = controller.run_day(self.ctx(auto_approve=False),
                                       self.DAY)
        self.assertTrue(paused)
        self.assertEqual(self.store.stage_status(self.DAY, "S5"), "waiting")
        self.assertEqual(self.store.stage_status(self.DAY, "S6"), None)
        self.assertEqual(self.store.get_pending_orders(), [])
        doc = os.path.join(self.tmp.name, "approvals",
                           "%s.md" % self.DAY)
        self.assertTrue(os.path.exists(doc))
        with open(doc) as fh:
            text = fh.read()
        self.assertIn("# Approval Request %s" % self.DAY, text)
        self.assertIn("approve --day %s" % self.DAY, text)

    def test_approve_resumes_and_places_pending_orders(self):
        controller.run_day(self.ctx(auto_approve=False), self.DAY)
        self._decide("approved")
        _, paused = controller.run_day(self.ctx(auto_approve=False),
                                       self.DAY)
        self.assertFalse(paused)
        self.assertEqual(self.store.stage_status(self.DAY, "S5"), "done")
        self.assertEqual(self.store.stage_status(self.DAY, "S6"), "done")
        self.assertTrue(self.store.get_pending_orders())

    def test_reject_blocks_execution(self):
        controller.run_day(self.ctx(auto_approve=False), self.DAY)
        self._decide("rejected")
        controller.run_day(self.ctx(auto_approve=False), self.DAY)
        self.assertEqual(self.store.stage_status(self.DAY, "S6"), "done")
        self.assertEqual(self.store.get_pending_orders(), [])
        controller.run_day(self.ctx(auto_approve=False), "2025-09-03")
        self.assertEqual(self.store.get_trades(), [])

    def test_hold_day_never_waits(self):
        controller.run_day(self.ctx(auto_approve=False), self.DAY)
        self._decide("approved")
        controller.run_day(self.ctx(auto_approve=False), self.DAY)
        # 2025-09-03: QQQ breach is covered by the in-flight order -> HOLD
        _, paused = controller.run_day(self.ctx(auto_approve=False),
                                       "2025-09-03")
        self.assertFalse(paused)
        self.assertEqual(self.store.stage_status("2025-09-03", "S6"),
                         "done")


if __name__ == "__main__":
    unittest.main()
