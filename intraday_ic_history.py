#!/usr/bin/env python3
"""Rolling RankIC / directional-accuracy history for the intraday model.

Sources, in the pipeline's own label convention:
  * frozen OOS parquet (oos_intraday*/summary_lora/oos_predictions_with_trade_filter.parquet)
    - 15:00 context rows only; label = actual_next_return (exec bar = next session 10:00).
  * live published run CSVs (runs/intraday_live_*/intraday_next_session_predictions.csv)
    - one run per signal day (newest wins); label recomputed from the canonical panel as
      close(next session 10:00 bar) / close(signal 15:00 bar) - 1, i.e. the exact
      actual_next_return identity, cross-checked against the OOS file.

Outputs JSON: overall daily IC series, per-industry IC series, per-stock IC series.
Read-only wrt models and orders; paper research only.
"""
from __future__ import annotations
import argparse, glob, json, os, sys
from pathlib import Path
import numpy as np
import pandas as pd


def resolve_oos(root: Path) -> Path | None:
    base = root / "finetune_lora" / "output"
    paths = sorted(base.glob("oos_intraday*/summary_lora/oos_predictions_with_trade_filter.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def resolve_panel(root: Path) -> Path | None:
    data = root / "finetune_lora" / "data"
    paths = []
    for p in data.glob("astock_30min_ohlcva_full_*.parquet"):
        name = p.name.lower()
        if "_em_" in name or "_tushare_" in name or "backfill" in name:
            continue
        paths.append(p)
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def load_live_runs(runs_dir: Path) -> tuple[pd.DataFrame, dict, list[dict]]:
    """Newest published CSV per signal day.  Returns rows, code->industry map, run audit."""
    frames, industry_map, audit = [], {}, []
    for csv_path in sorted(glob.glob(str(runs_dir / "intraday_live_*" / "intraday_next_session_predictions.csv"))):
        csv_path = Path(csv_path)
        try:
            f = pd.read_csv(csv_path, usecols=lambda c: c in {"ts_code", "name", "industry", "signal_timestamp", "observed_latest_bar", "pred_return"})
        except Exception:
            continue
        if f.empty:
            continue
        f["signal_timestamp"] = pd.to_datetime(f["signal_timestamp"], errors="coerce")
        f = f[f["signal_timestamp"].dt.strftime("%H:%M") == "15:00"]
        f = f.dropna(subset=["signal_timestamp", "pred_return"])
        if f.empty:
            continue
        f["signal_day"] = f["signal_timestamp"].dt.strftime("%Y-%m-%d")
        run = {"run_dir": csv_path.parent.name, "mtime": csv_path.stat().st_mtime,
               "signal_day": str(f["signal_day"].iloc[0]), "rows": int(len(f))}
        audit.append(run)
        for code, ind in zip(f["ts_code"], f.get("industry", pd.Series(dtype=str))):
            if isinstance(ind, str) and ind.strip():
                industry_map.setdefault(code, ind.strip())
        frames.append(f)
    if not frames:
        return pd.DataFrame(), industry_map, audit
    allf = pd.concat(frames, ignore_index=True)
    allf["_prio"] = allf["signal_day"].map({r["signal_day"]: r["mtime"] for r in audit})
    allf = allf.sort_values("_prio").drop_duplicates(["ts_code", "signal_day"], keep="last")
    return allf.drop(columns="_prio"), industry_map, audit


def exec_close_series(panel: Path, days: list[str]) -> pd.DataFrame:
    """close(ts_code x timestamps) restricted to 15:00 signal bars and 10:00 exec bars."""
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(panel)
    ts_idx = pf.schema_names.index("timestamps") if hasattr(pf, "schema_names") else pf.schema.names.index("timestamps")
    lo = pd.Timestamp(min(days)) - pd.Timedelta(days=6)
    hi = pd.Timestamp(max(days)) + pd.Timedelta(days=6)
    frames = []
    for i in range(pf.num_row_groups):
        stats = pf.metadata.row_group(i).column(ts_idx).statistics
        if stats and stats.max is not None and pd.Timestamp(stats.max) < lo:
            continue
        if stats and stats.min is not None and pd.Timestamp(stats.min) > hi:
            continue
        t = pf.read_row_group(i, columns=["ts_code", "timestamps", "close"]).to_pandas()
        frames.append(t)
    if not frames:
        return pd.DataFrame()
    f = pd.concat(frames, ignore_index=True)
    f["timestamps"] = pd.to_datetime(f["timestamps"], errors="coerce")
    f["close"] = pd.to_numeric(f["close"], errors="coerce")
    f = f.dropna().query("close > 0")
    f["hm"] = f["timestamps"].dt.strftime("%H:%M")
    keep = f[f["hm"].isin(["15:00", "10:00"])].copy()
    keep["ts"] = keep["timestamps"].dt.strftime("%Y-%m-%d %H:%M")
    return keep.pivot_table(index="ts_code", columns="ts", values="close", aggfunc="last")


def label_live(live: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """actual = close(next session 10:00) / close(signal day 15:00) - 1."""
    if live.empty or closes.empty:
        return pd.DataFrame()
    live = live[["ts_code", "signal_day", "pred_return"]].copy()
    cols = list(closes.columns)
    sig_cols = sorted(c for c in cols if c.endswith(" 15:00"))
    exec_cols_all = sorted(c for c in cols if c.endswith(" 10:00"))
    rows = []
    for day, g in live.groupby("signal_day"):
        sig_col = f"{day} 15:00"
        if sig_col not in closes.columns:
            continue
        exec_cols = [c for c in exec_cols_all if c > sig_col]
        if not exec_cols:
            continue
        exec_col = exec_cols[0]
        base = closes[sig_col]
        nxt = closes[exec_col]
        lab = (nxt / base - 1.0).rename("actual").reset_index()
        lab["signal_day"] = day
        lab["exec_day"] = exec_col[:10]
        rows.append(lab)
    if not rows:
        return pd.DataFrame()
    labels = pd.concat(rows, ignore_index=True)
    out = live.merge(labels, on=["ts_code", "signal_day"], how="inner")
    out = out.dropna(subset=["actual", "pred_return"])
    out["target_trade_date"] = out["exec_day"]
    out["source"] = "live"
    return out[["ts_code", "signal_day", "target_trade_date", "pred_return", "actual", "source"]]


def load_oos(oos: Path) -> pd.DataFrame:
    d = pd.read_parquet(oos, columns=["ts_code", "context_end_ts", "pred_return", "actual_next_return"])
    d["context_end_ts"] = pd.to_datetime(d["context_end_ts"])
    d = d[d["context_end_ts"].dt.strftime("%H:%M") == "15:00"]
    d = d.dropna(subset=["pred_return", "actual_next_return"])
    d["signal_day"] = d["context_end_ts"].dt.strftime("%Y-%m-%d")
    d["exec_day"] = (d["context_end_ts"] + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    d["target_trade_date"] = d["exec_day"]
    d["actual"] = d["actual_next_return"].astype(float)
    d["source"] = "oos"
    return d[["ts_code", "signal_day", "target_trade_date", "pred_return", "actual", "source"]]


def _spearman(sub: pd.DataFrame) -> float:
    if len(sub) < 5:
        return np.nan
    return float(sub["pred_return"].corr(sub["actual"], method="spearman"))


def _dir_acc(sub: pd.DataFrame) -> float:
    s = sub[sub["actual"].abs() > 1e-9]
    if s.empty:
        return np.nan
    return float((np.sign(s["pred_return"]) == np.sign(s["actual"])).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/opt/kronos")
    ap.add_argument("--runs-dir", default="/opt/quantweave/runs")
    ap.add_argument("--window", type=int, default=12, help="max matured signal days to keep")
    ap.add_argument("--min-industry-n", type=int, default=5)
    a = ap.parse_args()
    root, runs_dir = Path(a.project_root), Path(a.runs_dir)

    live, industry_map, audit = load_live_runs(runs_dir)
    panel = resolve_panel(root)
    oos_path = resolve_oos(root)
    oos_rows = load_oos(oos_path) if oos_path else pd.DataFrame()

    live_rows = pd.DataFrame()
    label_check = None
    days_needed = sorted(set(live["signal_day"]) | set(oos_rows["signal_day"])) if panel is not None else []
    if days_needed:
        closes = exec_close_series(panel, days_needed)
        if not live.empty:
            live_rows = label_live(live, closes)
        # validate the panel label against the parquet's own actual_next_return
        if not oos_rows.empty:
            panel_oos = label_live(oos_rows, closes)
            if not panel_oos.empty:
                chk = oos_rows.merge(panel_oos[["ts_code", "signal_day", "actual"]], on=["ts_code", "signal_day"], suffixes=("_parquet", "_panel"))
                chk = chk.dropna(subset=["actual_parquet", "actual_panel"])
                if len(chk):
                    label_check = {"n": int(len(chk)), "max_abs_diff": float((chk.actual_parquet - chk.actual_panel).abs().max()),
                                   "corr": float(chk.actual_parquet.corr(chk.actual_panel))}

    combined = pd.concat([oos_rows, live_rows], ignore_index=True)
    combined = combined.drop_duplicates(["ts_code", "signal_day"], keep="first")
    if combined.empty:
        print(json.dumps({"available": False, "reason": "没有可用的已成熟预测样本"}))
        return
    days = sorted(combined["signal_day"].unique())[-int(a.window):]
    combined = combined[combined["signal_day"].isin(days)].copy()
    combined["industry"] = combined["ts_code"].map(industry_map).fillna("未知")

    overall = []
    for day, g in combined.groupby("signal_day"):
        overall.append({"date": day, "ic": _spearman(g), "dir_acc": _dir_acc(g), "n": int(len(g))})

    industries = []
    for ind, g in combined.groupby("industry"):
        series = []
        for day in days:
            sub = g[g["signal_day"] == day]
            series.append(None if sub.empty or len(sub) < a.min_industry_n else round(_spearman(sub), 4))
        valid = [v for v in series if v is not None]
        industries.append({
            "industry": ind, "series": series,
            "mean_ic": round(float(np.mean(valid)), 4) if valid else None,
            "last_ic": valid[-1] if valid else None,
            "dir_acc": round(_dir_acc(g), 4), "n": int(len(g)),
            "stock_count": int(g["ts_code"].nunique()),
        })
    industries.sort(key=lambda x: (x["mean_ic"] if x["mean_ic"] is not None else -9), reverse=True)

    stocks = {}
    for code, g in combined.groupby("ts_code"):
        if len(g) < 4:
            continue
        g = g.sort_values("signal_day")
        ic = float(g["pred_return"].corr(g["actual"], method="spearman"))
        stocks[code] = {"n": int(len(g)),
                        "mean_ic": None if not np.isfinite(ic) else round(ic, 4),
                        "dir_acc": round(_dir_acc(g), 4),
                        "d": [x[5:] for x in g["signal_day"]],
                        "p": [round(float(r.pred_return), 4) for r in g.itertuples()],
                        "a": [round(float(r.actual), 4) for r in g.itertuples()]}

    payload = {
        "available": True,
        "label_definition": "次日首个10:00 bar收盘 / 信号日15:00 bar收盘 - 1（与OOS评估actual_next_return同口径）",
        "window_days": len(days), "dates": days,
        "overall": overall,
        "overall_mean_ic": round(float(np.nanmean([o["ic"] for o in overall])), 4) if overall else None,
        "industries": industries,
        "stocks": stocks,
        "label_check": label_check,
        "live_runs": audit[-8:],
        "sources": {"oos": str(oos_path) if oos_path else None, "panel": str(panel) if panel else None,
                    "live_rows": int(len(live_rows)), "oos_rows": int(len(oos_rows))},
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
