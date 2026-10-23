#!/usr/bin/env python3
"""Causal factor evaluation for the QuantWeave.

The default composite source runs a pure E1/E3/E5/E10 cash-and-share account
from the first available frozen score through --date, without SET10 or EMA.
Signals execute next open, with a TopK position cap and final-close exits.
There is no training, GPU use, or order submission.

--kronos-source daily keeps the legacy factor diagnostics: the frozen
rp10 daily-shadow curve supplies the Kronos factor and labels are next-open to
holding-days-open.  --kronos-source intraday swaps the Kronos factor for the
intraday model's 15:00 cross-section (published live runs, newest per signal
day, falling back to the frozen OOS file for older days) and labels every
source with the pipeline's own actual_next_return identity: signal-day 15:00
close to the next session's 10:00 close.  SET10 and the composite are ranked
against the same labels so the comparison stays internally consistent.
"""
from __future__ import annotations
import argparse, csv, json, math, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(os.getenv("STOCKLAB_PROJECT_ROOT", "/opt/kronos"))
sys.path.insert(0, str(ROOT))


def normal_score(x: np.ndarray, minimum: int = 100) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=np.float64); ok = np.isfinite(x)
    if int(ok.sum()) < minimum: return out
    r = rankdata(x[ok], method="average"); p = (r - .5) / len(r)
    from scipy.special import ndtri
    out[ok] = ndtri(np.clip(p, 1e-7, 1 - 1e-7)); return out

