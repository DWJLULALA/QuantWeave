"""Causality and structure tests for chan_core."""
import unittest

import numpy as np
import pandas as pd

import chan_core as core


def synthetic_bars(n=600, seed=7, freq="D"):
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    return pd.DataFrame({
        "dt": pd.date_range("2020-01-01", periods=n, freq=freq),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(1000, 5000, n).astype(float),
    })


class CausalityTests(unittest.TestCase):
    def test_prefix_states_never_repaint(self):
        bars = synthetic_bars(420)
        full = core.ChanEngine(bars).run()
        columns = ["phase", "trend_score", "pivot_zg", "pivot_zd", "pivot_pos",
                   "pivot_age", "pivot_strokes", "stroke_count", "div",
                   "last_dir", "last_stroke_ret", "bars_since_fractal"]
        for stop in (40, 80, 150, 260, 419):
            prefix = core.ChanEngine(bars.iloc[:stop + 1]).run()
            self.assertEqual(len(prefix), stop + 1)
            for col in columns:
                np.testing.assert_allclose(
                    prefix[col].to_numpy(float),
                    full[col].iloc[:stop + 1].to_numpy(float),
                    equal_nan=True, rtol=0, atol=1e-9,
                    err_msg="%s repainted at prefix %d" % (col, stop),
                )

    def test_last_endpoint_replacement_does_not_rewrite_past(self):
        bars = synthetic_bars(300, seed=11)
        full = core.ChanEngine(bars).run()
        # Every prefix must match the full run on the prefix.  This is the
        # same guard as above, but focused on a shorter series where endpoint
        # replacement is frequent.
        for stop in range(30, 300, 17):
            prefix = core.ChanEngine(bars.iloc[:stop + 1]).run()
            np.testing.assert_allclose(prefix["phase"].to_numpy(float),
                                       full["phase"].iloc[:stop + 1].to_numpy(float),
                                       equal_nan=True)


