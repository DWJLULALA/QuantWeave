"""Whole-session repair isolation and provenance for the daily replay inputs."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from composite_replay import DAILY_REPAIRS_DIR, apply_daily_repairs, evaluate, prepare_inputs


class DailyRepairsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repairs = self.root / "repairs"
        self.repairs.mkdir()
        self.dates = pd.bdate_range("2026-08-03", periods=3)
        self.days = self.dates.strftime("%Y%m%d").tolist()
        self.full = pd.DataFrame([
            {"timestamps": day, "ts_code": code, "open": 10.0, "close": 10.0, "volume": 100}
            for day in self.dates for code in ("A", "B")])
        self.panel_path = self.root / "panel.parquet"
        self.full[self.full.timestamps != self.dates[1]].to_parquet(self.panel_path, index=False)
        self.index_path = self.root / "index.csv"
        pd.DataFrame({"trade_date": self.days, "close": [100, 101, 100]}).to_csv(self.index_path, index=False)
        self.curves_path = self.root / "curves.csv"
        pd.DataFrame([{"signal_date": day, "ts_code": code,
                       **{f"k_e{h}": score for h in (1, 3, 5, 10)}}
                      for day in self.days for code, score in (("A", 2), ("B", 1))]
                     ).to_csv(self.curves_path, index=False)

    def tearDown(self):
        self.tmp.cleanup()

    def write_repair(self, frame=None, name="repair.parquet"):
        path = self.repairs / name
        (self.full[self.full.timestamps == self.dates[1]] if frame is None else frame).to_parquet(path, index=False)
        return path

    def load(self, **kwargs):
        return prepare_inputs(self.panel_path, self.curves_path, self.index_path,
                              repairs_dir=self.repairs, **kwargs)

    def test_only_absent_session_is_filled_and_saved_with_source_hash(self):
        repair = self.full.copy()
        repair.loc[repair.timestamps == self.dates[0], ["open", "close"]] = 999
        repair = pd.concat([repair, self.full.iloc[:2].assign(timestamps=pd.Timestamp("2026-08-08"))])
        path = self.write_repair(repair)
        source_hash = hashlib.sha256(self.panel_path.read_bytes()).hexdigest()
        opens, _, _, _, _, meta = self.load()
        self.assertEqual(opens.index.tolist(), self.days)
        self.assertEqual(opens.loc[self.days[0], "A"], 10)
        self.assertEqual(opens.loc[self.days[1], "A"], 10)
        expected = {"file": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "dates": [self.days[1]], "rows": 2}
        self.assertEqual(meta["daily_repairs"], [expected])
        self.assertEqual(meta["missing_panel_dates_before_repair"], [self.days[1]])
        self.assertEqual(hashlib.sha256(self.panel_path.read_bytes()).hexdigest(), source_hash)
        result = evaluate(self.panel_path, self.curves_path, self.index_path, self.root / "out",
                          modes=("off",), fees=(0,), top_k=1, exit_rank=2,
                          repairs_dir=self.repairs, single=True)
        saved = json.loads((self.root / "out/factor_snapshot.json").read_text())
        self.assertEqual(saved["input_audit"]["daily_repairs"], [expected])
        self.assertEqual(result["input_audit"]["daily_repairs"], [expected])

    def test_partial_existing_date_is_never_overwritten_or_filled(self):
        panel = self.full[~((self.full.timestamps == self.dates[1]) & (self.full.ts_code == "B"))]
        panel.to_parquet(self.panel_path, index=False)
        self.write_repair(self.full.assign(open=999, close=999))
        opens, _, _, _, _, meta = self.load()
        self.assertEqual(opens.loc[self.days[1], "A"], 10)
        self.assertTrue(np.isnan(opens.loc[self.days[1], "B"]))
        self.assertEqual(meta["daily_repairs"], [])

    def test_dates_outside_requested_window_are_not_added(self):
        self.write_repair()
        opens, _, _, _, _, meta = self.load(start=self.days[0], end=self.days[0])
        self.assertEqual(opens.index.tolist(), [self.days[0]])
        self.assertEqual(meta["daily_repairs"], [])

    def test_missing_session_without_applicable_repair_still_errors(self):
        self.write_repair(self.full[self.full.timestamps == self.dates[0]])
        with self.assertRaisesRegex(ValueError, "missing entire.*20260804"):
            self.load()

    def test_conflicting_files_for_same_absent_day_are_rejected(self):
        self.write_repair(name="one.parquet")
        self.write_repair(name="two.parquet")
        with self.assertRaisesRegex(ValueError, "Multiple daily repair files"):
            self.load()

    def test_duplicate_repair_rows_are_rejected(self):
        day = self.full[self.full.timestamps == self.dates[1]]
        self.write_repair(pd.concat([day, day]))
        with self.assertRaisesRegex(ValueError, "Duplicate daily repair"):
            self.load()

    def test_bundled_official_repair_contains_only_verified_date(self):
        path = DAILY_REPAIRS_DIR / "tushare_daily_20260929.parquet"
        day = pd.read_parquet(path)
        self.assertEqual(day.timestamps.dt.strftime("%Y%m%d").unique().tolist(), ["20260929"])
        self.assertEqual(len(day), 5559)
        self.assertFalse(day.duplicated(["timestamps", "ts_code"]).any())


if __name__ == "__main__":
    unittest.main()