def rank_ic(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if int(ok.sum()) < 30: return float("nan")
    return float(np.corrcoef(rankdata(a[ok]), rankdata(b[ok]))[0, 1])

def ema(prev: np.ndarray | None, cur: np.ndarray, decay: float) -> np.ndarray:
    if prev is None: return cur.copy()
    out = prev.copy(); ok = np.isfinite(cur)
    init = ok & ~np.isfinite(prev)
    out[init] = cur[init]
    blend = ok & np.isfinite(prev)
    out[blend] = decay * prev[blend] + (1.0 - decay) * cur[blend]
    return out

def _ymd(value) -> str:
    return str(value).replace("-", "")

def load_intraday_scores(runs_dir: Path, oos_root: Path):
    """15:00 分时模型截面分数。

    每个信号日：live 运行目录里最新一次发布的 intraday_next_session_predictions.csv
    优先（与 intraday_ic_history 同规则）；更早的历史日回退到冻结 OOS 文件的
    15:00 上下文行。返回 (scores_df, meta)。
    """
    import intraday_ic_history as iih
    live, _industry, _audit = iih.load_live_runs(runs_dir)
    live_days: set[str] = set()
    frames = []
    if len(live):
        lf = live[["ts_code", "signal_day", "pred_return"]].copy()
        lf["signal_date"] = lf["signal_day"].map(_ymd)
        live_days = set(lf["signal_date"].unique())
        frames.append(lf[["ts_code", "signal_date", "pred_return"]].assign(score_source="live"))
    oos_path = iih.resolve_oos(oos_root)
    oos_days: list[str] = []
    oos_fallback: dict[str, pd.Series] = {}
    if oos_path is not None:
        of = pd.read_parquet(oos_path, columns=["ts_code", "context_end_date", "context_end_ts", "pred_return", "actual_next_return"])
        # context_end_ts carries seconds ("... 15:00:00"), so match on the
        # parsed HH:MM like intraday_ic_history does for live CSV rows.
        hm = pd.to_datetime(of["context_end_ts"], errors="coerce").dt.strftime("%H:%M")
        of = of[hm == "15:00"]
        of = of.dropna(subset=["pred_return"])
        of["signal_date"] = of["context_end_date"].map(_ymd)
        of = of[~of["signal_date"].isin(live_days)]
        oos_days = sorted(of["signal_date"].unique())
        if len(of):
            frames.append(of[["ts_code", "signal_date", "pred_return"]].assign(score_source="oos"))
            # 面板缺历史 30 分钟 bar 时，用 OOS 自带的成熟标签兜底。
            for day, group in of.groupby("signal_date"):
                oos_fallback[str(day)] = group.set_index("ts_code")["actual_next_return"]
    meta = {"live_days": sorted(live_days), "oos_days": oos_days,
            "oos_file": str(oos_path) if oos_path is not None else None}
    if not frames:
        return pd.DataFrame(columns=["ts_code", "signal_date", "pred_return"]), meta
    return pd.concat(frames, ignore_index=True), meta

def intraday_label_vectors(closes: pd.DataFrame, day: str, codes: list[str], fallback=None):
    """actual = 信号日 15:00 收盘 → 下一交易时段 10:00 收盘 - 1（统一口径）。"""
    ret = np.full(len(codes), np.nan)
    ok_base = np.zeros(len(codes), dtype=bool)
    sig_col = f"{_ymd(day)[:4]}-{_ymd(day)[4:6]}-{_ymd(day)[6:]} 15:00"
    if sig_col in closes.columns:
        exec_cols = sorted(c for c in closes.columns if c.endswith(" 10:00") and c > sig_col)
        if exec_cols:
            base = closes[sig_col].reindex(codes).to_numpy(float)
            nxt = closes[exec_cols[0]].reindex(codes).to_numpy(float)
            ok = np.isfinite(base) & (base > 0) & np.isfinite(nxt) & (nxt > 0)
            ret[ok] = nxt[ok] / base[ok] - 1.0
            ok_base = ok
    elif fallback is not None:
        aligned = fallback.reindex(codes).to_numpy(float)
        ok = np.isfinite(aligned)
        ret[ok] = aligned[ok]
        ok_base = ok
    return ret, ok_base

def _bench_series(shadow: Path) -> dict[str, float]:
    """沪深300 收盘价字典（YYYYMMDD -> close）；文件缺失时返回空。"""
    path = shadow / "data" / "index_000300_SH.parquet"
    if not path.exists(): return {}
    frame = pd.read_parquet(path)
    out = {}
    for value, close in zip(frame["trade_date"], frame["close"]):
        key = "".join(ch for ch in str(value) if ch.isdigit())[:8]
        if len(key) == 8 and close > 0: out[key] = float(close)
    return out

def book_metrics(trades: list[dict], daily=None) -> dict:
    """Trade diagnostics and account returns have distinct denominators."""
    rets = np.array([t["ret"] for t in trades if np.isfinite(t["ret"])], dtype=float)
    exc = np.array([t["excess"] for t in trades if np.isfinite(t.get("excess", np.nan))], dtype=float)
    values = list((daily or {}).values())
    nav = np.array([v["nav"] for v in values], dtype=float)
    peak = np.maximum.accumulate(np.r_[1.0, nav])
    return {"trades": len(rets), "win_rate": float((rets > 0).mean()) if len(rets) else None,
            "mean_return": float(rets.mean()) if len(rets) else None,
            "mean_excess": float(exc.mean()) if len(exc) else None,
            "forced_closed": sum(bool(t.get("forced")) for t in trades),
            "gate_exits": sum(t.get("reason") == "gate" for t in trades),
            "total_return": float(nav[-1] - 1) if len(nav) else 0.0,
            "max_drawdown": float(-(np.r_[1.0, nav] / peak - 1).min())}


def simulate_signal_book(series_by_day, close_by_day, dates, codes, top_k, exit_rank,
                         bench_close, gate_by_day=None, *, open_by_day=None,
                         fee_bps=10.0, initial_cash=1_000_000.0):
    """Compatibility adapter to the shared self-financing, next-open engine.

    Explicit opens are mandatory: silently executing at the signal close would
    reintroduce the original timing bias. `dates` contains every market session.
    """
    from portfolio_replay import replay_portfolio
    if open_by_day is None:
        raise ValueError("Next-open replay requires open_by_day")
    closes = pd.DataFrame.from_dict(close_by_day, orient="index", columns=codes).reindex(dates)
    opens = pd.DataFrame.from_dict(open_by_day, orient="index", columns=codes).reindex(dates)
    scores = pd.DataFrame.from_dict(series_by_day, orient="index", columns=codes)
    result = replay_portfolio(opens, closes, scores, top_k=top_k, exit_rank=exit_rank,
                              mode="off", gate_by_day=gate_by_day, fee_bps=fee_bps,
                              initial_cash=initial_cash)
    trades = result.trades.to_dict("records")
    stats = {}
    for t in trades:
        a, b = bench_close.get(t["entry"]), bench_close.get(t["exit"])
        if a and b:
            t["excess"] = t["ret"] - (b / a - 1)
        stats.setdefault(t["exit"], []).append(t)
    port = {}; base_bench = bench_close.get(dates[0]); prev_bench = base_bench
    for row in result.daily.to_dict("records"):
        day = str(row["date"]); b = bench_close.get(day)
        row["bench_ret"] = b / prev_bench - 1 if b and prev_bench else None
        row["cum_excess"] = row["nav"] / (b / base_bench) - 1 if b and base_bench else None
        if b: prev_bench = b
        port[day] = row
    return trades, stats, port


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--kronos-source", choices=("daily", "intraday", "composite"), default="composite")
    ap.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parent / "runs")
    ap.add_argument("--kronos-ema", type=float, default=0.88)
    ap.add_argument("--set10-ema", type=float, default=0.88)
    ap.add_argument("--kronos-weight", type=float, default=0.45)
    ap.add_argument("--set10-weight", type=float, default=0.55)
    ap.add_argument("--lookback-days", type=int, default=10)
    ap.add_argument("--holding-days", type=int, default=2)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--exit-rank", type=int, default=1000)
    ap.add_argument("--gate-alpha", type=float, default=0.0)
    ap.add_argument("--gate-source", choices=("off", "wave", "chan"), default="chan")
    ap.add_argument("--gate-mode", choices=("off", "non_down", "up_only"), default=None)
    ap.add_argument("--fee-bps", type=float, default=10.0)
    ap.add_argument("--initial-cash", type=float, default=1_000_000.0)
    args = ap.parse_args()
    for name, value in (("kronos_ema", args.kronos_ema), ("set10_ema", args.set10_ema)):
        if not 0 <= value < 1: raise ValueError(f"{name} must be in [0,1)")
    if args.kronos_source != "composite" and abs(args.kronos_weight + args.set10_weight - 1) > 1e-6: raise ValueError("factor weights must sum to 1")
    if args.lookback_days < 1 or args.holding_days < 1 or args.top_k < 1: raise ValueError("window values must be positive")
    if args.exit_rank < 1: raise ValueError("exit_rank must be positive")
    if args.gate_alpha != 0 and not (0.5 <= args.gate_alpha < 1): raise ValueError("gate_alpha must be 0 (off) or in [0.5,1)")
    shadow = ROOT / "factor_horizon" / "shadow_live"
    curves = shadow / "curves" / "rp10_current.parquet"
    if not curves.exists():
        found = sorted(shadow.glob("curves/*rp10*.parquet")); curves = found[-1] if found else curves
    if args.kronos_source != "intraday" and not curves.exists(): raise FileNotFoundError(f"Kronos curve not found: {curves}")
    if args.kronos_source == "composite":
        from composite_replay import evaluate
        panel_paths = sorted((shadow / "data" / "panels").glob("astock_daily_ohlcva_*.parquet"))
        if not panel_paths:
            raise FileNotFoundError("No official daily panel")
        mode = args.gate_mode or ("up_only" if args.gate_source == "chan" else "off")
        if args.gate_source == "off": mode = "off"
        if args.gate_source == "wave" and args.gate_mode != "off":
            raise ValueError("Pure composite replay uses off/non_down/up_only; choose an explicit gate mode")
        payload = evaluate(panel_paths[-1], curves, shadow / "data" / "index_000300_SH.parquet",
                           args.output_dir, end=str(args.date).replace("-", ""), modes=(mode,),
                           fees=(args.fee_bps,), top_k=args.top_k, exit_rank=args.exit_rank,
                           initial_cash=args.initial_cash, single=True)
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return
    from factor_horizon.factor_panel import SET10_FACTORS, build_set10_factor_matrices
    # Locate dated official snapshots.  They are cumulative, so only the
    # newest snapshot is loaded; this keeps memory bounded while retaining the
    # full historical warm-up for the 60-day SET10 features.
    paths: dict[str, Path] = {}
    for p in ROOT.rglob("astock_daily_ohlcva_*.parquet"):
        m = re.search(r"(\d{8})\.parquet$", p.name)
        if not m: continue
        d = m.group(1)
        if d not in paths or "shadow_live/data/panels" in str(p): paths[d] = p
    # Official panel files are cumulative snapshots (the 20260911 file in
    # particular contains history back to 2005), so loading every dated
    # snapshot would duplicate billions of rows.  Use the newest snapshot;
    # the feature builder itself supplies the historical warm-up.
    dates = [sorted(paths)[-1]] if paths else []
    if not dates: raise FileNotFoundError("no daily OHLCV panels found")
    frames = [pd.read_parquet(paths[d], columns=["ts_code","timestamps","open","high","low","close","volume","amount"]) for d in dates]
    panel = pd.concat(frames, ignore_index=True)
    panel["timestamps"] = pd.to_datetime(panel["timestamps"])
    raw, wide, idx = build_set10_factor_matrices(panel)
    # Canonical SET10 factorwise rank->rank_pct score, equivalent to dynamic_strategy.
    factor_score = {}
    for name in SET10_FACTORS:
        a = raw[name].to_numpy(float); z = np.full_like(a, np.nan)
        for i in range(len(a)):
            ok = np.isfinite(a[i]);
            if ok.sum() < 100: continue
            v = a[i, ok]; z[i, ok] = np.clip((v-v.mean())/(v.std()+1e-12), -3, 3)
        r = np.full_like(z, np.nan)
        for i in range(len(z)):
            ok = np.isfinite(z[i]);
            if ok.sum() >= 100: r[i, ok] = rankdata(z[i, ok], method="average")/ok.sum()-.5
        factor_score[name] = r
    set_raw = np.nanmean(np.stack([factor_score[n] for n in SET10_FACTORS], axis=2), axis=2)
    set_score = np.stack([normal_score(row) for row in set_raw])
    codes = list(wide["close"].columns); pos = {c:i for i,c in enumerate(codes)}
    intraday_meta: dict = {}
    ret_by_day: dict[str, np.ndarray] = {}
    base_ok_by_day: dict[str, np.ndarray] = {}
    scores_df = kf = None
    if args.kronos_source == "intraday":
        import intraday_ic_history as iih
        scores_df, intraday_meta = load_intraday_scores(args.runs_dir, ROOT)
        if not len(scores_df):
            raise FileNotFoundError("没有可用的分时预测：live 运行目录与冻结 OOS 文件均为空")
        panel30 = iih.resolve_panel(ROOT)
        days = sorted({str(x) for x in scores_df["signal_date"].unique()})
        closes = iih.exec_close_series(panel30, days) if panel30 is not None else pd.DataFrame()
        for day in days:
            fallback = intraday_meta.get("oos_fallback", {}).get(day)
            ret, base_ok = intraday_label_vectors(closes, day, codes, fallback)
            ret_by_day[day] = ret; base_ok_by_day[day] = base_ok
        k_dates = days
        score_source = "intraday_live+oos"
    else:
        # Kronos scores are normalised cross-sectionally per horizon before EMA.
        kf = pd.read_parquet(curves)
        kf["signal_date"] = kf["signal_date"].astype(str)
        k_dates = sorted(set(kf.signal_date) & set(idx.strftime("%Y%m%d")))
        score_source = "composite_e1e3e5e10" if args.kronos_source == "composite" else "daily_rp10"
    k_raw: dict[str, np.ndarray] = {}
    for d in k_dates:
        if args.kronos_source == "composite":
            # 组合因子排名：E1/E3/E5/E10 各取横截面百分位 [-1,1] 等权平均（与 composite_factor.py 同口径）
            sub = kf[kf.signal_date == d].set_index("ts_code")
            comp, cover = None, None
            for h in (1, 3, 5, 10):
                sv = pd.to_numeric(sub.get(f"k_e{h}"), errors="coerce").reindex(codes)
                z = sv.rank(pct=True) * 2 - 1
                comp = z.fillna(0.0) if comp is None else comp + z.fillna(0.0)
                cover = z.notna() if cover is None else cover | z.notna()
            arr = comp.to_numpy(float) / 4.0
            arr[~cover.to_numpy()] = np.nan
            k_raw[d] = arr
            continue
        sub = scores_df[scores_df.signal_date == d] if args.kronos_source == "intraday" else kf[kf.signal_date == d]
        arr = np.full((len(codes),), np.nan)
        # Use the configured holding horizon as the daily Kronos E_h score; the
        # intraday source carries a single pred_return column instead of E1..E10.
        col = "pred_return" if args.kronos_source == "intraday" else f"k_e{min(args.holding_days,10)}"
        vals = pd.to_numeric(sub[col], errors="coerce")
        for code, val in zip(sub.ts_code.astype(str), vals):
            if code in pos: arr[pos[code]] = float(val)
        k_raw[d] = normal_score(arr)
    # Align source scores and calculate causal EMA on available signal dates.
    signals = sorted(set(k_dates) & set(idx.strftime("%Y%m%d")))
    target = str(args.date).replace("-", "")
    signals = [d for d in signals if d <= target]
    replay_dates = [d.strftime("%Y%m%d") for d in idx if signals[0] <= d.strftime("%Y%m%d") <= target]
    gate_by_day = None
    mode = args.gate_mode or ("up_only" if args.gate_source == "chan" else "off")
    if args.gate_source == "chan" and mode != "off":
        from chan_phase_engine import chan_phases, macd
        cf = pd.read_parquet(shadow / "data" / "index_000300_SH.parquet")
        cf["date"] = cf.trade_date.astype(str).str.replace("-", "", regex=False)
        cf = cf[cf.date <= target].sort_values("date")
        dif, _, _ = macd(cf.close)
        phases, _, _, _ = chan_phases(cf.close.to_numpy(float), dif.to_numpy(float))
        pmap = dict(zip(cf.date, phases))
        gate_by_day = {d: bool(np.isfinite(pmap.get(d, np.nan)) and
                       (pmap[d] == 1 if mode == "up_only" else pmap[d] != -1)) for d in replay_dates}
    elif args.gate_source == "wave" and args.gate_alpha >= 0.5 and args.gate_mode != "off":
        wpath = Path(__file__).resolve().parent / "data" / "market_wave.json"
        if not wpath.exists(): raise FileNotFoundError(wpath)
        wdata = json.loads(wpath.read_text(encoding="utf-8")).get("daily", [])
        em, wmap = None, {}
        for r in sorted(wdata, key=lambda r: str(r["date"])):
            if r.get("score") is not None:
                em = float(r["score"]) if em is None else args.gate_alpha * em + (1 - args.gate_alpha) * float(r["score"])
            wmap[str(r["date"])] = em
        gate_by_day = {d: wmap.get(d) is not None and wmap[d] >= 0 for d in replay_dates}
    idx_pos = {d.strftime("%Y%m%d"):i for i,d in enumerate(idx)}
    close_by_day = {d: wide["close"].iloc[idx_pos[d]].to_numpy(float) for d in replay_dates}
    open_by_day = {d: wide["open"].iloc[idx_pos[d]].to_numpy(float) for d in replay_dates}
    bench_close = _bench_series(shadow)
    k_series: dict[str, np.ndarray] = {}; set_series: dict[str, np.ndarray] = {}; comp_series: dict[str, np.ndarray] = {}
    # Warm the EMA on every causal signal available before selecting the
    # requested reporting window. Otherwise changing lookback changes the
    # indicator's initial state and leaks a window-dependent warm-up.
    report_signals = set(signals[-args.lookback_days:])
    set_prev = k_prev = None; rows=[]
    for d in signals:
        i = idx_pos[d]; set_prev = ema(set_prev, set_score[i], args.set10_ema); k_prev = ema(k_prev, k_raw[d], 0.0 if args.kronos_source == "composite" else args.kronos_ema)
        composite = args.kronos_weight*k_prev + args.set10_weight*set_prev
        k_series[d] = k_prev; set_series[d] = set_prev; comp_series[d] = composite
        if args.kronos_source == "intraday":
            if d not in ret_by_day: continue
            ret = ret_by_day[d]; ret_ok = np.isfinite(ret)
            eligible = np.isfinite(composite) & base_ok_by_day[d]
        else:
            # next-open to holding-days-open return; labels are only read after
            # ranking.  Rows whose holding window has not matured keep NaN labels
            # so the daily mark-to-market series stays continuous.
            entry_i, exit_i = i+1, i+1+args.holding_days
            ret = np.full(len(codes), np.nan); ret_ok = ret.astype(bool)
            eligible = np.isfinite(composite)
            if exit_i < len(idx):
                op0, op1 = wide["open"].iloc[entry_i].to_numpy(float), wide["open"].iloc[exit_i].to_numpy(float)
                ret = op1/op0-1
                ret[~(np.isfinite(op1) & (op1 > 0))] = np.nan
                ret_ok = np.isfinite(ret) & (op1 > 0)
                eligible = np.isfinite(composite)&np.isfinite(op0)&(op0>0)
        if eligible.sum() == 0: continue
        order = np.argsort(np.where(np.isfinite(composite), composite, -np.inf))[::-1]
        # Select using scores and tradability known on the signal date. Future
        # returns only determine whether the selected names have matured labels.
        top = [j for j in order if eligible[j]][:args.top_k]
        def source_topk(source: np.ndarray) -> float:
            order = np.argsort(np.where(np.isfinite(source), source, -np.inf))[::-1]
            chosen = [j for j in order if eligible[j]][:args.top_k]
            matured = [ret[j] for j in chosen if ret_ok[j]]
            return float(np.mean(matured)) if matured else np.nan
        matured = [ret[j] for j in top if ret_ok[j]]
        rows.append({"signal_date":d,"kronos_ic":rank_ic(k_prev,ret),"set10_ic":rank_ic(set_prev,ret),"composite_ic":rank_ic(composite,ret),"topk_count":len(top),"matured_topk_count":len(matured),"topk_mean_return":float(np.mean(matured)) if matured else np.nan,"topk_median_return":float(np.median(matured)) if matured else np.nan,"kronos_topk_return":source_topk(k_prev),"set10_topk_return":source_topk(set_prev),"universe":int(eligible.sum()),"matured_universe":int(ret_ok.sum()),"kronos_weight":args.kronos_weight,"set10_weight":args.set10_weight,"kronos_ema":args.kronos_ema,"set10_ema":args.set10_ema,"holding_days":args.holding_days,"in_window":1 if d in report_signals else 0})
    all_rows = rows
    rows = [r for r in rows if r.get("in_window")]
    books = {
        "kronos": simulate_signal_book(k_series, close_by_day, replay_dates, codes, args.top_k, args.exit_rank, bench_close, gate_by_day, open_by_day=open_by_day, fee_bps=args.fee_bps, initial_cash=args.initial_cash),
        "set10": simulate_signal_book(set_series, close_by_day, replay_dates, codes, args.top_k, args.exit_rank, bench_close, gate_by_day, open_by_day=open_by_day, fee_bps=args.fee_bps, initial_cash=args.initial_cash),
        "comp": simulate_signal_book(comp_series, close_by_day, replay_dates, codes, args.top_k, args.exit_rank, bench_close, gate_by_day, open_by_day=open_by_day, fee_bps=args.fee_bps, initial_cash=args.initial_cash),
    }
    book_summary = {name: book_metrics(trades, _port) for name, (trades, _stats, _port) in books.items()}
    row_map = {r["signal_date"]: r for r in all_rows}
    all_rows = [row_map.get(d, {"signal_date": d, "in_window": 0}) for d in replay_dates]
    for row in all_rows:
        d = row["signal_date"]
        pp = books["comp"][2].get(d) or {}
        row.update({key: pp.get(key) for key in ("nav", "equity", "cash", "cum_return", "drawdown", "exposure", "gate_open")})
        row["port_ret"] = pp.get("port_ret")
        row["bench_ret"] = pp.get("bench_ret")
        row["cum_excess"] = pp.get("cum_excess")
        row["n_held"] = pp.get("n_held", 0)
    for row in rows:
        d = row["signal_date"]
        for name, (_trades, stats, _port) in books.items():
            ts = stats.get(d) or []
            rets = [t["ret"] for t in ts if np.isfinite(t["ret"])]
            exs = [t["excess"] for t in ts if np.isfinite(t.get("excess", float("nan")))]
            row[f"{name}_day_trades"] = len(ts)
            row[f"{name}_day_mean"] = float(np.mean(rets)) if rets else np.nan
            row[f"{name}_day_excess"] = float(np.mean(exs)) if exs else np.nan
            row[f"{name}_day_win"] = float(np.mean([r > 0 for r in rets])) if rets else np.nan
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, (trades, _stats, _port) in books.items():
        pd.DataFrame(trades).to_csv(args.output_dir / f"factor_trades_{name}.csv", index=False)
    daily = pd.DataFrame(rows)
    pd.DataFrame(all_rows).to_csv(args.output_dir/"factor_daily.csv", index=False)
    metrics = []
    for source, col in (("Kronos","kronos_ic"),("SET10","set10_ic"),("Composite","composite_ic")):
        vals = pd.to_numeric(daily[col], errors="coerce") if len(daily) else pd.Series(dtype=float)
        return_col = "kronos_topk_return" if source == "Kronos" else "set10_topk_return" if source == "SET10" else "topk_mean_return"
        rv = pd.to_numeric(daily.get(return_col,pd.Series(dtype=float)),errors="coerce")
        metrics.append({"factor":source,"sessions":int(vals.notna().sum()),"mean_rank_ic":float(vals.mean()) if vals.notna().any() else None,"mean_topk_return":float(rv.mean()) if rv.notna().any() else None,"weight":args.kronos_weight if source=="Kronos" else args.set10_weight if source=="SET10" else 1.0,"ema":args.kronos_ema if source=="Kronos" else args.set10_ema if source=="SET10" else None})
    for m in metrics:
        key = {"Kronos": "kronos", "SET10": "set10", "Composite": "comp"}[m["factor"]]
        m.update(book_summary[key])
        m["mean_topk_return"] = m["mean_return"]
    pd.DataFrame(metrics).to_csv(args.output_dir/"factor_performance.csv", index=False)
    payload={"available":bool(rows),"kind":"factor_evaluation","kronos_source":args.kronos_source,"score_source":score_source,"label_convention":"信号日 15:00 收盘 → 下一交易时段 10:00 收盘（与分时 IC 历史同口径）" if args.kronos_source=="intraday" else "信号次日开盘进入，持有指定交易日后开盘计算（日线口径）","intraday_score_days":{"live":intraday_meta.get("live_days",[]),"oos":intraday_meta.get("oos_days",[])},"requested_signal_date":target,"signals_evaluated":len(rows),"signals_charted":len(all_rows),"effective_parameters":{"kronos_source":args.kronos_source,"kronos_weight":args.kronos_weight,"set10_weight":args.set10_weight,"kronos_ema":args.kronos_ema,"set10_ema":args.set10_ema,"lookback_days":args.lookback_days,"holding_days":args.holding_days,"top_k":args.top_k},"data_sessions_loaded":len(idx),"data_first_date":idx.min().strftime("%Y%m%d"),"data_last_date":idx.max().strftime("%Y%m%d"),"exit_rank":args.exit_rank,"engine_version":"causal_cash_book_v2","gate_mode":mode,"fee_bps":args.fee_bps,"gate_alpha":args.gate_alpha,"gate_source":args.gate_source,"gate_applied":gate_by_day is not None,"benchmark":"沪深300（index_000300_SH 收盘）","strategy":"收盘信号、次日开盘；最多TopK席位，只补当日前TopK；现金+股数每日估值；末日收盘强平","book_summary":book_summary,"kronos_curve":str(curves),"metrics":metrics,"daily_curve":str(args.output_dir/"factor_daily.csv"),"read_only":True}
    (args.output_dir/"factor_snapshot.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(payload,ensure_ascii=False))

if __name__ == "__main__": main()
