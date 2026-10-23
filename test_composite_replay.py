import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from composite_replay import evaluate, pure_composite_scores


class CompositeIntegrationTest(unittest.TestCase):
    def test_four_horizons_only_and_complete_coverage(self):
        f = pd.DataFrame({"signal_date": ["20260102"] * 3, "ts_code": ["C", "A", "B"],
                          "k_e1": [1, 3, 2], "k_e3": [1, 3, 2],
                          "k_e5": [1, 3, 2], "k_e10": [np.nan, 3, 2],
                          "set10": [999, -999, 0], "k_e2": [999, -999, 0]})
        scores = pure_composite_scores(f)
        self.assertGreater(scores.loc["20260102", "A"], scores.loc["20260102", "B"])
        self.assertTrue(np.isnan(scores.loc["20260102", "C"]))
        changed = f.copy(); changed["set10"] *= -1; changed["k_e2"] *= -1
        pd.testing.assert_frame_equal(scores, pure_composite_scores(changed))
        future = f.assign(signal_date="20260105", k_e1=[9, 1, 7])
        pd.testing.assert_frame_equal(scores, pure_composite_scores(pd.concat([f, future])).loc[scores.index])

    def test_end_to_end_nav_serialization_and_daily_calendar(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            dates = pd.date_range("2026-01-01", periods=8)
            pd.DataFrame({"trade_date": dates.strftime("%Y%m%d"), "close": [10, 9, 10, 11, 12, 13, 12, 11]}).to_csv(p / "index.csv", index=False)
            rows = [{"timestamps": day, "ts_code": code, "open": price, "close": price,
                     "volume": 100} for day in dates for code, price in [("A", 10), ("B", 20)]]
            pd.DataFrame(rows).to_parquet(p / "panel.parquet", index=False)
            # Score gaps must not create gaps in the valuation calendar.
            pd.DataFrame([{"signal_date": day, "ts_code": code,
                           **{f"k_e{h}": score for h in (1, 3, 5, 10)}}
                          for day in ("20260101", "20260105") for code, score in [("A", 2), ("B", 1)]]).to_csv(p / "curves.csv", index=False)
            result = evaluate(p / "panel.parquet", p / "curves.csv", p / "index.csv", p / "out",
                              end="20260108", top_k=1, exit_rank=2)
            self.assertEqual(len(result["results"]), 6)
            self.assertEqual(result["input_audit"]["market_sessions"], 8)
            off = next(r for r in result["results"] if r["mode"] == "off" and r["fee_bps"] == 0)
            self.assertAlmostEqual(off["total_return"], 0)
            self.assertEqual(off["max_n_held"], 1)
            fee = next(r for r in result["results"] if r["mode"] == "off" and r["fee_bps"] == 10)
            self.assertLess(fee["total_return"], off["total_return"])
            snapshot = json.loads((p / "out/off_fee10bps/factor_snapshot.json").read_text())
            self.assertEqual(snapshot["effective_parameters"]["set10_weight"], 0)
            self.assertEqual(snapshot["effective_parameters"]["kronos_ema"], 0)
            self.assertTrue(snapshot["book_summary"]["comp"]["fully_liquidated"])


if __name__ == "__main__":
    unittest.main()
