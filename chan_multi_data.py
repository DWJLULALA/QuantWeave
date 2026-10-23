#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resumable data cache for the multi-timeframe Chan engine.

Sources
-------
* 510300.SH 5-minute bars via Tushare ``stk_mins`` (the HS300 ETF is used as
  the intraday market proxy; Tushare does not expose index minute bars on this
  account).
* 000300.SH daily OHLC via ``index_daily`` (the actual HS300 index, used for
  the daily/weekly/monthly levels and for forward-return labels).
* 510300.SH daily OHLC via ``fund_daily`` (diagnostic cross-check only).

The token is read from ``TUSHARE_TOKEN`` or from the existing Kronos runtime
source file.  It is never written to logs or artifacts.

``stk_mins`` on the current Tushare account is limited to one request per
minute.  The downloader therefore sleeps between chunks and is resumable; a
killed process can simply be restarted.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "chan_multi"
RAW_ROOT = DATA_ROOT / "raw"
BAR_ROOT = DATA_ROOT / "bars"
LOG_PATH = DATA_ROOT / "download.log"
STATUS_PATH = DATA_ROOT / "status.json"
TUSHARE_URL = "http://api.tushare.pro"
DEFAULT_TOKEN_SOURCE = Path(
    "/opt/kronos/finetune_lora/"
    "generate_kronos_today_candidates.py"
)

MINS_FIELDS = "ts_code,trade_time,open,close,high,low,vol,amount"
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"


def log(message: str) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def load_token() -> str:
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TUSHARE_PROXY_TOKEN")
    if token:
        return token.strip()
    source = Path(os.getenv("TUSHARE_TOKEN_SOURCE", str(DEFAULT_TOKEN_SOURCE)))
    if source.exists():
        text = source.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"TOKEN\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            return match.group(1).strip()
    raise RuntimeError("Tushare token unavailable: set TUSHARE_TOKEN or TUSHARE_TOKEN_SOURCE")


