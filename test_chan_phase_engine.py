"""Regression tests for the as-of-close, close-only Chan phase approximation."""
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import chan_phase_engine as chan


EXAMPLE = np.array(
    [10, 9, 10, 11, 12, 13, 12, 11, 10, 8, 9, 10, 11, 12, 14, 13.5, 12, 13, 15, 14],
    dtype=float,
)
REAL_INDEX = Path("/tmp/chan_gate_audit_data/index_000300_SH.csv")


class CausalityTests(unittest.TestCase):
    def assert_prefixes_unchanged(self, close, stops):
        dif = chan.macd(pd.Series(close))[0].to_numpy()
        full = chan.chan_phases(close, dif)
        for stop in stops:
            prefix = chan.chan_phases(close[:stop], dif[:stop])
            for field in (0, 1):
                np.testing.assert_allclose(
                    prefix[field], full[field][:stop], equal_nan=True,
                    err_msg=f"{'phase' if field == 0 else 'div'} repainted at prefix {stop}",
                )

    def test_original_twenty_point_counterexample(self):
        self.assert_prefixes_unchanged(EXAMPLE, range(1, len(EXAMPLE) + 1))

    def test_divergence_remains_after_same_kind_fractal_replaces_endpoint(self):
        # The upward leg confirmed at 15 later extends to the top confirmed at 19.
        # Both divergences were observable in real time; the first must not vanish.
        dif = np.full(len(EXAMPLE), 2.0)
        dif[5] = 5.0
        full = chan.chan_phases(EXAMPLE, dif)
        prefix = chan.chan_phases(EXAMPLE[:16], dif[:16])
        np.testing.assert_allclose(full[1][:16], prefix[1], equal_nan=True)
        self.assertEqual(full[1][15], -1)
        self.assertEqual(full[1][19], -1)
        self.assertEqual(full[3]["s1_div"], 2)

    def test_online_state_matches_prefix_structure_rule(self):
        close = 100 * np.exp(np.cumsum(np.random.default_rng(29).normal(0, 0.03, 500)))
        phase = chan.chan_phases(close, chan.macd(pd.Series(close))[0].to_numpy())[0]
        # Independent snapshot oracle: build the structure from each prefix only.
        # This guards against fixing repainting by silently changing the phase rule.
        for day in range(len(close)):
            observed = close[:day + 1]
            _, legs = chan.strokes(chan.fractals(observed), observed)
            expected = np.nan if not legs else legs[-1]["dir"]
            for k in range(len(legs) - 1, 1, -1):
                triple = legs[k - 2:k + 1]
                lo = max(min(leg["p1"], leg["p2"]) for leg in triple)
                hi = min(max(leg["p1"], leg["p2"]) for leg in triple)
                if hi > lo:
                    expected = 0 if lo <= close[day] <= hi else (1 if close[day] > hi else -1)
                    break
            np.testing.assert_allclose(phase[day], expected, equal_nan=True)

    def test_empty_and_invalid_inputs(self):
        self.assertEqual(len(chan.chan_phases(np.array([]), np.array([]))[0]), 0)
        with self.assertRaises(ValueError):
            chan.chan_phases(np.array([1, np.nan]), np.array([1, 2]))
        with self.assertRaises(ValueError):
            chan.chan_phases(np.array([1, 2]), np.array([1]))

    @unittest.skipUnless(REAL_INDEX.exists(), "optional audit index fixture is unavailable")
    def test_real_index_prefixes(self):
        frame = pd.read_csv(REAL_INDEX, dtype={"trade_date": str}).sort_values("trade_date")
        close = frame.close.to_numpy(float)
        # Historical checkpoints plus every decision date in the portfolio comparison.
        dates = frame.trade_date.astype(str).str.replace("-", "", regex=False)
        recent = np.flatnonzero(dates.between("20260728", "20261009").to_numpy()) + 1
        stops = sorted(set(range(50, len(close), 250)) | set(recent) | {len(close)})
        self.assert_prefixes_unchanged(close, stops)


class DelayTests(unittest.TestCase):
    def test_post_pivot_direction_is_opposite_the_incoming_move(self):
        pivots = {p["index"]: p for p in chan.zigzag_pivots(EXAMPLE, 0.05)}
        self.assertEqual(pivots[14]["direction_after"], -1)
        self.assertEqual(pivots[9]["direction_after"], 1)
        self.assertGreater(pivots[14]["confirm"], 14)

    def test_capture_rate_denominator_is_eligible_pivots(self):
        phase = np.zeros(10)
        phase[5] = -1
        pivots = [{"index": 1, "direction_after": 1},
                  {"index": 3, "direction_after": -1},
                  {"index": 8, "direction_after": 1}]
        result = chan.pivot_delay_metrics(phase, pivots, min_history=2, max_lag=4)
        self.assertEqual(result["可评估拐点"], 1)
        self.assertEqual(result["已捕获"], 1)
        self.assertEqual(result["捕获率%"], 100.0)
        self.assertEqual(result["中位"], 2.0)

    def test_no_captures_or_eligible_pivots(self):
        pivots = [{"index": 3, "direction_after": -1}]
        result = chan.pivot_delay_metrics(np.ones(10), pivots, min_history=2, max_lag=4)
        self.assertEqual(result["捕获率%"], 0.0)
        self.assertIsNone(result["中位"])
        result = chan.pivot_delay_metrics(np.ones(10), pivots, min_history=200)
        self.assertEqual(result["可评估拐点"], 0)
        self.assertIsNone(result["捕获率%"])


if __name__ == "__main__":
    unittest.main()
