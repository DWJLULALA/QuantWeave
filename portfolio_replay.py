"""Causal, self-financing daily replay for a persistent Top-K stock portfolio.

Prices and scores are date-by-security frames. A close-t score/phase can only
affect orders at the next market session's open. Scores may be sparse; the
price calendar controls valuation and daily phase checks. Existing positions
are retained until their observed rank exceeds ``exit_rank`` or the gate closes.
New positions come only from the current Top-K and fill at most K total slots.

Each batch buys equal cash notionals, capped at opening NAV/K per name; limited
cash is shared equally across that batch, including fees. Existing shares are
never rebalanced. Missing quotes cannot execute an order: held shares retain
their last observable mark, and sell orders retry at subsequent opens. Missing
scores do not trigger rank exits or new buys. Missing phase is conservatively
gate-closed for gated modes (and is reported); ``off`` ignores phase entirely.

The final calendar session allows exits but no new entries, then liquidates at
its actual close. A missing final close leaves an explicitly unliquidated
position, still marked at its last available quote, never a fabricated fill.
Fractional shares are a research convention. No volume/limit-up/down, lot-size,
dividend or corporate-action modelling is performed: provide consistently
adjusted OHLC data and check those constraints before interpreting live fills.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


@dataclass
class ReplayResult:
    daily: pd.DataFrame
    trades: pd.DataFrame
    fills: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict
    unliquidated: list[dict]


def _day(value) -> str:
    """Canonical date keys, accepting strings, date objects and timestamps."""
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return pd.Timestamp(f"{text[:4]}-{text[4:6]}-{text[6:8]}").strftime("%Y%m%d")
    return pd.Timestamp(value).strftime("%Y%m%d")


def _frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a date-by-security DataFrame")
    result = frame.copy()
    result.index = [_day(x) for x in result.index]
    result.columns = result.columns.map(str)
    if result.index.has_duplicates or result.columns.has_duplicates:
        raise ValueError(f"{name} has duplicate dates or security codes")
    return result.apply(pd.to_numeric, errors="coerce").sort_index()


def _mapping(values) -> dict:
    if values is None:
        return {}
    result = {}
    for key, value in values.items():
        day = _day(key)
        if day in result:
            raise ValueError("duplicate normalised gate/phase date")
        result[day] = value
    return result


def _positive(value) -> bool:
    return bool(np.isfinite(value) and value > 0)


def replay_portfolio(
    open_prices: pd.DataFrame,
    close_prices: pd.DataFrame,
    scores: pd.DataFrame,
    chan_phase: pd.Series | Mapping | None = None,
    *,
    top_k: int = 100,
    exit_rank: int = 1000,
    mode: str = "off",
    initial_cash: float = 1_000_000.0,
    fee_bps: float = 0.0,
    gate_by_day: pd.Series | Mapping | None = None,
) -> ReplayResult:
    """Replay one mode; return account NAV plus separately labelled trade stats.

    ``mode`` is ``off``, ``non_down`` (phase != -1), or ``up_only`` (phase == 1).
    Phase values must be -1, 0, 1 or missing. ``gate_by_day`` optionally supplies
    an explicit boolean/0-or-1 close-time gate instead, e.g. for a wave model.
    It overrides mode, including ``off``; missing dates are gate-closed.

    ``metrics['max_drawdown']`` is a nonnegative loss magnitude and includes
    initial capital in the running high-water mark. ``gross_turnover`` means
    sum of buy plus sell filled notionals / initial capital (not annualised).
    ``trade_count`` counts fills, ``trades``/``roundtrip_count`` closed positions.
    Portfolio returns ALWAYS come from cash plus marked fixed share quantities.
    """
    if mode not in {"off", "non_down", "up_only"}:
        raise ValueError("mode must be off, non_down, or up_only")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(exit_rank, int) or isinstance(exit_rank, bool) or exit_rank < top_k:
        raise ValueError("exit_rank must be an integer >= top_k")
    if not np.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial_cash must be positive and finite")
    if not np.isfinite(fee_bps) or not 0 <= fee_bps < 10000:
        raise ValueError("fee_bps must be in [0, 10000)")
    opens = _frame(open_prices, "open_prices")
    closes = _frame(close_prices, "close_prices")
    score_frame = _frame(scores, "scores")
    dates = sorted(set(opens.index) | set(closes.index))
    if not dates:
        raise ValueError("price calendar is empty")
    codes = sorted(set(opens.columns) | set(closes.columns) | set(score_frame.columns))
    if not codes:
        raise ValueError("security universe is empty")
    opens = opens.reindex(index=dates, columns=codes)
    closes = closes.reindex(index=dates, columns=codes)
    score_frame = score_frame.reindex(columns=codes)
    open_values, close_values = opens.to_numpy(float), closes.to_numpy(float)
    score_map = {day: row.to_numpy(float) for day, row in score_frame.iterrows()}
    phase_map, explicit_gates = _mapping(chan_phase), _mapping(gate_by_day)
    positions = {}
    pending_sells = {}
    pending_buys = []
    last_marks = np.full(len(codes), np.nan)
    cash = float(initial_cash)
    fee_rate = fee_bps / 10000.0
    daily_rows, trade_rows, fill_rows, order_rows = [], [], [], []
    previous_equity = high_water = float(initial_cash)
    missing_gate_days = 0

    def gate_at(day):
        nonlocal missing_gate_days
        if gate_by_day is not None:
            value = explicit_gates.get(day)
            if value is None or pd.isna(value):
                missing_gate_days += 1
                return False
            if value not in (True, False, 0, 1):
                raise ValueError(f"invalid gate on {day}: {value!r}")
            return bool(value)
        if mode == "off":
            return True
        phase = phase_map.get(day)
        if phase is None or pd.isna(phase):
            missing_gate_days += 1
            return False
        if phase not in (-1, 0, 1):
            raise ValueError(f"invalid phase on {day}: {phase!r}")
        return phase != -1 if mode == "non_down" else phase == 1

    def market_value():
        return float(sum(p["shares"] * last_marks[j] for j, p in positions.items()))

    def sell(j, price, day, signal_day, reason, session):
        nonlocal cash
        position = positions.pop(j)
        notional = position["shares"] * price
        fee = notional * fee_rate
        proceeds = notional - fee
        cash += proceeds
        pnl = proceeds - position["entry_cost"]
        fill_rows.append({"date": day, "order_date": signal_day,
                          "code": codes[j], "side": "sell", "session": session,
                          "price": price, "shares": position["shares"],
                          "notional": notional, "fee": fee, "reason": reason})
        trade_rows.append({
            "code": codes[j], "entry_order_date": position["entry_order_date"],
            "entry": position["entry"], "exit_order_date": signal_day,
            "exit": day, "entry_price": position["entry_price"], "exit_price": price,
            "shares": position["shares"], "entry_notional": position["entry_notional"],
            "exit_notional": notional, "entry_fee": position["entry_fee"],
            "exit_fee": fee, "fees": position["entry_fee"] + fee,
            "pnl": pnl, "ret": pnl / position["entry_cost"],
            "gross_ret": price / position["entry_price"] - 1,
            "reason": reason, "forced": reason == "final", "exit_session": session,
        })

    for i, day in enumerate(dates):
        op, cl = open_values[i], close_values[i]
        valid_open = np.isfinite(op) & (op > 0)
        last_marks[valid_open] = op[valid_open]
        fills_before = len(fill_rows)
        # Orders originate strictly from an earlier close. Failed sales persist.
        for j, order in sorted(list(pending_sells.items())):
            if j not in positions:
                del pending_sells[j]
            elif valid_open[j]:
                sell(j, float(op[j]), day, order["order_date"], order["reason"], "open")
                del pending_sells[j]
            else:
                order_rows.append({"date": day, "order_date": order["order_date"],
                                   "code": codes[j], "side": "sell",
                                   "status": "pending_missing_open", "reason": order["reason"]})
        # The last day is liquidation-only, avoiding terminal round-trip churn.
        if i < len(dates) - 1:
            slots = max(0, top_k - len(positions))
            selected = []
            for j, signal_day in pending_buys:
                if j in positions:
                    continue
                if not valid_open[j]:
                    order_rows.append({"date": day, "order_date": signal_day,
                                       "code": codes[j], "side": "buy",
                                       "status": "cancelled_missing_open", "reason": "top_k"})
                elif len(selected) < slots:
                    selected.append((j, signal_day))
            if selected and cash > initial_cash * 1e-14:
                opening_nav = cash + market_value()
                amount = min(opening_nav / top_k, cash / (len(selected) * (1 + fee_rate)))
                for j, signal_day in selected:
                    fee = amount * fee_rate
                    shares = amount / float(op[j])
                    positions[j] = {
                        "shares": shares, "entry": day, "entry_order_date": signal_day,
                        "entry_price": float(op[j]), "entry_notional": amount,
                        "entry_fee": fee, "entry_cost": amount + fee,
                    }
                    cash -= amount + fee
                    fill_rows.append({"date": day, "order_date": signal_day,
                                      "code": codes[j], "side": "buy", "session": "open",
                                      "price": float(op[j]), "shares": shares,
                                      "notional": amount, "fee": fee, "reason": "top_k"})
                if cash < -initial_cash * 1e-10:
                    raise AssertionError("self-financing invariant violated: negative cash")
                cash = max(0.0, cash)  # Clamp floating-point subtraction noise only.
        pending_buys = []
        valid_close = np.isfinite(cl) & (cl > 0)
        last_marks[valid_close] = cl[valid_close]
        value_before_final = market_value()
        equity_before_final = cash + value_before_final
        n_before_final = len(positions)
        if i == len(dates) - 1:
            for j in sorted(list(positions)):
                if valid_close[j]:
                    sell(j, float(cl[j]), day, day, "final", "close")
                    pending_sells.pop(j, None)
                else:
                    order_rows.append({"date": day, "order_date": day, "code": codes[j],
                                       "side": "sell", "status": "unliquidated_missing_close",
                                       "reason": "final"})
        gate_open = gate_at(day)
        score = score_map.get(day)
        ranks = None
        if score is not None:
            finite = np.isfinite(score)
            order = np.argsort(-np.where(finite, score, -np.inf), kind="stable")
            order = order[:int(finite.sum())]
            ranks = np.full(len(codes), np.nan)
            ranks[order] = np.arange(1, len(order) + 1)
        # Build tomorrow's orders using only today's state. Missing rankings keep
        # held names; a previously triggered sell remains outstanding until filled.
        if i < len(dates) - 1:
            for j in sorted(positions):
                reason = "gate" if not gate_open else (
                    "rank" if ranks is not None and np.isfinite(ranks[j]) and ranks[j] > exit_rank else None)
                if reason and j not in pending_sells:
                    pending_sells[j] = {"order_date": day, "reason": reason}
            if gate_open and ranks is not None:
                # Never scan below Top-K to replace an already-held candidate.
                pending_buys = [(int(j), day) for j in order[:top_k] if j not in positions]
        market_val = market_value()
        equity = cash + market_val
        if len(positions) > top_k or cash < 0:
            raise AssertionError("portfolio capacity/cash invariant violated")
        high_water = max(high_water, equity)
        todays_fills = fill_rows[fills_before:]
        daily_rows.append({
            "date": day, "equity": equity, "cash": cash, "market_value": market_val,
            "nav": equity / initial_cash, "port_ret": equity / previous_equity - 1,
            "cum_return": equity / initial_cash - 1,
            "drawdown": equity / high_water - 1, "n_held": len(positions),
            "exposure": market_val / equity if equity > 0 else 0.0,
            "n_held_before_final": n_before_final,
            "exposure_before_final": value_before_final / equity_before_final if equity_before_final > 0 else 0.0,
            "stale_mark_count": int(sum(not valid_close[j] for j in positions)),
            "has_score": score is not None and bool(np.isfinite(score).any()),
            "gate_open": gate_open,
            "buy_notional": sum(f["notional"] for f in todays_fills if f["side"] == "buy"),
            "sell_notional": sum(f["notional"] for f in todays_fills if f["side"] == "sell"),
            "fees": sum(f["fee"] for f in todays_fills), "fill_count": len(todays_fills),
        })
        previous_equity = equity

    unliquidated = [{"code": codes[j], "shares": p["shares"], "entry": p["entry"],
                    "last_mark": float(last_marks[j]), "market_value": p["shares"] * last_marks[j],
                    "reason": "missing_final_close"} for j, p in sorted(positions.items())]
    daily = pd.DataFrame(daily_rows)
    trades = pd.DataFrame(trade_rows, columns=[
        "code", "entry_order_date", "entry", "exit_order_date", "exit", "entry_price", "exit_price",
        "shares", "entry_notional", "exit_notional", "entry_fee", "exit_fee", "fees", "pnl", "ret",
        "gross_ret", "reason", "forced", "exit_session"])
    fills = pd.DataFrame(fill_rows, columns=["date", "order_date", "code", "side", "session", "price",
                                                "shares", "notional", "fee", "reason"])
    orders = pd.DataFrame(order_rows, columns=["date", "order_date", "code", "side", "status", "reason"])
    gross_turnover = float(daily["buy_notional"].sum() + daily["sell_notional"].sum()) / initial_cash
    metrics = {
        "mode": mode, "total_return": float(daily.iloc[-1]["nav"] - 1),
        "max_drawdown": float(-daily["drawdown"].min()),
        "max_drawdown_signed": float(daily["drawdown"].min()),
        "initial_cash": initial_cash, "final_equity": float(daily.iloc[-1]["equity"]),
        "final_cash": cash, "sessions": len(dates), "first_date": dates[0], "last_date": dates[-1],
        "trades": len(trades), "roundtrip_count": len(trades), "trade_count": len(fills),
        "buy_count": int((fills["side"] == "buy").sum()),
        "sell_count": int((fills["side"] == "sell").sum()),
        "win_rate": float((trades["ret"] > 0).mean()) if len(trades) else None,
        "mean_return": float(trades["ret"].mean()) if len(trades) else None,
        "forced_closed": int(trades["forced"].sum()) if len(trades) else 0,
        "gate_exits": int((trades["reason"] == "gate").sum()),
        "rank_exits": int((trades["reason"] == "rank").sum()),
        "gross_turnover": gross_turnover, "turnover": gross_turnover,
        "one_way_turnover": gross_turnover / 2,
        "total_fees": float(daily["fees"].sum()), "fee_bps": fee_bps,
        "mean_exposure": float(daily["exposure"].mean()),
        "mean_exposure_before_final": float(daily["exposure_before_final"].mean()),
        "mean_n_held": float(daily["n_held"].mean()),
        "max_n_held": int(daily["n_held_before_final"].max()),
        "invested_days": int((daily["n_held_before_final"] > 0).sum()),
        "missing_gate_days": missing_gate_days,
        "stale_mark_days": int((daily["stale_mark_count"] > 0).sum()),
        "unliquidated_count": len(unliquidated), "fully_liquidated": not unliquidated,
        "top_k": top_k, "exit_rank": exit_rank,
        "fractional_shares": True, "execution": "close_signal_next_open_final_close",
    }
    return ReplayResult(daily=daily, trades=trades, fills=fills, orders=orders,
                        metrics=metrics, unliquidated=unliquidated)
