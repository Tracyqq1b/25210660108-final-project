"""Crash-recovery: an interrupted run must resume from checkpoints without
repeating completed stages, and the final state must be identical to an
uninterrupted run."""

import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import controller  # noqa: E402


class RecoveryTest(WorkflowTestCase):
    def test_resume_after_crash_skips_completed_stages(self):
        crash_day = "2025-09-03"
        ctx = self.ctx(crash_after="S2", crash_on=crash_day)
        controller.run_day(self.ctx(), "2025-09-02")
        with self.assertRaises(controller.SimulatedCrash):
            controller.run_day(ctx, crash_day)

        done_before = {"%s:%s" % (r["run_date"], r["stage"])
                       for r in self.store.conn.execute(
                           "SELECT run_date, stage FROM stage_runs"
                           " WHERE status = 'done'").fetchall()}
        self.assertIn(crash_day + ":S1", done_before)
        self.assertIn(crash_day + ":S2", done_before)
        self.assertNotIn(crash_day + ":S3", done_before)
        attempts_before = self.store.attempts(crash_day, "S1")

        # Resume with a fresh context (simulating a process restart).
        controller.run_day(self.ctx(), crash_day)
        for stage in controller.STAGES:
            self.assertEqual(self.store.stage_status(crash_day, stage),
                             "done", "%s not completed after resume" % stage)
        self.assertEqual(self.store.attempts(crash_day, "S1"),
                         attempts_before,
                         "S1 was re-executed after checkpoint resume")
        self.assertEqual(self.store.attempts(crash_day, "S2"), 1,
                         "S2 was re-executed after checkpoint resume")

    def test_crashed_and_clean_runs_reach_same_state(self):
        ctx = self.ctx(crash_after="S4", crash_on="2025-09-04")
        for day in ["2025-09-02", "2025-09-03"]:
            controller.run_day(self.ctx(), day)
        with self.assertRaises(controller.SimulatedCrash):
            controller.run_day(ctx, "2025-09-04")
        controller.run_day(self.ctx(), "2025-09-04")
        controller.run_day(self.ctx(), "2025-09-05")
        crashed_cash = self.store.get_cash()
        crashed_pos = self.store.get_positions()

        other = WorkflowTestCase("runTest")
        other.setUp()
        try:
            for day in ["2025-09-02", "2025-09-03", "2025-09-04",
                        "2025-09-05"]:
                controller.run_day(other.ctx(), day)
            self.assertAlmostEqual(crashed_cash, other.store.get_cash(),
                                   places=6)
            self.assertEqual(crashed_pos, other.store.get_positions())
        finally:
            other.tearDown()


if __name__ == "__main__":
    unittest.main()
