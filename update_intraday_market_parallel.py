#!/usr/bin/env python3
"""Incremental 30-minute refresh through EastMoney.

Sina frequently returns HTTP 456 for the shared server IP. Failed symbols
retain their old bars, so a partial refresh remains usable.
"""
from __future__ import annotations
import argparse, json, random, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
import requests

URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
COLS = ["ts_code", "timestamps", "open", "high", "low", "close", "volume", "amount"]

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input-panel", required=True); p.add_argument("--output-panel", required=True)
    p.add_argument("--as-of-date", required=True); p.add_argument("--scale", default="30")
    p.add_argument("--datalen", type=int, default=1970); p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit-codes", type=int, default=0); p.add_argument("--retries", type=int, default=3)
    p.add_argument("--cutoff", default="", help="只保留已完成 bar，格式 YYYY-MM-DD HH:MM")
    return p.parse_args()

def secid(code):
    symbol, market = str(code).split(".")
    return ("1" if market.upper() == "SH" else "0") + "." + symbol

def fetch_one(code, args):
    last = None
    for attempt in range(args.retries + 1):
        try:
            params = {"secid": secid(code), "klt": args.scale, "fqt": "0", "beg": "19900101",
                      "end": args.as_of_date, "lmt": str(args.datalen),
                      "fields1": "f1,f2,f3,f4,f5,f6",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                      "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
            response = requests.get(URL, params=params, headers=HEADERS, timeout=15)
            response.raise_for_status()
            lines = ((response.json().get("data") or {}).get("klines") or [])
            rows = []
            for line in lines:
                parts = str(line).split(",")
                if len(parts) >= 7:
                    rows.append([code, parts[0], parts[1], parts[3], parts[4], parts[2], parts[5], parts[6]])
            frame = pd.DataFrame(rows, columns=COLS)
            if frame.empty: raise ValueError("EastMoney returned no kline rows")
            frame["timestamps"] = pd.to_datetime(frame["timestamps"], errors="coerce")
            for col in COLS[2:]: frame[col] = pd.to_numeric(frame[col], errors="coerce")
            # EastMoney volume is expressed in lots (手); the project panel
            # convention is shares, matching the Sina/Tushare feed.
            frame["volume"] = frame["volume"] * 100.0
            frame = frame[frame["timestamps"].dt.strftime("%Y%m%d") <= args.as_of_date]
            if args.cutoff:
                frame = frame[frame["timestamps"] <= pd.Timestamp(args.cutoff)]
            return code, frame.dropna(subset=["timestamps", "open", "high", "low", "close"]), None
        except Exception as exc:
            last = exc
            # A shared Sina/EastMoney egress can return HTTP 456 or close the
            # connection for a symbol. Retrying those repeatedly blocks the
            # next completed bar, so record the failure and move on quickly.
            if getattr(getattr(exc, "response", None), "status_code", None) == 456 or "456" in str(exc):
                break
            if attempt < args.retries: time.sleep(min(8.0, 0.8 * (2 ** attempt)) + random.random() * 0.3)
    return code, None, f"{type(last).__name__}: {last}"

def main():
    args = parse_args(); old = pd.read_parquet(args.input_panel)
    codes = sorted(old["ts_code"].dropna().astype(str).unique())
    if args.limit_codes > 0: codes = codes[:args.limit_codes]
    frames, failures = [], []
    workers = max(1, min(int(args.workers), 16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, code, args) for code in codes]
        for idx, future in enumerate(as_completed(futures), 1):
            code, frame, error = future.result()
            if error: failures.append({"ts_code": code, "error": error})
            elif frame is not None and not frame.empty: frames.append(frame)
            if idx % 100 == 0 or idx == len(codes): print({"done": idx, "total": len(codes), "frames": len(frames), "failures": len(failures)}, flush=True)
    new = pd.concat(frames, ignore_index=True) if frames else old.iloc[:0].copy()
    combined = pd.concat([old, new], ignore_index=True)
    combined["timestamps"] = pd.to_datetime(combined["timestamps"])
    # Never carry a later bar from a previously generated same-day panel into
    # an earlier as-of refresh.  Otherwise the summary can claim freshness
    # beyond the requested cutoff and the model would see future intraday data.
    if args.cutoff:
        combined = combined[combined["timestamps"] <= pd.Timestamp(args.cutoff)]
    combined = combined.drop_duplicates(["ts_code", "timestamps"], keep="last").sort_values(["ts_code", "timestamps"]).reset_index(drop=True)
    out = Path(args.output_panel); out.parent.mkdir(parents=True, exist_ok=True); combined.to_parquet(out, index=False)
    max_ts = combined["timestamps"].max()
    summary = {"source": "eastmoney", "input_panel": str(args.input_panel), "output_panel": str(out),
               "codes": len(codes), "old_rows": len(old), "new_rows_fetched": len(new),
               "combined_rows": len(combined), "max_timestamp": str(max_ts),
               "latest_codes": int(combined.loc[combined["timestamps"] == max_ts, "ts_code"].nunique()),
               "failure_count": len(failures), "failures": failures[:30]}
    out.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__": main()