def _post(payload: Dict, timeout: int = 60) -> Dict:
    response = requests.post(TUSHARE_URL, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api(name: str, params: Dict, fields: str, retries: int = 8,
        rate_sleep: float = 62.0) -> Tuple[List[str], List[List]]:
    token = load_token()
    payload = {"api_name": name, "token": token, "params": params, "fields": fields}
    last_message = ""
    for attempt in range(retries + 1):
        try:
            body = _post(payload)
        except Exception as exc:  # network jitter
            last_message = "%s: %s" % (type(exc).__name__, str(exc)[:180])
            log("request error (%d/%d): %s" % (attempt + 1, retries + 1, last_message))
            time.sleep(min(30, 5 * (attempt + 1)))
            continue
        code = body.get("code")
        message = str(body.get("msg") or "")[:180]
        if code == 0:
            data = body.get("data") or {}
            return list(data.get("fields") or []), list(data.get("items") or [])
        if code == 40203:  # per-minute rate limit
            log("rate limited; sleeping %.0fs" % rate_sleep)
            time.sleep(rate_sleep)
            continue
        if code == 40101:
            raise RuntimeError("Tushare rejected the token: %s" % message)
        last_message = "api_code_%s: %s" % (code, message)
        log("api error (%d/%d): %s" % (attempt + 1, retries + 1, last_message))
        time.sleep(min(30, 5 * (attempt + 1)))
    raise RuntimeError("Tushare request failed after retries: %s" % last_message)


def _chunks(start: date, end: date, months: int = 6) -> List[Tuple[date, date]]:
    out = []
    cursor = start
    while cursor <= end:
        # advance whole months without dateutil
        month = cursor.month - 1 + months
        year = cursor.year + month // 12
        month = month % 12 + 1
        nxt = date(year, month, 1) - timedelta(days=1)
        if nxt > end:
            nxt = end
        out.append((cursor, nxt))
        cursor = nxt + timedelta(days=1)
    return out


def _chunk_path(code: str, freq: str, start: date, end: date) -> Path:
    return RAW_ROOT / ("%s_%s_%s_%s.parquet" % (
        code.replace(".", "_"), freq, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))


def _empty_path(code: str, freq: str, start: date, end: date) -> Path:
    return RAW_ROOT / ("%s_%s_%s_%s.empty.json" % (
        code.replace(".", "_"), freq, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))


def fetch_minutes(code: str, freq: str, start: date, end: date,
                  force: bool = False, rate_sleep: float = 62.0) -> Dict:
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    BAR_ROOT.mkdir(parents=True, exist_ok=True)
    frames: List[pd.DataFrame] = []
    fetched = skipped = empty = 0
    for chunk_start, chunk_end in _chunks(start, end, months=6):
        path = _chunk_path(code, freq, chunk_start, chunk_end)
        empty_marker = _empty_path(code, freq, chunk_start, chunk_end)
        if path.exists() and not force:
            try:
                frame = pd.read_parquet(path)
                if not frame.empty:
                    frames.append(frame)
                skipped += 1
                continue
            except Exception:
                pass
        if empty_marker.exists() and not force:
            empty += 1
            continue
        params = {
            "ts_code": code, "freq": freq,
            "start_date": chunk_start.strftime("%Y-%m-%d") + " 09:00:00",
            "end_date": chunk_end.strftime("%Y-%m-%d") + " 15:00:00",
        }
        fields, items = api("stk_mins", params, MINS_FIELDS, rate_sleep=rate_sleep)
        if not items:
            empty_marker.write_text(json.dumps(
                {"ts_code": code, "freq": freq, "start": str(chunk_start),
                 "end": str(chunk_end), "reason": "empty_items"}), encoding="utf-8")
            empty += 1
            log("%s %s %s..%s empty" % (code, freq, chunk_start, chunk_end))
            time.sleep(max(1.0, rate_sleep))
            continue
        frame = pd.DataFrame(items, columns=fields)
        frame = frame.rename(columns={"trade_time": "dt", "vol": "volume"})
        if "dt" not in frame.columns:
            raise RuntimeError("stk_mins response missing trade_time")
        frame = frame[["dt", "open", "high", "low", "close", "volume", "amount"]].copy()
        frame["dt"] = pd.to_datetime(frame["dt"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["dt", "open", "high", "low", "close"]).sort_values("dt")
        frame.to_parquet(path, index=False)
        frames.append(frame)
        fetched += 1
        log("%s %s %s..%s rows=%d" % (code, freq, chunk_start, chunk_end, len(frame)))
        time.sleep(max(1.0, rate_sleep))

    columns = ["dt", "open", "high", "low", "close", "volume", "amount"]
    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    if not merged.empty:
        merged = merged.drop_duplicates("dt", keep="last").sort_values("dt").reset_index(drop=True)
        target = BAR_ROOT / ("%s_%s.parquet" % (code.replace(".", "_"), freq))
        merged.to_parquet(target, index=False)
    else:
        target = BAR_ROOT / ("%s_%s.parquet" % (code.replace(".", "_"), freq))
    summary = {
        "code": code, "freq": freq, "rows": int(len(merged)),
        "start": str(merged["dt"].iloc[0]) if len(merged) else None,
        "end": str(merged["dt"].iloc[-1]) if len(merged) else None,
        "chunks_fetched": fetched, "chunks_skipped": skipped, "chunks_empty": empty,
        "path": str(target),
    }
    log("combined %s %s rows=%d range=%s..%s" % (
        code, freq, summary["rows"], summary["start"], summary["end"]))
    return summary


def fetch_daily(code: str, api_name: str, start: date, end: date,
                force: bool = False, rate_sleep: float = 2.0) -> Dict:
    BAR_ROOT.mkdir(parents=True, exist_ok=True)
    target = BAR_ROOT / ("%s_daily.parquet" % code.replace(".", "_"))
    if target.exists() and not force:
        frame = pd.read_parquet(target)
        return {"code": code, "api": api_name, "rows": int(len(frame)),
                "start": str(frame["dt"].iloc[0]) if len(frame) else None,
                "end": str(frame["dt"].iloc[-1]) if len(frame) else None,
                "path": str(target), "skipped": True}
    params = {"ts_code": code, "start_date": start.strftime("%Y%m%d"),
              "end_date": end.strftime("%Y%m%d")}
    fields, items = api(api_name, params, DAILY_FIELDS, rate_sleep=rate_sleep)
    if not items:
        raise RuntimeError("%s returned no rows for %s" % (api_name, code))
    frame = pd.DataFrame(items, columns=fields)
    frame = frame.rename(columns={"trade_date": "dt"})
    frame["dt"] = pd.to_datetime(frame["dt"], format="%Y%m%d", errors="coerce")
    for col in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["dt", "open", "high", "low", "close"]).sort_values("dt")
    frame = frame.drop_duplicates("dt", keep="last").reset_index(drop=True)
    frame.to_parquet(target, index=False)
    summary = {"code": code, "api": api_name, "rows": int(len(frame)),
               "start": str(frame["dt"].iloc[0]), "end": str(frame["dt"].iloc[-1]),
               "path": str(target)}
    log("daily %s rows=%d range=%s..%s" % (code, summary["rows"], summary["start"], summary["end"]))
    return summary


def coverage() -> Dict:
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "bars": {}}
    for path in sorted(BAR_ROOT.glob("*.parquet")):
        try:
            frame = pd.read_parquet(path)
            out["bars"][path.stem] = {
                "rows": int(len(frame)),
                "start": str(frame["dt"].iloc[0]) if len(frame) else None,
                "end": str(frame["dt"].iloc[-1]) if len(frame) else None,
                "bytes": path.stat().st_size,
            }
        except Exception as exc:
            out["bars"][path.stem] = {"error": str(exc)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2013-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--code", default="510300.SH")
    parser.add_argument("--freq", default="5min")
    parser.add_argument("--daily", action="store_true", help="also refresh daily index/ETF bars")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--rate-sleep", type=float, default=62.0)
    args = parser.parse_args()

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_ROOT / ".download.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another chan_multi_data downloader is running")

        if args.status:
            summary = coverage()
            STATUS_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return

        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
        summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "intraday": fetch_minutes(args.code, args.freq, start, end,
                                             force=args.force, rate_sleep=args.rate_sleep)}
        if args.daily:
            summary["index_daily"] = fetch_daily("000300.SH", "index_daily",
                                                 date(2005, 1, 1), end, force=args.force)
            summary["etf_daily"] = fetch_daily("510300.SH", "fund_daily",
                                               date(2012, 5, 1), end, force=args.force)
        STATUS_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
