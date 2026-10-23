#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a 5-minute market proxy from a fixed HS300 top-weight basket.

Baostock exposes 5-minute stock bars from 2020 onward (index and ETF minute
bars are not available).  We therefore use the top-weight HS300 constituents
as of a fixed date, download their 5-minute bars sequentially (Baostock does
not support concurrent sessions on the same account), and aggregate
weight-normalised per-stock returns into a synthetic market 5-minute series.

The basket is fixed at the selection date, so its composition cannot leak
future index changes.  The result is a large-cap market proxy, not the exact
HS300 index; every downstream artifact records that limitation.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "chan_multi"
RAW_ROOT = DATA_ROOT / "raw" / "bs5m"
BAR_ROOT = DATA_ROOT / "bars"
BASKET_PATH = DATA_ROOT / "basket_20200630.json"
LOG_PATH = DATA_ROOT / "build_market_5f.log"
EXPECTED_MINUTES = set()
for start in (9 * 60 + 35, 13 * 60 + 5):
    for i in range(24):
        EXPECTED_MINUTES.add(start + i * 5)


def log(message: str) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def to_baostock(ts_code: str) -> str:
    code, market = ts_code.split(".")
    return ("sh." if market.upper() == "SH" else "sz.") + code


def load_basket(path: Path, top: int) -> List[Dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    members = payload["members"][:top]
    return [{"ts_code": m["ts_code"], "weight": float(m["weight"])} for m in members]


def fetch_year(bs, ts_code: str, year: int, force: bool = False) -> Optional[pd.DataFrame]:
    path = RAW_ROOT / ("%s_%d.parquet" % (ts_code.replace(".", "_"), year))
    empty = path.with_suffix(".empty.json")
    if path.exists() and not force:
        try:
            frame = pd.read_parquet(path)
            return frame if len(frame) else None
        except Exception:
            pass
    if empty.exists() and not force:
        return None
    code = to_baostock(ts_code)
    start = "%d-01-01" % year
    end = "%d-12-31" % year
    started = time.time()
    rs = bs.query_history_k_data_plus(
        code, "date,time,open,high,low,close",
        start_date=start, end_date=end, frequency="5", adjustflag="2")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        empty.write_text(json.dumps({"ts_code": ts_code, "year": year,
                                     "reason": "empty", "error": rs.error_msg}),
                         encoding="utf-8")
        log("%s %d empty (%.1fs)" % (ts_code, year, time.time() - started))
        return None
    frame = pd.DataFrame(rows, columns=["date", "time", "open", "high", "low", "close"])
    frame["dt"] = pd.to_datetime(frame["time"].str[:14], format="%Y%m%d%H%M%S", errors="coerce")
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["dt", "open", "high", "low", "close"]).sort_values("dt")
    frame = frame[["dt", "open", "high", "low", "close"]].drop_duplicates("dt", keep="last")
    frame = frame.reset_index(drop=True)
    frame.to_parquet(path, index=False)
    log("%s %d rows=%d (%.1fs)" % (ts_code, year, len(frame), time.time() - started))
    return frame


def download_basket(members: List[Dict], start_year: int, end_year: int,
                    force: bool = False) -> None:
    import baostock as bs
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("baostock login failed: %s %s" % (login.error_code, login.error_msg))
    try:
        for i, member in enumerate(members, 1):
            started = time.time()
            for year in range(start_year, end_year + 1):
                for attempt in range(3):
                    try:
                        fetch_year(bs, member["ts_code"], year, force=force)
                        break
                    except Exception as exc:
                        log("%s %d error (%d/3): %s" % (
                            member["ts_code"], year, attempt + 1, str(exc)[:160]))
                        time.sleep(2 * (attempt + 1))
            log("stock %d/%d %s done in %.1fs" % (
                i, len(members), member["ts_code"], time.time() - started))
    finally:
        bs.logout()