class StructureTests(unittest.TestCase):
    def test_fractal_is_confirmed_only_after_right_bar_closes(self):
        # A clean top at bar 2.  The merged bar 3 is open until bar 4 proves
        # it is not included by bar 4, so the fractal cannot be known at bar 3.
        prices = [10, 11, 13, 12, 11, 10, 9]
        bars = pd.DataFrame({
            "dt": pd.date_range("2024-01-01", periods=len(prices), freq="D"),
            "open": prices, "high": np.array(prices) + 0.1,
            "low": np.array(prices) - 0.1, "close": prices,
            "volume": 1.0,
        })
        engine = core.ChanEngine(bars, min_gap=1)
        engine.run()
        self.assertTrue(engine.fractals)
        top = [f for f in engine.fractals if f.kind > 0]
        self.assertTrue(top)
        self.assertGreaterEqual(top[0].avail_idx, 3)

    def test_three_overlapping_strokes_form_a_pivot(self):
        strokes = [
            core.Stroke(0, 4, 10, 12, 1, 0, 4, 5, pd.Timestamp("2024-01-01"),
                        pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-06")),
            core.Stroke(4, 8, 12, 9, -1, 4, 8, 9, pd.Timestamp("2024-01-05"),
                        pd.Timestamp("2024-01-09"), pd.Timestamp("2024-01-10")),
            core.Stroke(8, 12, 9, 11.5, 1, 8, 12, 13, pd.Timestamp("2024-01-09"),
                        pd.Timestamp("2024-01-13"), pd.Timestamp("2024-01-14")),
        ]
        pivot = core.ChanEngine._make_pivot(strokes, 2)
        self.assertIsNotNone(pivot)
        self.assertAlmostEqual(pivot.zd, 10.0)
        self.assertAlmostEqual(pivot.zg, 11.5)

    def test_non_overlapping_strokes_have_no_pivot(self):
        strokes = [
            core.Stroke(0, 4, 10, 12, 1, 0, 4, 5, pd.Timestamp("2024-01-01"),
                        pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-06")),
            core.Stroke(4, 8, 12, 13, 1, 4, 8, 9, pd.Timestamp("2024-01-05"),
                        pd.Timestamp("2024-01-09"), pd.Timestamp("2024-01-10")),
            core.Stroke(8, 12, 13, 15, 1, 8, 12, 13, pd.Timestamp("2024-01-09"),
                        pd.Timestamp("2024-01-13"), pd.Timestamp("2024-01-14")),
        ]
        self.assertIsNone(core.ChanEngine._make_pivot(strokes, 2))

    def test_engine_finds_pivots_on_a_random_walk(self):
        bars = synthetic_bars(600)
        engine = core.ChanEngine(bars)
        states = engine.run()
        self.assertGreater(len(engine.strokes), 10)
        self.assertTrue(engine.pivots or engine.active_pivot)
        self.assertTrue(states["pivot_zg"].notna().any())
        valid = states["pivot_zg"].notna()
        self.assertTrue((states.loc[valid, "pivot_zg"] >
                         states.loc[valid, "pivot_zd"]).all())

    def test_phase_matches_position_relative_to_active_pivot(self):
        bars = synthetic_bars(500, seed=13)
        engine = core.ChanEngine(bars)
        states = engine.run()
        active = states["pivot_zg"].notna()
        self.assertTrue(active.any())
        sub = states[active]
        expected = np.where(sub["close"] > sub["pivot_zg"], 1,
                            np.where(sub["close"] < sub["pivot_zd"], -1, 0))
        np.testing.assert_array_equal(sub["phase"].to_numpy(int), expected)


class ResampleTests(unittest.TestCase):
    def test_30min_keeps_lunch_break_separate(self):
        rows = []
        for day in ("2026-10-12", "2026-10-13"):
            base = pd.Timestamp(day)
            for minutes in ([9 * 60 + 35 + i * 5 for i in range(24)] +
                            [13 * 60 + 5 + i * 5 for i in range(24)]):
                dt = base + pd.Timedelta(minutes=minutes)
                rows.append({"dt": dt, "open": 10.0, "high": 10.1, "low": 9.9,
                             "close": 10.0, "volume": 1.0})
        five = pd.DataFrame(rows)
        thirty = core.resample_intraday(five, "30min")
        self.assertEqual(len(thirty), 16)  # 8 bars per day, two days
        self.assertEqual(thirty.iloc[0]["dt"], pd.Timestamp("2026-10-12 10:00"))
        self.assertEqual(thirty.iloc[3]["dt"], pd.Timestamp("2026-10-12 11:30"))
        self.assertEqual(thirty.iloc[4]["dt"], pd.Timestamp("2026-10-12 13:30"))
        self.assertEqual(thirty.iloc[-1]["dt"], pd.Timestamp("2026-10-13 15:00"))

    def test_weekly_bar_uses_last_trading_day(self):
        dates = pd.bdate_range("2026-10-05", periods=10)
        daily = pd.DataFrame({"dt": dates, "open": 10.0, "high": 11.0,
                              "low": 9.0, "close": 10.5, "volume": 1.0})
        weekly = core.resample_period(daily, "W")
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly.iloc[0]["dt"], dates[4])
        self.assertEqual(weekly.iloc[1]["dt"], dates[9])

    def test_monthly_bar_uses_last_trading_day(self):
        dates = pd.bdate_range("2026-01-01", periods=45)
        daily = pd.DataFrame({"dt": dates, "open": 10.0, "high": 11.0,
                              "low": 9.0, "close": 10.5, "volume": 1.0})
        monthly = core.resample_period(daily, "M")
        self.assertEqual(len(monthly), 3)
        self.assertEqual(monthly.iloc[0]["dt"].month, 1)
        self.assertEqual(monthly.iloc[0]["dt"], dates[dates.month == 1][-1])


class InputTests(unittest.TestCase):
    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            core.ChanEngine(pd.DataFrame({"dt": [pd.Timestamp("2024-01-01")],
                                          "high": [1], "low": [1]}))
        bad = synthetic_bars(10)
        bad.loc[0, "close"] = np.nan
        with self.assertRaises(ValueError):
            core.ChanEngine(bad)
        bad = synthetic_bars(10)
        bad.loc[0, "high"] = bad.loc[0, "low"] - 1
        with self.assertRaises(ValueError):
            core.ChanEngine(bad)


if __name__ == "__main__":
    unittest.main()
