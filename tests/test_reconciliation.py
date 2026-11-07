"""S1 positions reconciliation: an external broker snapshot that matches
recorded state lets the pipeline proceed; a divergent one stops it until
a human records the outside-workflow adjustments."""

import os
import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import rules_eval, stages  # noqa: E402

DAY = "2025-09-02"


class ReconciliationTest(WorkflowTestCase):
    def _write_snapshot(self, text):
        path = os.path.join(self.tmp.name, "%s_positions.csv" % DAY)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def ctx(self, **kw):
        kw.setdefault("inbox_dir", self.tmp.name)
        return super().ctx(**kw)

    def test_matching_snapshot_passes(self):
        self._write_snapshot(
            "AAPL,20\nMSFT,8\nNVDA,25\nQQQ,7\nIEF,40\nCOST,4\n"
            "CASH,10000.00\n")
        artifact = stages.s1_ingest(self.ctx(), DAY)
        recon = artifact["positions_reconciliation"]
        self.assertEqual(recon["status"], "ok")
        self.assertEqual(recon["diffs"], [])

    def test_divergent_snapshot_stops_pipeline(self):
        self._write_snapshot(
            "AAPL,25\nMSFT,8\nNVDA,25\nQQQ,7\nIEF,40\nCOST,4\n"
            "CASH,9000.00\n")
        with self.assertRaises(stages.PositionsDivergedError):
            stages.s1_ingest(self.ctx(), DAY)

        # Human resolves: record the outside-workflow adjustments, then
        # the same snapshot reconciles and S1 passes.
        self.store.apply_adjustment(DAY, "external_trade", symbol="AAPL",
                                    qty_delta=5, note="manual buy")
        self.store.apply_adjustment(DAY, "cash", cash_delta=-1000.0,
                                    note="manual buy funding")
        artifact = stages.s1_ingest(self.ctx(), DAY)
        self.assertEqual(artifact["positions_reconciliation"]["status"],
                         "ok")
        self.assertEqual(self.store.get_positions()["AAPL"], 25.0)
        self.assertAlmostEqual(self.store.get_cash(), 9000.0)
        self.assertEqual(len(self.store.get_adjustments()), 2)

    def test_reconstruct_reverses_adjustments(self):
        self.store.apply_adjustment("2025-09-10", "external_trade",
                                    symbol="AAPL", qty_delta=5)
        self.store.apply_adjustment("2025-09-10", "cash",
                                    cash_delta=-1150.0)
        days = ["2025-09-02", "2025-09-10", "2025-09-30"]
        pos, cash = rules_eval.reconstruct_start_state(self.ctx(), days)
        self.assertEqual(pos.get("AAPL"), 20.0)
        self.assertAlmostEqual(cash, 10000.0)

    def test_no_inbox_file_means_no_reconciliation(self):
        artifact = stages.s1_ingest(self.ctx(), DAY)
        self.assertNotIn("positions_reconciliation", artifact)


if __name__ == "__main__":
    unittest.main()