def aggregate(members: List[Dict], min_coverage: float = 0.60) -> Dict:
    total_weight = sum(m["weight"] for m in members)
    weighted_sums = None
    available_weight = None
    per_stock = {}
    for member in members:
        frames = []
        for path in sorted(RAW_ROOT.glob("%s_*.parquet" % member["ts_code"].replace(".", "_"))):
            try:
                frame = pd.read_parquet(path)
                if len(frame):
                    frames.append(frame)
            except Exception:
                continue
        if not frames:
            continue
        stock = pd.concat(frames, ignore_index=True).drop_duplicates("dt", keep="last")
        stock = stock.sort_values("dt").reset_index(drop=True)
        prev = stock["close"].shift(1)
        valid = prev.notna() & (prev > 0)
        if not valid.any():
            continue
        sub = stock.loc[valid].copy()
        base = prev.loc[valid].to_numpy(float)
        for col in ("open", "high", "low", "close"):
            sub[col + "_ret"] = np.clip(sub[col].to_numpy(float) / base - 1.0, -0.20, 0.20)
        sub["weight"] = member["weight"]
        for col in ("open", "high", "low", "close"):
            sub["w_" + col] = sub[col + "_ret"] * sub["weight"]
        sub["ret5"] = stock["close"].pct_change(5).loc[valid].to_numpy(float)
        sub["ma20"] = stock["close"].rolling(20).mean().loc[valid].to_numpy(float)
        sub["_pos"] = (sub["close_ret"] > 0).astype(float)
        sub["_up5"] = (sub["ret5"] > 0).astype(float)
        sub["_up5_n"] = sub["ret5"].notna().astype(float)
        sub["_above20"] = (sub["close"] > sub["ma20"]).astype(float)
        sub["_above20_n"] = sub["ma20"].notna().astype(float)
        sub["_sq"] = sub["close_ret"] ** 2
        per_stock[member["ts_code"]] = len(sub)
        agg = sub.groupby("dt").agg(
            n=("weight", "size"), w=("weight", "sum"),
            so=("w_open", "sum"), sh=("w_high", "sum"),
            sl=("w_low", "sum"), sc=("w_close", "sum"),
            pos_count=("_pos", "sum"), up5_count=("_up5", "sum"),
            up5_n=("_up5_n", "sum"), above20_count=("_above20", "sum"),
            above20_n=("_above20_n", "sum"), ret_sq=("_sq", "sum"),
        )
        weighted_sums = agg if weighted_sums is None else weighted_sums.add(agg, fill_value=0.0)
        log("aggregate %s rows=%d" % (member["ts_code"], len(sub)))

    if weighted_sums is None or weighted_sums.empty:
        raise SystemExit("no 5-minute basket data")

    frame = weighted_sums.sort_index()
    weight = frame["w"].astype(float)
    enough = weight >= total_weight * min_coverage
    frame = frame.loc[enough]
    weight = frame["w"].astype(float)
    n = frame["n"].astype(float)
    rows = []
    level = 1000.0
    for ts, row in frame.iterrows():
        w = float(row["w"])
        if not rows:
            rows.append({"dt": pd.Timestamp(ts), "open": level, "high": level,
                         "low": level, "close": level, "n_stocks": int(row["n"])})
            continue
        o = level * (1.0 + float(row["so"]) / w)
        h = level * (1.0 + float(row["sh"]) / w)
        l = level * (1.0 + float(row["sl"]) / w)
        c = level * (1.0 + float(row["sc"]) / w)
        h = max(h, o, c)
        l = min(l, o, c)
        mean_ret = float(row["sc"]) / w
        dispersion = math.sqrt(max(float(row["ret_sq"]) / float(row["n"]) - mean_ret ** 2, 0.0))
        breadth_up = float(row["pos_count"]) / float(row["n"])
        breadth_5 = (float(row["up5_count"]) / float(row["up5_n"])
                     if float(row["up5_n"]) > 0 else float("nan"))
        breadth_ma20 = (float(row["above20_count"]) / float(row["above20_n"])
                        if float(row["above20_n"]) > 0 else float("nan"))
        rows.append({"dt": pd.Timestamp(ts), "open": o, "high": h, "low": l,
                     "close": c, "n_stocks": int(row["n"]),
                     "breadth_up": breadth_up, "breadth_5": breadth_5,
                     "breadth_ma20": breadth_ma20, "dispersion": dispersion})
        level = c

    out = pd.DataFrame(rows)
    out["minutes"] = out["dt"].dt.hour * 60 + out["dt"].dt.minute
    out = out[out["minutes"].isin(EXPECTED_MINUTES)].drop(columns=["minutes"])
    out = out.reset_index(drop=True)
    BAR_ROOT.mkdir(parents=True, exist_ok=True)
    target = BAR_ROOT / "market_5f.parquet"
    out.to_parquet(target, index=False)
    meta = {
        "kind": "weighted_hs300_top_basket_5f",
        "basket_date": "2020-06-30",
        "basket_members": len(members),
        "basket_weight_pct": round(total_weight, 3),
        "min_coverage": min_coverage,
        "rows": int(len(out)),
        "start": str(out["dt"].iloc[0]),
        "end": str(out["dt"].iloc[-1]),
        "n_stocks_median": float(out["n_stocks"].median()),
        "n_stocks_min": int(out["n_stocks"].min()),
        "n_stocks_max": int(out["n_stocks"].max()),
        "per_stock_rows": per_stock,
        "output": str(target),
        "note": "fixed top-weight HS300 basket; weighted mean of per-stock 5-min returns; qfq (baostock adjustflag=2)",
    }
    (BAR_ROOT / "market_5f.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log("market_5f rows=%d range=%s..%s" % (
        meta["rows"], meta["start"], meta["end"]))
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--basket", type=Path, default=BASKET_PATH)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--aggregate-only", action="store_true")
    parser.add_argument("--min-coverage", type=float, default=0.60)
    args = parser.parse_args()
    members = load_basket(args.basket, args.top)
    if not args.aggregate_only:
        download_basket(members, args.start_year, args.end_year, force=args.force)
    aggregate(members, min_coverage=args.min_coverage)


if __name__ == "__main__":
    main()
