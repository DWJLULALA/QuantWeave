"""Unit tests for labels, metrics and walk-forward isolation."""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import chan_multi_engine as engine


class LabelTests(unittest.TestCase):
    def _frame(self, close):
        return pd.DataFrame({
            "dt": pd.bdate_range("2026-01-05", periods=len(close)),
            "close": np.asarray(close, dtype=float),
        })

    def test_path_label_requires_sustained_move(self):
        up = engine.add_labels(self._frame([100, 101, 102, 103, 104, 105]),
                               3, 0.01, mode="path", pullback=0.02)
        self.assertEqual(int(up.loc[0, "label"]), 1)
        flat = engine.add_labels(self._frame([100, 101, 102, 100, 99, 100]),
                                 3, 0.01, mode="path", pullback=0.02)
        self.assertEqual(int(flat.loc[0, "label"]), 0)
        down = engine.add_labels(self._frame([100, 99, 98, 97, 96, 95]),
                                 3, 0.01, mode="path", pullback=0.02)
        self.assertEqual(int(down.loc[0, "label"]), -1)

    def test_endpoint_label_is_kept_for_comparison(self):
        frame = self._frame([100, 101, 102, 100, 101, 100])
        out = engine.add_labels(frame, 3, 0.01, mode="endpoint")
        self.assertEqual(int(out.loc[0, "label"]), 0)  # 100 -> 100
        frame = self._frame([100, 101, 102, 98, 99, 100])
        out = engine.add_labels(frame, 3, 0.01, mode="endpoint")
        self.assertEqual(int(out.loc[0, "label"]), -1)  # 100 -> 98 is -2%

    def test_last_horizon_rows_are_nan(self):
        out = engine.add_labels(self._frame([100, 101, 102, 103]), 3, 0.01)
        self.assertTrue(out["label"].iloc[-3:].isna().all())
        self.assertTrue(out["fwd_ret"].iloc[-3:].isna().all())


class MetricTests(unittest.TestCase):
    def test_balanced_accuracy_and_confusion(self):
        y = np.array([-1, -1, 0, 0, 0, 1, 1])
        p = np.array([-1, 0, 0, 0, 1, 1, 0])
        self.assertAlmostEqual(engine.balanced_accuracy(y, p), (0.5 + 2 / 3 + 0.5) / 3)
        m = engine.metrics(y, p)
        self.assertEqual(m["confusion"], [[1, 1, 0], [0, 2, 1], [0, 1, 1]])
        self.assertEqual(m["class_counts"], {"-1": 2, "0": 3, "1": 2})

    def test_missing_class_is_not_counted_as_zero_recall(self):
        y = np.array([0, 0, 1, 1])
        p = np.array([0, 0, 1, 1])
        self.assertAlmostEqual(engine.balanced_accuracy(y, p), 1.0)
        self.assertIsNone(engine.metrics(y, p)["recall"]["-1"])


class AlignmentTests(unittest.TestCase):
    def test_asof_never_uses_a_future_bar(self):
        decisions = pd.DataFrame({
            "dt_decision": pd.to_datetime(["2026-01-05 15:00", "2026-01-06 15:00",
                                           "2026-01-07 15:00"]),
            "dt": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
        })
        states = pd.DataFrame({"dt": pd.to_datetime(["2026-01-05 15:00", "2026-01-06 15:00"])})
        for col in engine.STATE_COLS:
            states[col] = 0.0
        states["phase"] = [1, -1]
        out = engine._asof(decisions, states, "ld_", pd.Timedelta(days=1), same_day=False)
        self.assertEqual(out["ld_phase"].tolist(), [1.0, -1.0, -1.0])
        # Same-day matching rejects the stale prior-day bar.
        out2 = engine._asof(decisions, states, "ld_", pd.Timedelta(days=1), same_day=True)
        self.assertTrue(np.isnan(out2["ld_phase"].iloc[2]))
        self.assertEqual(out2["ld_phase"].iloc[0], 1.0)


class WalkForwardTests(unittest.TestCase):
    def test_training_labels_end_before_the_test_year(self):
        n = 900
        frame = pd.DataFrame({
            "dt": pd.bdate_range("2018-01-01", periods=n),
            "label": np.tile([-1, 0, 1], n // 3 + 1)[:n],
        })
        frame["label_end"] = frame["dt"].shift(-5)
        seen = []

        def fake_fit(train, test, cols):
            seen.append((train["label_end"].max(), test["dt"].min()))
            return np.zeros(len(test), dtype=int), {
                "classes": [-1, 0, 1],
                "proba": np.full((len(test), 3), 1 / 3),
            }

        with mock.patch.object(engine, "_fit_predict", side_effect=fake_fit):
            pred, proba, years = engine.walk_forward(frame, ["dt"], start_year=2020, min_train=100)
        self.assertTrue(years)
        for train_end, test_start in seen:
            self.assertLess(train_end, test_start)

    def test_hierarchical_cut_stays_in_training_window(self):
        # The calibration tail is part of train; the selected cut must be a
        # member of the pre-declared grid, not a value fitted on test labels.
        rng = np.random.default_rng(3)
        n = 800
        frame = pd.DataFrame({
            "dt": pd.bdate_range("2018-01-01", periods=n),
            "x1": rng.normal(size=n),
            "x2": rng.normal(size=n),
            "label": np.tile([-1, 0, 1], n // 3 + 1)[:n],
        })
        frame["label_end"] = frame["dt"].shift(-5)
        train = frame.iloc[:500]
        test = frame.iloc[500:520]
        pred, info = engine._fit_predict_hier(train, test, ["x1", "x2"])
        self.assertIsNotNone(pred)
        self.assertIn(info["trend_cut"], (0.30, 0.35, 0.40, 0.45, 0.50, 0.55))


if __name__ == "__main__":
    unittest.main()
