"""Account-level regression tests for the causal Top-K replay."""
import unittest

import numpy as np
import pandas as pd

from portfolio_replay import replay_portfolio


class PortfolioReplayTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range("2026-08-03", periods=5)
        self.keys = self.dates.strftime("%Y%m%d").tolist()

    def prices(self, values, codes=("A", "B", "C")):
        array = np.asarray(values, dtype=float)
        if array.ndim == 1:
            array = np.repeat(array[:, None], len(codes), axis=1)
        return pd.DataFrame(array, index=self.dates[:len(array)], columns=codes)

    def scores(self, values=(3, 2, 1)):
        return self.prices([values] * len(self.dates))

    def test_stable_top_one_does_not_accumulate_lower_ranked_names(self):
        prices = self.prices([100, 110, 121, 133.1, 146.41])
        r = replay_portfolio(prices, prices, self.scores(), top_k=1, exit_rank=3)
        self.assertEqual(r.daily.n_held.tolist(), [0, 1, 1, 1, 0])
        self.assertEqual(r.trades.code.tolist(), ["A"])
        self.assertEqual(r.metrics["buy_count"], 1)
        self.assertAlmostEqual(r.metrics["total_return"], 146.41 / 110 - 1)

    def test_self_financing_equal_entries_and_fixed_shares(self):
        op = self.prices([[100, 200, 300]] * 5)
        cl = op.copy()
        cl.iloc[2:, :] = [[130, 180, 300]] * 3
        r = replay_portfolio(op, cl, self.scores(), top_k=2, exit_rank=3,
                             initial_cash=1000, fee_bps=10)
        buys = r.fills[r.fills.side == "buy"]
        self.assertEqual(len(buys), 2)
        self.assertAlmostEqual(buys.iloc[0].notional, buys.iloc[1].notional)
        self.assertTrue((r.daily.cash >= 0).all())
        self.assertTrue((r.daily.n_held <= 2).all())
        np.testing.assert_allclose(r.daily.equity, r.daily.cash + r.daily.market_value)
        for _, trade in r.trades.iterrows():
            buy = buys[buys.code == trade.code].iloc[0]
            self.assertAlmostEqual(trade.shares, buy.shares)
        self.assertAlmostEqual(r.metrics["final_cash"] - 1000, r.trades.pnl.sum())

    def test_decisions_fill_at_next_open_not_signal_close(self):
        op = self.prices([100, 200, 200, 200, 200])
        cl = self.prices([100, 210, 220, 230, 240])
        r = replay_portfolio(op, cl, self.scores(), top_k=1, exit_rank=3)
        buy = r.fills[r.fills.side == "buy"].iloc[0]
        self.assertEqual(buy.order_date, self.keys[0])
        self.assertEqual(buy.date, self.keys[1])
        self.assertEqual(buy.price, 200)
        self.assertAlmostEqual(r.metrics["total_return"], 0.2)
        self.assertEqual(r.daily.iloc[0].nav, 1.0)

    def test_gate_changes_exit_without_a_score_on_that_day(self):
        px = self.prices([100] * 5)
        sparse_scores = self.scores().iloc[[0]]
        phase = pd.Series([1, -1, -1, -1, -1], index=self.dates)
        r = replay_portfolio(px, px, sparse_scores, phase, top_k=1, exit_rank=3, mode="non_down")
        self.assertEqual(len(r.daily), 5)
        self.assertEqual(r.trades.iloc[0].exit_order_date, self.keys[1])
        self.assertEqual(r.trades.iloc[0].exit, self.keys[2])
        self.assertEqual(r.trades.iloc[0].reason, "gate")
        self.assertEqual(r.daily.n_held.tolist(), [0, 1, 0, 0, 0])

    def test_missing_score_neither_rebuys_nor_uses_old_rank(self):
        px = self.prices([100] * 5)
        sparse = self.scores().iloc[[0]]
        r = replay_portfolio(px, px, sparse, top_k=2, exit_rank=3)
        self.assertEqual(r.metrics["buy_count"], 2)
        self.assertEqual(r.metrics["rank_exits"], 0)
        self.assertEqual(r.metrics["forced_closed"], 2)

    def test_rank_exit_and_replacement_only_after_threshold_crosses(self):
        px = self.prices([100] * 5)
        scores = self.scores()
        scores.iloc[1] = [2, 3, 1]  # A rank 2 stays held although B is now Top1.
        scores.iloc[2:] = [[1, 3, 2]] * 3
        r = replay_portfolio(px, px, scores, top_k=1, exit_rank=2)
        self.assertEqual(r.fills[r.fills.side == "buy"].code.tolist(), ["A", "B"])
        a = r.trades[r.trades.code == "A"].iloc[0]
        self.assertEqual(a.exit_order_date, self.keys[2])
        self.assertEqual(a.exit, self.keys[3])
        self.assertEqual(a.reason, "rank")
        self.assertEqual(r.metrics["max_n_held"], 1)

    def test_missing_rank_retains_position(self):
        px = self.prices([100] * 5)
        scores = self.scores()
        scores.iloc[1:, 0] = np.nan
        r = replay_portfolio(px, px, scores, top_k=1, exit_rank=2)
        self.assertEqual(r.trades.code.tolist(), ["A"])
        self.assertEqual(r.trades.reason.tolist(), ["final"])

    def test_missing_open_keeps_sell_pending_without_deleting_holding(self):
        op = self.prices([100] * 5)
        cl = op.copy()
        op.iloc[2, 0] = np.nan
        cl.iloc[2, 0] = np.nan
        phase = pd.Series([1, -1, 1, 1, 1], index=self.dates)
        r = replay_portfolio(op, cl, self.scores().iloc[[0]], phase,
                             top_k=1, exit_rank=3, mode="up_only")
        self.assertEqual(r.daily.n_held.tolist(), [0, 1, 1, 0, 0])
        self.assertEqual(r.trades.iloc[0].exit, self.keys[3])
        self.assertEqual(r.trades.iloc[0].exit_order_date, self.keys[1])
        self.assertEqual(r.daily.iloc[2].nav, 1.0)
        self.assertEqual(r.daily.iloc[2].stale_mark_count, 1)

    def test_missing_price_does_not_scan_below_top_k(self):
        op = self.prices([100] * 5)
        cl = op.copy()
        op.iloc[1:, 0] = np.nan
        r = replay_portfolio(op, cl, self.scores(), top_k=1, exit_rank=3)
        self.assertEqual(r.metrics["buy_count"], 0)
        self.assertEqual(r.metrics["total_return"], 0)

    def test_last_day_has_no_new_buys_and_forces_actual_close(self):
        op = self.prices([100] * 5)
        cl = op.copy()
        cl.iloc[-1] = [120, 100, 100]
        r = replay_portfolio(op, cl, self.scores(), top_k=1, exit_rank=3)
        self.assertTrue(r.metrics["fully_liquidated"])
        self.assertEqual(r.daily.iloc[-1].n_held, 0)
        self.assertEqual(r.trades.iloc[0].exit_price, 120)
        self.assertEqual(r.trades.iloc[0].exit_session, "close")
        only_last_signal = self.scores().iloc[[-2]]
        empty = replay_portfolio(op, cl, only_last_signal, top_k=1, exit_rank=3)
        self.assertEqual(empty.metrics["trade_count"], 0)

    def test_missing_final_close_is_explicit_unliquidated_position(self):
        op = self.prices([100] * 5)
        cl = op.copy()
        cl.iloc[-1, 0] = np.nan
        r = replay_portfolio(op, cl, self.scores(), top_k=1, exit_rank=3)
        self.assertFalse(r.metrics["fully_liquidated"])
        self.assertEqual(r.metrics["unliquidated_count"], 1)
        self.assertEqual(len(r.trades), 0)
        self.assertEqual(r.unliquidated[0]["code"], "A")
        self.assertEqual(r.daily.iloc[-1].market_value, 1_000_000)

    def test_returns_from_nav_not_product_of_simultaneous_trades(self):
        op = self.prices([100] * 5)
        cl = op.copy()
        cl.iloc[-1] = [110, 90, 100]
        r = replay_portfolio(op, cl, self.scores(), top_k=2, exit_rank=3)
        self.assertAlmostEqual(r.metrics["total_return"], 0)
        self.assertAlmostEqual(np.prod(1 + r.trades.ret) - 1, -0.01)
        self.assertAlmostEqual(np.prod(1 + r.daily.port_ret) - 1, r.metrics["total_return"])

    def test_fees_and_initial_capital_are_in_nav_drawdown(self):
        px = self.prices([100] * 5)
        r = replay_portfolio(px, px, self.scores(), top_k=1, exit_rank=3,
                             initial_cash=1000, fee_bps=10)
        expected = (1 - 0.001) / (1 + 0.001) - 1
        self.assertAlmostEqual(r.metrics["total_return"], expected)
        self.assertAlmostEqual(r.metrics["max_drawdown"], -expected)
        self.assertAlmostEqual(r.metrics["total_fees"], -expected * 1000)

    def test_three_gate_modes_have_distinct_semantics(self):
        px = self.prices([100] * 5)
        phase = pd.Series([0, 0, 0, 0, 0], index=self.dates)
        results = {mode: replay_portfolio(px, px, self.scores(), phase, top_k=1,
                                         exit_rank=3, mode=mode)
                   for mode in ("off", "non_down", "up_only")}
        self.assertEqual(results["off"].metrics["buy_count"], 1)
        self.assertEqual(results["non_down"].metrics["buy_count"], 1)
        self.assertEqual(results["up_only"].metrics["buy_count"], 0)

    def test_missing_gate_is_closed_not_silently_enabled(self):
        px = self.prices([100] * 5)
        phase = pd.Series([1], index=self.dates[:1])
        r = replay_portfolio(px, px, self.scores(), phase, top_k=1, exit_rank=3, mode="up_only")
        self.assertEqual(r.metrics["missing_gate_days"], 4)
        self.assertEqual(r.trades.iloc[0].reason, "gate")
        self.assertEqual(r.trades.iloc[0].exit, self.keys[2])

    def test_gate_override_for_wave_source(self):
        px = self.prices([100] * 5)
        gate = dict(zip(self.keys, [True, False, False, False, False]))
        r = replay_portfolio(px, px, self.scores(), top_k=1, exit_rank=3,
                             mode="off", gate_by_day=gate)
        self.assertEqual(r.trades.iloc[0].reason, "gate")
        self.assertEqual(r.trades.iloc[0].exit, self.keys[2])

    def test_future_scores_and_prices_do_not_change_past_path(self):
        op = self.prices([100, 101, 102, 103, 104])
        scores = self.scores()
        base = replay_portfolio(op, op, scores, top_k=2, exit_rank=3)
        changed_px, changed_scores = op.copy(), scores.copy()
        changed_px.iloc[-1] *= 10
        changed_scores.iloc[-1] = [100, -100, 0]
        changed = replay_portfolio(changed_px, changed_px, changed_scores, top_k=2, exit_rank=3)
        pd.testing.assert_frame_equal(base.daily.iloc[:-1], changed.daily.iloc[:-1])

    def test_rejects_duplicate_dates_and_invalid_configuration(self):
        px = self.prices([100] * 5)
        with self.assertRaises(ValueError):
            replay_portfolio(px, px, self.scores(), top_k=3, exit_rank=2)
        with self.assertRaises(ValueError):
            replay_portfolio(pd.concat([px, px.iloc[[0]]]), px, self.scores())


if __name__ == "__main__":
    unittest.main()
