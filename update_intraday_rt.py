#!/usr/bin/env python3
"""Refresh a 30-minute panel through the Tushare rt_min realtime API.

rt_min takes comma-separated codes (<=300 companies per call, <=1000 rows per
call), so the whole market needs ~47 chunked calls instead of 5,525 single-stock
requests. The token is resolved from TUSHARE_PROXY_TOKEN/TUSHARE_TOKEN or the
existing backfill token file; it is never written to output, summaries, or logs.

`--probe` runs one chunked call for three codes and exits 0 when the realtime
permission is active, 3 when the server answers 403, 4 without a token.
"""
from __future__ import annotations
import argparse, json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
import requests

API_URL = "https://t.xiaodefa.top/"
CHUNK = 120  # 120 codes * <=8 daily 30MIN bars = 960 rows < 1000-row cap
COLS = ["ts_code", "timestamps", "open", "high", "low", "close", "volume", "amount"]


def args():
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", default="/opt/kronos")
    p.add_argument("--input-panel", default="")
    p.add_argument("--output-panel", default="")
    p.add_argument("--as-of-date", required=True)
    p.add_argument("--cutoff", default="")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--limit-codes", type=int, default=0)
    p.add_argument("--token-file", default="")
    p.add_argument("--probe", action="store_true")
    return p.parse_args()


def token(a) -> str:
    v = os.getenv("TUSHARE_PROXY_TOKEN") or os.getenv("TUSHARE_TOKEN")
    if v:
        return v
    path = Path(a.token_file) if a.token_file else Path(a.project_root) / "finetune_lora" / "data" / "astock_30min_backfill_2000" / "token.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def safe_message(value, secret):
    text = str(value).replace(secret, "[REDACTED]") if secret else str(value)
    return re.sub(r"(?i)token[^\s,，;；]{0,100}", "token=[REDACTED]", text)[:240]


def fetch_chunk(codes, t, as_of, cutoff, retries):
    payload = {"api_name": "rt_min", "token": t,
               "params": {"ts_code": ",".join(codes), "freq": "30MIN"},
               "fields": "ts_code,time,open,close,high,low,vol,amount"}
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(API_URL, json=payload, headers={"Accept-Encoding": "gzip"}, timeout=60)
            r.raise_for_status()
            body = r.json()
            if body.get("code") == 403:
                return None, f"permission_denied: {safe_message(body.get('msg', ''), t)}", True
            if body.get("code") != 0:
                return None, f"api_code_{body.get('code')}: {safe_message(body.get('msg', ''), t)}", False
            data = body.get("data") or {}
            frame = pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])
            if frame.empty:
                return pd.DataFrame(columns=COLS), None, False
            frame = frame.rename(columns={"time": "timestamps", "vol": "volume"})
            frame["timestamps"] = pd.to_datetime(frame["timestamps"], errors="coerce")
            for c in ["open", "high", "low", "close", "volume", "amount"]:
                frame[c] = pd.to_numeric(frame[c], errors="coerce")
            frame = frame.dropna(subset=["timestamps"] + [c for c in COLS if c != "timestamps"])
            day_start = pd.Timestamp(f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]} 09:00:00")
            frame = frame[frame["timestamps"] >= day_start]
            if cutoff:
                frame = frame[frame["timestamps"] <= pd.Timestamp(cutoff)]
            return frame[COLS], None, False
        except Exception as e:
            msg = str(e).replace(t, "[REDACTED]") if t else str(e)
            last = f"{type(e).__name__}: {safe_message(msg, t)}"
            if attempt < retries:
                time.sleep(0.7 * (attempt + 1))
    return None, last, False


def main():
    a = args()
    t = token(a)
    if a.probe:
        if not t:
            print(json.dumps({"probe": "no_token"}), flush=True)
            return 4
        frame, error, denied = fetch_chunk(["600000.SH", "000001.SZ", "300750.SZ"], t, a.as_of_date, "", 1)
        if denied or (error and "permission_denied" in str(error)):
            print(json.dumps({"probe": "denied", "msg": safe_message(error, t)}), flush=True)
            return 3
        if error:
            print(json.dumps({"probe": "error", "msg": safe_message(error, t)}), flush=True)
            return 5
        print(json.dumps({"probe": "ok", "rows": int(len(frame))}), flush=True)
        return 0

    if not a.input_panel:
        print("error: --input-panel is required for full refresh", flush=True)
        return 2
    old = pd.read_parquet(a.input_panel)
    codes = sorted(old["ts_code"].dropna().astype(str).unique())
    if a.limit_codes > 0:
        codes = codes[: a.limit_codes]
    chunks = [codes[i:i + CHUNK] for i in range(0, len(codes), CHUNK)]
    frames, failures = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(a.workers, 8))) as pool:
        futs = {pool.submit(fetch_chunk, ch, t, a.as_of_date, a.cutoff, a.retries): ch for ch in chunks}
        for idx, fut in enumerate(as_completed(futs), 1):
            frame, error, denied = fut.result()
            if denied:
                print(json.dumps({"abort": "permission_denied", "done": idx, "total": len(chunks)}, ensure_ascii=False), flush=True)
                raise SystemExit(3)
            if frame is not None and not frame.empty:
                frames.append(frame)
            if error:
                failures.append({"codes": futs[fut][0], "error": error})
            if idx % 10 == 0 or idx == len(chunks):
                print({"done": idx, "total": len(chunks), "frames": len(frames), "failures": len(failures)}, flush=True)
    new = pd.concat(frames, ignore_index=True) if frames else old.iloc[:0].copy()
    combined = pd.concat([old, new], ignore_index=True)
    combined["timestamps"] = pd.to_datetime(combined["timestamps"])
    combined = combined.drop_duplicates(["ts_code", "timestamps"], keep="last").sort_values(["ts_code", "timestamps"]).reset_index(drop=True)
    out = Path(a.output_panel)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.parquet")
    combined.to_parquet(tmp, index=False)
    tmp.replace(out)
    max_ts = combined["timestamps"].max()
    summary = {"source": "tushare_rt_min", "input_panel": str(a.input_panel), "output_panel": str(out),
               "codes": len(codes), "chunks": len(chunks), "old_rows": len(old), "new_rows_fetched": len(new),
               "combined_rows": len(combined), "max_timestamp": str(max_ts),
               "latest_codes": int(combined.loc[combined["timestamps"] == max_ts, "ts_code"].nunique()),
               "failure_count": len(failures), "failures": failures[:30]}
    out.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
