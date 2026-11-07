"""No future-information leakage: every read through the data feed is
bounded by its as_of date, and non-trading days never silently fall back
to stale prices."""

import unittest

from common import WorkflowTestCase  # noqa: E402

from pma import datafeed  # noqa: E402

SYMBOLS = ["AAPL", "MSFT", "NVDA", "QQQ", "IEF", "COST"]
PROBE_DATES = ["2024-12-31", "2025-03-14", "2025-06-30", "2025-09-15"]


class LeakageTest(WorkflowTestCase):
    def test_load_never_returns_rows_after_as_of(self):
        for sym in SYMBOLS:
            for day in PROBE_DATES:
                df = datafeed.load(sym, day)
                self.assertTrue((df["Date"] <= day).all(),
                                "%s leaked rows after %s" % (sym, day))
                _, cache_end = datafeed.cache_range(sym)
                self.assertLess(day, cache_end)

    def test_price_on_rejects_non_trading_days(self):
        with self.assertRaises(datafeed.DataNotCachedError):
            datafeed.price_on("AAPL", "2025-09-01")  # Labor Day
        with self.assertRaises(datafeed.DataNotCachedError):
            datafeed.price_on("AAPL", "2025-09-06")  # Saturday

    def test_simulation_days_are_bounded(self):
        days = datafeed.trading_days("2025-09-02", "2025-09-30")
        self.assertTrue(all("2025-09-02" <= d <= "2025-09-30" for d in days))
        self.assertEqual(len(days), len(set(days)))

    def test_uncached_symbol_raises(self):
        with self.assertRaises(datafeed.DataNotCachedError):
            datafeed.load("TSLA", "2025-09-15")


if __name__ == "__main__":
    unittest.main()
