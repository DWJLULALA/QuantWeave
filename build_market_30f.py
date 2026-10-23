#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a long-history equal-weight market 30-minute series from the local
2000-stock A-share 30-minute panel.

The panel stores per-stock bars.  We convert every stock's bar to a return
relative to that stock's previous close, winsorise extreme resumption gaps, and
average the cross-section at each timestamp.  A synthetic index level is then
compounded from the average returns.  The result is a *market breadth* series,
not the HS300 index itself; it is used only as the 30-minute structure level.

Output columns: dt, open, high, low, close, volume.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
DEFAULT_PANEL = Path(
    "/opt/kronos/finetune_lora/data/"
    "astock_30min_2000_panel_20260918.parquet"
)
OUT_PATH = ROOT / "data" / "chan_multi" / "bars" / "market_30f.parquet"
META_PATH = ROOT / "data" / "chan_multi" / "bars" / "market_30f.meta.json"
EXPECTED_MINUTES = {10 * 60, 10 * 60 + 30, 11 * 60, 11 * 60 + 30,
                    13 * 60 + 30, 14 * 60, 14 * 60 + 30, 15 * 60}


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=OUT_PATH)
    parser.add_argument("--min-stocks", type=int, default=200)
    parser.add_argument("--clip", type=float, default=0.20)
    parser.add_argument("--limit-row-groups", type=int, default=0)
    args = parser.parse_args()

    parquet = pq.ParquetFile(str(args.panel))
    total_groups = parquet.metadata.num_row_groups
    if args.limit_row_groups:
        total_groups = min(total_groups, args.limit_row_groups)
    log("panel=%s row_groups=%d columns=%s" % (
        args.panel, parquet.metadata.num_row_groups, parquet.schema_arrow.names))

    sums = None
    started = time.time()
    for i in range(total_groups):
        table = parquet.read_row_group(i, columns=[
            "ts_code", "timestamps", "open", "high", "low", "close", "volume"])
        frame = table.to_pandas()
        frame = frame.sort_values(["ts_code", "timestamps"], kind="mergesort")
        prev = frame.groupby("ts_code", sort=False)["close"].shift(1)
        valid = prev.notna() & (prev > 0) & frame["close"].notna()
        if not valid.any():
            continue
        sub = frame.loc[valid].copy()
        base = prev.loc[valid].to_numpy(float)
        for col in ("open", "high", "low", "close"):
            sub[col + "_ret"] = np.clip(sub[col].to_numpy(float) / base - 1.0,
                                        -args.clip, args.clip)
        sub["_one"] = 1.0
        grouped = sub.groupby("ts_code", sort=False)["close"]
        sub["ret5"] = grouped.transform(lambda x: x.pct_change(5))
        sub["ma20"] = grouped.transform(lambda x: x.rolling(20).mean())
        sub["_pos"] = (sub["close_ret"] > 0).astype(float)
        sub["_up5"] = (sub["ret5"] > 0).astype(float)
        sub["_up5_n"] = sub["ret5"].notna().astype(float)
        sub["_above20"] = (sub["close"] > sub["ma20"]).astype(float)
        sub["_above20_n"] = sub["ma20"].notna().astype(float)
        sub["_sq"] = sub["close_ret"] ** 2
        agg = sub.groupby("timestamps", sort=True).agg(
            n=("_one", "sum"),
            so=("open_ret", "sum"), sh=("high_ret", "sum"),
            sl=("low_ret", "sum"), sc=("close_ret", "sum"),
            volume=("volume", "sum"), pos_count=("_pos", "sum"),
            up5_count=("_up5", "sum"), up5_n=("_up5_n", "sum"),
            above20_count=("_above20", "sum"), above20_n=("_above20_n", "sum"),
            ret_sq=("_sq", "sum"),
        )
        sums = agg if sums is None else sums.add(agg, fill_value=0.0)
        if i % 10 == 0 or i == total_groups - 1:
            log("row group %d/%d rows=%d aggregate=%d" % (
                i + 1, total_groups, len(frame), len(sums)))

    if sums is None or sums.empty:
        raise SystemExit("no rows aggregated")

    index = pd.to_datetime(sums.index)
    minutes = index.hour * 60 + index.minute
    keep = np.isin(minutes, list(EXPECTED_MINUTES))
    sums = sums.loc[keep].copy()
    sums = sums[sums["n"] >= args.min_stocks].sort_index()
    if sums.empty:
        raise SystemExit("no timestamp has at least %d stocks" % args.min_stocks)

    rows = []
    level = 1000.0
    for ts, row in sums.iterrows():
        n = float(row["n"])
        if not rows:
            rows.append({"dt": pd.Timestamp(ts), "open": level, "high": level,
                         "low": level, "close": level, "volume": float(row["volume"]),
                         "n_stocks": int(n)})
            continue
        o = level * (1.0 + float(row["so"]) / n)
        h = level * (1.0 + float(row["sh"]) / n)
        l = level * (1.0 + float(row["sl"]) / n)
        c = level * (1.0 + float(row["sc"]) / n)
        h = max(h, o, c)
        l = min(l, o, c)
        mean_ret = float(row["sc"]) / n
        dispersion = math.sqrt(max(float(row["ret_sq"]) / n - mean_ret ** 2, 0.0))
        breadth_up = float(row["pos_count"]) / n
        breadth_5 = (float(row["up5_count"]) / float(row["up5_n"])
                     if float(row["up5_n"]) > 0 else float("nan"))
        breadth_ma20 = (float(row["above20_count"]) / float(row["above20_n"])
                        if float(row["above20_n"]) > 0 else float("nan"))
        rows.append({"dt": pd.Timestamp(ts), "open": o, "high": h, "low": l,
                     "close": c, "volume": float(row["volume"]), "n_stocks": int(n),
                     "breadth_up": breadth_up, "breadth_5": breadth_5,
                     "breadth_ma20": breadth_ma20, "dispersion": dispersion})
        level = c

    out = pd.DataFrame(rows)
    out = out[out["n_stocks"] >= args.min_stocks].reset_index(drop=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.output, index=False)
    meta = {
        "kind": "equal_weight_market_30f",
        "input_panel": str(args.panel),
        "output": str(args.output),
        "min_stocks": args.min_stocks,
        "rows": int(len(out)),
        "start": str(out["dt"].iloc[0]),
        "end": str(out["dt"].iloc[-1]),
        "n_stocks_median": float(out["n_stocks"].median()),
        "n_stocks_min": int(out["n_stocks"].min()),
        "n_stocks_max": int(out["n_stocks"].max()),
        "elapsed_seconds": round(time.time() - started, 1),
        "note": "cross-sectional mean of per-stock bar returns; capped at +/-%.0f%%" % (args.clip * 100),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
