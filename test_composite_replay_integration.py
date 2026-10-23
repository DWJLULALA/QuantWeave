"""End-to-end tests for the pure-score loader and saved account artefacts."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from composite_replay import evaluate, prepare_inputs, pure_composite_scores


class CompositeReplayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dates = pd.bdate_range("2026-08-03", periods=5)
        self.days = self.dates.strftime("%Y%m%d").tolist()
        self.curves = pd.DataFrame([
            {"signal_date": d, "ts_code": c, "k_e1": s, "k_e3": s,
             "k_e5": s, "k_e10": s, "set10": -100000 * s}
            for d in self.days for c, s in (("A", 3), ("B", 2), ("C", 1))])
        self.panel = pd.DataFrame([
            {"timestamps": d, "ts_code": c, "open": 100,
             "close": (110 if c == "A" else 90 if c == "B" else 100) if i == 4 else 100,
             "volume": 10000}
            for i, d in enumerate(self.dates) for c in ("A", "B", "C")])
        self.index = pd.DataFrame({"trade_date": self.days, "close": [100, 101, 99, 101, 100]})

    def tearDown(self):
        self.tmp.cleanup()

    def write_inputs(self):
        paths = []
        for name, frame in (("panel", self.panel), ("curves", self.curves), ("index", self.index)):
            path = self.root / f"{name}.csv"
            frame.to_csv(path, index=False)
            paths.append(path)
        return paths

    def test_pure_ranks_ignore_set10_and_require_four_observed_horizons(self):
        original = pure_composite_scores(self.curves)
        modified = self.curves.copy()
        modified["set10"] *= -999
        pd.testing.assert_frame_equal(original, pure_composite_scores(modified))
        self.assertEqual(original.columns.tolist(), ["A", "B", "C"])
        self.assertAlmostEqual(original.iloc[0].A, 1)
        modified.loc[modified.ts_code == "A", "k_e10"] = np.nan
        self.assertTrue(pure_composite_scores(modified).A.isna().all())

    def test_csv_inputs_to_six_saved_variants_have_reconciled_nav_and_costs(self):
        paths = self.write_inputs()
        result = evaluate(*paths, self.root / "out", top_k=2, exit_rank=3)
        self.assertEqual(len(result["results"]), 6)
        for row in result["results"]:
            destination = self.root / "out" / f'{row["mode"]}_fee{row["fee_bps"]:g}bps'
            snapshot = json.loads((destination / "factor_snapshot.json").read_text())
            daily = pd.read_csv(destination / "factor_daily.csv")
            fills = pd.read_csv(destination / "factor_fills.csv")
            trades = pd.read_csv(destination / "factor_trades_comp.csv")
            metric = snapshot["book_summary"]["comp"]
            self.assertEqual(snapshot["score_source"], "pure_e1e3e5e10")
            self.assertEqual(snapshot["effective_parameters"]["set10_weight"], 0)
            self.assertEqual(len(daily), 5)
            np.testing.assert_allclose(daily.equity, daily.cash + daily.market_value)
            self.assertAlmostEqual(daily.nav.iloc[-1] - 1, metric["total_return"])
            self.assertAlmostEqual(fills.fee.sum(), metric["total_fees"])
            self.assertAlmostEqual(trades.fees.sum(), metric["total_fees"])
            self.assertAlmostEqual(trades.pnl.sum(), metric["final_equity"] - metric["initial_cash"])
            self.assertTrue((daily.cash >= 0).all())
            self.assertTrue((daily.n_held <= 2).all())
        off = [r for r in result["results"] if r["mode"] == "off"]
        self.assertAlmostEqual(off[0]["total_return"], 0)
        self.assertLess(off[1]["total_return"], 0)

    def test_sparse_signal_days_preserve_full_market_calendar(self):
        self.curves = self.curves[self.curves.signal_date == self.days[0]]
        paths = self.write_inputs()
        result = evaluate(*paths, self.root / "out", end=self.days[-1],
                          modes=("off",), fees=(0,), top_k=2, exit_rank=3, single=True)
        self.assertEqual(result["input_audit"]["signal_sessions"], 1)
        self.assertEqual(result["input_audit"]["market_sessions"], 5)
        self.assertEqual(len(result["input_audit"]["missing_signal_dates"]), 4)
        self.assertEqual(result["book_summary"]["comp"]["buy_count"], 2)

    def test_zero_volume_last_day_cannot_be_fabricated_liquidation(self):
        self.panel.loc[(self.panel.timestamps == self.dates[-1]) & (self.panel.ts_code == "A"), "volume"] = 0
        paths = self.write_inputs()
        result = evaluate(*paths, self.root / "out", modes=("off",), fees=(0,),
                          top_k=1, exit_rank=3, single=True)
        self.assertEqual(result["book_summary"]["comp"]["unliquidated_count"], 1)
        self.assertEqual(result["unliquidated"][0]["code"], "A")

    def test_missing_all_prices_for_a_session_fails_instead_of_skipping_date(self):
        self.panel = self.panel[self.panel.timestamps != self.dates[2]]
        paths = self.write_inputs()
        with self.assertRaisesRegex(ValueError, "missing entire"):
            prepare_inputs(*paths)

    def test_parquet_end_date_includes_close_timestamp_on_that_day(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow not installed")
        paths = self.write_inputs()
        self.panel["timestamps"] += pd.Timedelta(hours=15)
        panel_path = self.root / "panel.parquet"
        self.panel.to_parquet(panel_path, index=False)
        opens, closes, _, _, _, meta = prepare_inputs(panel_path, paths[1], paths[2])
        self.assertEqual(meta["end"], self.days[-1])
        self.assertEqual(len(opens), 5)
        self.assertEqual(closes.loc[self.days[-1], "A"], 110)


if __name__ == "__main__":
    unittest.main()
