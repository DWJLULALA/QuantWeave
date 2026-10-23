#!/usr/bin/env python3
"""Causal E1/E3/E5/E10 portfolio replay and controlled gate comparison.

This is a daily-bar research simulator, not an order execution service.
Scores are observed at the close; orders use the following session's open.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from chan_phase_engine import chan_phases, macd
from composite_factor import _norm
from portfolio_replay import replay_portfolio

MODES = {"off": "不门控", "non_down": "仅下跌清仓", "up_only": "仅上涨持仓"}
HORIZONS = (1, 3, 5, 10)
DAILY_REPAIRS_DIR = Path(__file__).resolve().parent / "data" / "daily_repairs"


def ymd(value):
    return pd.Timestamp(str(value)).strftime("%Y%m%d")


def pure_composite_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Equal percentile ranks, no SET10 and no temporal EMA.

    Require all four horizons, rather than silently filling missing components
    with a neutral score. Column order makes tied ranks reproducible by code.
    """
    f = frame.copy()
    required = ["signal_date", "ts_code", *[f"k_e{h}" for h in HORIZONS]]
    missing = sorted(set(required) - set(f.columns))
    if missing:
        raise ValueError(f"Missing curve columns: {missing}")
    f["signal_date"] = f.signal_date.astype(str).str.replace("-", "", regex=False)
    f["ts_code"] = f.ts_code.astype(str)
    if f.duplicated(["signal_date", "ts_code"]).any():
        raise ValueError("Duplicate signal-date / stock observations")
    f = f.sort_values(["signal_date", "ts_code"])
    cols = [f"k_e{h}" for h in HORIZONS]
    for c in cols:
        f[c] = pd.to_numeric(f[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
    parts = []
    for _, day in f.groupby("signal_date", sort=True):
        z = pd.concat([_norm(day[c]) for c in cols], axis=1)
        day = day.copy()
        day["score"] = z.mean(axis=1).where(day[cols].notna().all(axis=1))
        parts.append(day[["signal_date", "ts_code", "score"]])
    if not parts:
        raise ValueError("No scores in requested window")
    return pd.concat(parts).pivot(index="signal_date", columns="ts_code", values="score").sort_index(axis=1)


def read_frame(path, **kwargs):
    return pd.read_parquet(path, **kwargs) if Path(path).suffix == ".parquet" else pd.read_csv(path)


def apply_daily_repairs(panel, calendar, repairs_dir=DAILY_REPAIRS_DIR):
    """Fill whole absent market sessions only, without modifying the source panel.

    Repairs are immutable input supplements. Existing dates (even partially
    populated dates) always belong to the original panel. Multiple repair files
    claiming the same absent date are rejected instead of silently choosing one.
    """
    missing = set(calendar) - set(panel.date)
    if not missing or repairs_dir is None or not Path(repairs_dir).is_dir():
        return panel, []
    additions, audit, claimed = [], [], set()
    required = set(panel.columns) - {"date"}
    for path in sorted(Path(repairs_dir).glob("*.parquet")):
        repair = read_frame(path)
        if "timestamps" not in repair:
            raise ValueError(f"Daily repair missing timestamps: {path}")
        repair["date"] = pd.to_datetime(repair.timestamps).dt.strftime("%Y%m%d")
        repair = repair[repair.date.isin(missing)].copy()
        if repair.empty:
            continue
        absent_columns = sorted(required - set(repair.columns))
        if absent_columns:
            raise ValueError(f"Daily repair missing columns {absent_columns}: {path}")
        days = set(repair.date)
        overlap = claimed & days
        if overlap:
            raise ValueError(f"Multiple daily repair files for dates {sorted(overlap)}")
        if repair.duplicated(["date", "ts_code"]).any():
            raise ValueError(f"Duplicate daily repair price observations: {path}")
        prices = repair[["open", "close"]].apply(pd.to_numeric, errors="coerce")
        if not np.isfinite(prices.to_numpy()).all() or not prices.gt(0).all().all():
            raise ValueError(f"Daily repair has invalid open/close prices: {path}")
        claimed.update(days)
        additions.append(repair.reindex(columns=panel.columns))
        audit.append({"file": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "dates": sorted(days), "rows": int(len(repair))})
    if additions:
        panel = pd.concat([panel, *additions], ignore_index=True)
        panel = panel.sort_values(["date", "ts_code"]).reset_index(drop=True)
    return panel, audit


def prepare_inputs(panel_path, curves_path, index_path, start=None, end=None,
                   repairs_dir=DAILY_REPAIRS_DIR):
    curves = read_frame(curves_path)
    curves["signal_date"] = curves.signal_date.astype(str).str.replace("-", "", regex=False)
    start = start or curves.signal_date.min()
    end = end or curves.signal_date.max()
    start, end = ymd(start), ymd(end)
    curves = curves[(curves.signal_date >= start) & (curves.signal_date <= end)]
    scores = pure_composite_scores(curves)
    filters = [("timestamps", ">=", pd.Timestamp(start)), ("timestamps", "<", pd.Timestamp(end) + pd.Timedelta(days=1))]
    panel = read_frame(panel_path, filters=filters)
    panel["date"] = pd.to_datetime(panel.timestamps).dt.strftime("%Y%m%d")
    panel = panel[(panel.date >= start) & (panel.date <= end)].copy()
    if panel.duplicated(["date", "ts_code"]).any():
        raise ValueError("Duplicate daily price observations")
    index = read_frame(index_path)
    index["date"] = index.trade_date.astype(str).str.replace("-", "", regex=False).str[:8]
    index = index[index.date <= end].sort_values("date").drop_duplicates("date")
    calendar = index.loc[index.date >= start, "date"].tolist()
    if not calendar:
        raise ValueError("No index trading calendar in requested window")
    missing_before = sorted(set(calendar) - set(panel.date))
    panel, repairs = apply_daily_repairs(panel, calendar, repairs_dir)
    missing_after = sorted(set(calendar) - set(panel.date))
    if missing_after:
        raise ValueError(f"Panel missing entire index trading sessions: {missing_after}; no applicable daily repair")
    dif, _, _ = macd(index.close)
    phase, div, _, _ = chan_phases(index.close.to_numpy(float), dif.to_numpy(float))
    phases = pd.Series(phase, index=index.date, name="phase").reindex(calendar)
    opens = panel.pivot(index="date", columns="ts_code", values="open").reindex(index=calendar, columns=scores.columns)
    closes = panel.pivot(index="date", columns="ts_code", values="close").reindex_like(opens)
    if "volume" in panel:
        volume = panel.pivot(index="date", columns="ts_code", values="volume").reindex_like(opens)
        # Zero/missing volume is not an executable daily bar. Valuation still
        # uses the close or last observed close in the accounting engine.
        opens = opens.where(volume > 0)
        closes = closes.where(volume > 0)
    benchmark = index.set_index("date").close.reindex(calendar)
    meta = {"start": calendar[0], "end": calendar[-1], "market_sessions": len(calendar),
            "signal_sessions": len(scores.index.intersection(calendar)),
            "missing_signal_dates": sorted(set(calendar) - set(scores.index)),
            "universe": len(scores.columns), "score": "mean(percentile(E1,E3,E5,E10)*2-1); all four required",
            "panel_source": str(panel_path), "curves_source": str(curves_path), "index_source": str(index_path),
            "missing_panel_dates_before_repair": missing_before,
            "daily_repairs_dir": str(Path(repairs_dir).resolve()) if repairs_dir is not None else None,
            "daily_repairs": repairs}
    return opens, closes, scores, phases, benchmark, meta


def attach_benchmark(result, benchmark, phases):
    daily = result.daily.copy()
    dates = daily["date"].astype(str)
    b = benchmark.reindex(dates).to_numpy(float)
    daily["bench_ret"] = pd.Series(b).pct_change(fill_method=None).fillna(0).to_numpy()
    daily["cum_excess"] = daily.nav.to_numpy() / (b / b[0]) - 1
    daily["signal_date"] = dates
    daily["chan_phase"] = phases.reindex(dates).to_numpy()
    daily["gate_exp"] = daily.exposure
    daily["cum_return"] = daily.nav - 1
    result.daily = daily
    result.metrics["benchmark_return"] = float(b[-1] / b[0] - 1)
    result.metrics["relative_return"] = float(daily.cum_excess.iloc[-1])
    return result


def save_result(result, directory, mode, fee_bps, meta, top_k, exit_rank, initial_cash):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result.daily.to_csv(directory / "factor_daily.csv", index=False)
    result.trades.to_csv(directory / "factor_trades_comp.csv", index=False)
    result.fills.to_csv(directory / "factor_fills.csv", index=False)
    result.orders.to_csv(directory / "unfilled_orders.csv", index=False)
    metrics = dict(result.metrics)
    # Compatibility names all refer to the account, never product(trade PnL).
    metrics.setdefault("trades", metrics.get("trade_count", len(result.trades)))
    metrics.setdefault("mean_return", float(result.trades.ret.mean()) if len(result.trades) else None)
    metrics.setdefault("mean_excess", None)
    metrics["factor"] = "E1+E3+E5+E10"
    metrics["sessions"] = meta["market_sessions"]
    payload = {"available": True, "kind": "factor_evaluation", "engine_version": "causal_cash_book_v2",
               "kronos_source": "composite", "score_source": "pure_e1e3e5e10", "gate_source": "chan" if mode != "off" else "off",
               "gate_mode": mode, "gate_applied": mode != "off", "gate_alpha": None,
               "requested_signal_date": meta["end"], "signals_evaluated": meta["signal_sessions"],
               "signals_charted": meta["market_sessions"], "effective_parameters": {
                   "kronos_source": "composite", "kronos_weight": 1.0, "set10_weight": 0.0,
                   "kronos_ema": 0.0, "top_k": top_k, "exit_rank": exit_rank,
                   "gate_mode": mode, "fee_bps": fee_bps, "initial_cash": initial_cash},
               "exit_rank": exit_rank, "benchmark": "沪深300收盘；相同起止日期",
               "label_convention": "收盘形成信号，下一交易日开盘成交；每日现金+股数估值",
               "strategy": "最多TopK席位；仅从当日前TopK补空位；旧仓不每日再平衡；门控关闭/排名跌出阈值次日开盘退出；末日收盘强平",
               "book_summary": {"comp": metrics}, "metrics": [metrics], "daily_curve": str(directory / "factor_daily.csv"),
               "input_audit": meta, "unliquidated": result.unliquidated,
               "limitations": ["日线研究撮合，允许碎股；未建模涨跌停队列、整手、冲击或容量", "价格沿用现有面板；未另行核验复权与公司行动", "fee_bps为假设单边综合成本，并非逐项法定费率", "缺因子日不沿用旧排名开仓；门控和估值仍逐交易日运行"],
               "read_only": True}
    (directory / "factor_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    pd.DataFrame([metrics]).to_csv(directory / "factor_performance.csv", index=False)
    return payload


def evaluate(panel_path, curves_path, index_path, output_dir, *, start=None, end=None,
             modes=("off", "non_down", "up_only"), fees=(0.0, 10.0), top_k=100,
             exit_rank=1000, initial_cash=1_000_000.0, single=False, repairs_dir=DAILY_REPAIRS_DIR):
    op, cl, scores, phases, bench, meta = prepare_inputs(panel_path, curves_path, index_path, start, end, repairs_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scores.to_csv(output_dir / "pure_scores.csv")
    phases.to_csv(output_dir / "causal_chan_phase.csv", index_label="date")
    rows, payloads = [], []
    for fee in fees:
        for mode in modes:
            result = replay_portfolio(op, cl, scores, phases, top_k=top_k, exit_rank=exit_rank,
                                      mode=mode, initial_cash=initial_cash, fee_bps=fee)
            result = attach_benchmark(result, bench, phases)
            dest = output_dir if single else output_dir / f"{mode}_fee{fee:g}bps"
            payload = save_result(result, dest, mode, fee, meta, top_k, exit_rank, initial_cash)
            payloads.append(payload)
            rows.append({"mode": mode, "label": MODES[mode], "fee_bps": fee, **result.metrics})
    pd.DataFrame(rows).to_csv(output_dir / "comparison.csv", index=False)
    summary = {"input_audit": meta, "results": rows,
               "source_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                 for name in ["composite_replay.py", "portfolio_replay.py", "chan_phase_engine.py"]}}
    (output_dir / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return payloads[0] if single else summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--curves", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--exit-rank", type=int, default=1000)
    ap.add_argument("--modes", nargs="+", choices=list(MODES), default=list(MODES))
    ap.add_argument("--fees", nargs="+", type=float, default=[0.0, 10.0])
    ap.add_argument("--daily-repairs-dir", type=Path, default=DAILY_REPAIRS_DIR,
                    help="Whole-session price supplements; existing panel dates are never overwritten")
    args = ap.parse_args()
    result = evaluate(args.panel, args.curves, args.index, args.output_dir, start=args.start, end=args.end,
                      top_k=args.top_k, exit_rank=args.exit_rank, modes=args.modes, fees=args.fees,
                      repairs_dir=args.daily_repairs_dir)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
