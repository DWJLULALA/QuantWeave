#!/usr/bin/env python3
"""Build selectable Kronos E1..E10 composite factors.

The UI used to expose one horizon (usually E1 or E10) at a time.  This small,
read-only adapter keeps the canonical ``rp10`` curve untouched and produces a
cross-sectional composite from any selected horizons.  It is intentionally
independent from model inference: no training, orders, or future prices are
read.  ``stable``/``watch``/``break`` are evidence labels based only on the
recent realised signal history and are therefore safe to show as a reminder,
not as an order instruction.

Examples::

    python composite_factor.py --curves rp10_current.parquet \
      --date 20260911 --output-dir runs/composite_20260911 \
      --horizons 1,3,5,10 --weights 0.1,0.2,0.3,0.4 --top-k 10

    python composite_factor.py --curves rp10_current.parquet \
      --date 20260911 --output-dir runs/composite_e1_e3 \
      --horizons 1,3 --weights equal
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _norm(values: pd.Series) -> pd.Series:
    """Cross-sectional percentile score in [-1, 1], larger is better."""
    x = pd.to_numeric(values, errors="coerce")
    valid = x.notna()
    out = pd.Series(np.nan, index=x.index, dtype=float)
    n = int(valid.sum())
    if n:
        # average ranks keep ties deterministic and causal
        out.loc[valid] = x.loc[valid].rank(method="average", pct=True).to_numpy() * 2 - 1
    return out


def _parse_ints(text: str) -> list[int]:
    try:
        values = [int(v.strip().upper().lstrip("E")) for v in text.split(",") if v.strip()]
    except ValueError as exc:
        raise ValueError("horizons must be comma-separated E1..E10 values") from exc
    if not values or any(v < 1 or v > 10 for v in values) or len(set(values)) != len(values):
        raise ValueError("horizons must contain unique values in 1..10")
    return values


def _parse_weights(text: str, count: int) -> list[float]:
    if text.strip().lower() in {"equal", "均等", "等权"}:
        return [1.0 / count] * count
    try:
        weights = [float(v.strip()) for v in text.split(",")]
    except ValueError as exc:
        raise ValueError("weights must be comma-separated numbers or equal") from exc
    if len(weights) != count or any(not math.isfinite(v) or v < 0 for v in weights):
        raise ValueError("weights must match horizons and be non-negative")
    total = sum(weights)
    if total <= 0:
        raise ValueError("at least one weight must be positive")
    return [v / total for v in weights]


def _open_dates(curves_dir: Path) -> list[str]:
    """SSE 交易日历中的开盘日（YYYYMMDD 升序）；日历缺失时返回空。"""
    base = curves_dir.parent / "data"
    cal_dir = base / "cache" / "trade_cal"
    dates: set[str] = set()
    try:
        for p in sorted(cal_dir.glob("SSE_*.parquet")):
            f = pd.read_parquet(p)
            dcol = next((c for c in ("cal_date", "trade_date", "date") if c in f.columns), None)
            ocol = next((c for c in ("is_open", "is_trading", "open") if c in f.columns), None)
            if dcol is None or ocol is None:
                continue
            for v, o in zip(f[dcol], f[ocol]):
                key = "".join(ch for ch in str(v) if ch.isdigit())[:8]
                if len(key) == 8 and str(o).strip().lower() in {"1", "1.0", "true"}:
                    dates.add(key)
    except Exception:
        pass
    try:
        # 交易日缓存通常不含未来日期；合并 shadow-live 生成的 future_calendar（官方日历外推）
        for p in sorted(base.glob("future_calendar_*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            for v in data.get("future_dates", []) or []:
                key = "".join(ch for ch in str(v) if ch.isdigit())[:8]
                if len(key) == 8:
                    dates.add(key)
    except Exception:
        pass
    return sorted(dates)


def _stability(history: pd.DataFrame, top_k: int, window: int, threshold: float) -> pd.DataFrame:
    """Add evidence labels to latest rows, grouped by stock."""
    if history.empty:
        return history.assign(stability_status=pd.Series(dtype=str))
    dates = sorted(history["signal_date"].astype(str).unique())[-window:]
    h = history[history["signal_date"].astype(str).isin(dates)].copy()
    h["rank"] = h.groupby("signal_date")["composite_score"].rank(method="first", ascending=False)
    h["in_top_k"] = h["rank"] <= top_k
    stats = h.groupby("ts_code", as_index=False).agg(
        stability_days=("in_top_k", "sum"),
        observed_days=("in_top_k", "size"),
        mean_rank=("rank", "mean"),
        latest_rank=("rank", "last"),
    )
    stats["stability_ratio"] = stats["stability_days"] / stats["observed_days"].clip(lower=1)
    # A break means a previously stable name has fallen below the median rank
    # on the latest signal.  It is a review/exit reminder, never an order.
    stable = stats["stability_ratio"] >= threshold
    stats["stability_status"] = np.where(
        stable & (stats["latest_rank"] <= top_k), "stable",
        np.where(stable & (stats["latest_rank"] > len(h["ts_code"].unique()) / 2), "break", "watch"),
    )
    latest = h[ h["signal_date"].astype(str) == dates[-1] ][["ts_code", "rank"]].rename(columns={"rank": "latest_rank"})
    stats = stats.drop(columns=["latest_rank"]).merge(latest, on="ts_code", how="left")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Selectable Kronos E1..E10 composite factor")
    ap.add_argument("--curves", type=Path, required=True, help="rp10_current parquet/csv")
    ap.add_argument("--date", required=True, help="signal date YYYYMMDD")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--horizons", default="1,3,5,10", help="for example 1,3,5,10")
    ap.add_argument("--weights", default="equal", help="same length as horizons, or equal")
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--stable-threshold", type=float, default=0.60)
    ap.add_argument("--stock-basic", type=Path, default=None, help="optional stock name/industry parquet")
    args = ap.parse_args()
    horizons = _parse_ints(args.horizons)
    weights = _parse_weights(args.weights, len(horizons))
    if args.lookback_days < 1 or args.top_k < 1 or not 0 < args.stable_threshold <= 1:
        raise ValueError("lookback-days/top-k must be positive and stable-threshold in (0,1]")
    target = str(args.date).replace("-", "")
    if len(target) != 8 or not target.isdigit():
        raise ValueError("date must be YYYYMMDD")
    frame = _read(args.curves)
    required = {"signal_date", "ts_code", *[f"k_e{h}" for h in horizons]}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"curve is missing columns: {missing}")
    frame["signal_date"] = frame["signal_date"].astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8]
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame = frame[frame["signal_date"] <= target].copy()
    dates = sorted(frame["signal_date"].dropna().unique())
    if not dates:
        raise ValueError(f"no curve rows on or before {target}")
    parts = []
    for date in dates:
        day = frame[frame["signal_date"] == date].copy()
        score = pd.Series(0.0, index=day.index)
        coverage = pd.Series(0, index=day.index, dtype=int)
        for horizon, weight in zip(horizons, weights):
            z = _norm(day[f"k_e{horizon}"])
            score = score + weight * z.fillna(0.0)
            coverage = coverage + z.notna().astype(int)
        day["composite_score"] = score.where(coverage > 0)
        day["factor_group"] = "+".join(f"E{h}" for h in horizons)
        day["factor_coverage"] = coverage
        parts.append(day[["signal_date", "ts_code", "composite_score", "factor_group", "factor_coverage"] + [f"k_e{h}" for h in horizons]])
    scores = pd.concat(parts, ignore_index=True).dropna(subset=["composite_score"])
    latest_date = dates[-1]
    latest_raw = frame[frame["signal_date"] == latest_date].copy()
    latest = scores[scores["signal_date"] == latest_date].copy()
    latest["rank"] = latest["composite_score"].rank(method="first", ascending=False).astype(int)
    latest = latest.sort_values("rank")
    stability = _stability(scores, args.top_k, args.lookback_days, args.stable_threshold)
    latest = latest.merge(stability, on="ts_code", how="left", suffixes=("", "_history"))
    hor_cols = [f"k_e{h}" for h in horizons]
    latest = latest.merge(
        latest_raw[["ts_code"] + hor_cols].rename(columns={f"k_e{k}": f"pred_e{k}" for k in horizons}),
        on="ts_code", how="left")
    open_dates = _open_dates(args.curves.parent)
    after = [d for d in open_dates if d > latest_date]
    future = []
    topk_codes = latest.head(args.top_k)["ts_code"]
    for h in horizons:
        item = {"horizon": h, "target_date": (after[h - 1] if len(after) >= h else None)}
        col = f"k_e{h}"
        if col in latest_raw.columns and len(topk_codes):
            vals = pd.to_numeric(latest_raw[latest_raw["ts_code"].isin(topk_codes)][col], errors="coerce")
            item["topk_mean_raw"] = round(float(vals.mean()), 4) if vals.notna().any() else None
        future.append(item)
    date_summary = []
    for dt, grp in sorted(scores.groupby("signal_date")):
        topk_grp = grp.nlargest(args.top_k, "composite_score")
        top_mean = float(topk_grp["composite_score"].mean())
        med = float(grp["composite_score"].median())
        e1_mean = float(pd.to_numeric(topk_grp["k_e1"], errors="coerce").mean()) if "k_e1" in topk_grp else float("nan")
        date_summary.append({"date": dt, "universe": int(len(grp)), "topk_mean_score": round(top_mean, 4),
                             "median_score": round(med, 4), "spread": round(top_mean - med, 4),
                             "pred_e1_mean": (round(e1_mean, 4) if math.isfinite(e1_mean) else None)})
    date_summary = date_summary[-90:]
    basic_path = args.stock_basic
    if basic_path is None:
        candidate = args.curves.parent.parent / "data" / "stock_basic_all.parquet"
        if candidate.exists(): basic_path = candidate
    if basic_path and basic_path.exists() and len(latest):
        basic = pd.read_parquet(basic_path)
        keep = [c for c in ("ts_code", "name", "industry", "market") if c in basic.columns]
        if "ts_code" in keep:
            latest = latest.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
    latest["recommendation"] = np.where(latest["stability_status"] == "stable", "stable_candidate", np.where(latest["stability_status"] == "break", "review_exit", "watch"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    latest.to_csv(args.output_dir / "composite_scores_latest.csv", index=False)
    scores.to_csv(args.output_dir / "composite_scores_history.csv", index=False)
    options = {"groups": [{"id": f"E{h}", "label": f"Kronos E{h}", "horizons": [h], "weights": [1.0]} for h in range(1, 11)] + [{"id": "E1_E3_E5_E10", "label": "E1+E3+E5+E10 等权", "horizons": [1, 3, 5, 10], "weights": [0.25] * 4}, {"id": "custom", "label": "自定义组合", "horizons": horizons, "weights": weights}], "default": "E1_E3_E5_E10"}
    (args.output_dir / "factor_options.json").write_text(json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {"available": True, "kind": "kronos_composite_factor", "signal_date": latest_date, "requested_signal_date": target, "factor_group": "+".join(f"E{h}" for h in horizons), "horizons": horizons, "weights": weights, "lookback_days": args.lookback_days, "top_k": args.top_k, "stable_threshold": args.stable_threshold, "universe": int(len(latest)), "stable_count": int((latest["stability_status"] == "stable").sum()), "break_count": int((latest["stability_status"] == "break").sum()), "date_summary": date_summary,"future": future,"scores_path": str(args.output_dir / "composite_scores_latest.csv"), "history_path": str(args.output_dir / "composite_scores_history.csv"), "options_path": str(args.output_dir / "factor_options.json"), "read_only": True}
    (args.output_dir / "composite_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
