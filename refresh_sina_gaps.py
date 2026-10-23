#!/usr/bin/env python3
"""Paced gap-filler for the 30-minute panel (Sina or Tencent source).

Bulk sweeps of Sina's kline services trip hard IP-level HTTP-456 bans after a
few thousand rapid requests (observed 2026-09-18 on both quotes.sina.cn JSONP
and money.finance.sina.com.cn).  Tencent's ifzq.gtimg.cn m30 kline is a
separate backend with the same bar-END timestamp labels and 手-denominated
volume (×100 = shares, verified against Sina on 000001.SZ 2026-09-17 15:00);
it carries no per-bar amount, which normalize() now tolerates as NaN.

This driver re-fetches ONLY the codes whose newest bar is older than the
target cutoff, in small paced chunks, then merges into the panel with the
same (ts_code, timestamps) dedup as update_intraday_sina_parallel.  Safe to
run repeatedly: already-current codes are skipped, so a second pass after a
cooldown repairs exactly the remaining holes.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

FTL = Path("/opt/kronos/finetune_lora")
if str(FTL) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(FTL))
from update_intraday_sina_direct import normalize, sina_symbol  # noqa: E402


def fetch_tencent(ts_code: str, datalen: int) -> pd.DataFrame:
    """Tencent m30 kline: bar-END labels [ts, open, close, high, low, vol(手)]."""
    sym = sina_symbol(ts_code)
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m30,,{datalen}"
    req = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    body = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore"))
    node = body["data"][sym]
    key = next((k for k in node if "m30" in str(k)), None)
    if key is None:
        raise ValueError(f"no m30 kline in tencent payload: {list(node)}")
    rows = node[key]
    recs = []
    for r in rows:
        ts = str(r[0])
        recs.append({
            "day": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:00",
            "open": r[1], "close": r[2], "high": r[3], "low": r[4],
            "volume": float(r[5]) * 100,  # 手 -> 股 (sina scale)
        })
    return pd.DataFrame(recs)


def fetch_sina_raw(ts_code: str, scale: str, datalen: int) -> pd.DataFrame:
    import update_intraday_sina_direct as sd
    return sd.fetch_sina(sina_symbol(ts_code), scale, datalen)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--panel", required=True, help="panel to repair in place")
    p.add_argument("--as-of-date", required=True)
    p.add_argument("--cutoff", required=True, help="target completed bar, e.g. 2026-09-18 11:30:00")
    p.add_argument("--source", choices=("tencent", "sina"), default="tencent")
    p.add_argument("--scale", default="30")
    p.add_argument("--datalen", type=int, default=240)
    p.add_argument("--chunk", type=int, default=300)
    p.add_argument("--chunk-sleep", type=float, default=12.0)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--rounds", type=int, default=2, help="fetch rounds; later rounds retry failures")
    p.add_argument("--round-sleep", type=float, default=60.0)
    a = p.parse_args()

    panel = Path(a.panel)
    cutoff = pd.Timestamp(a.cutoff)
    old = pd.read_parquet(panel)
    old["timestamps"] = pd.to_datetime(old["timestamps"])
    last_bar = old.groupby("ts_code")["timestamps"].max()
    todo = sorted(last_bar[last_bar < cutoff].index.tolist())
    print(json.dumps({"todo": len(todo), "panel_rows": len(old), "cutoff": a.cutoff,
                      "source": a.source}), flush=True)

    fetch = (lambda c: fetch_tencent(c, a.datalen)) if a.source == "tencent" else \
            (lambda c: fetch_sina_raw(c, a.scale, a.datalen))

    merged = old
    total_fail: dict[str, str] = {}
    for rnd in range(1, a.rounds + 1):
        if not todo:
            break
        if rnd > 1:
            print(json.dumps({"cooldown_s": a.round_sleep}), flush=True)
            time.sleep(a.round_sleep)
        frames, fails = [], {}
        done = 0
        for i in range(0, len(todo), a.chunk):
            batch = todo[i:i + a.chunk]
            with ThreadPoolExecutor(max_workers=a.workers) as pool:
                futs = {c: pool.submit(fetch, c) for c in batch}
                for c, f in futs.items():
                    try:
                        frames.append(normalize(f.result(), c, a.as_of_date))
                    except Exception as exc:
                        fails[c] = f"{type(exc).__name__}: {str(exc)[:120]}"
            done += len(batch)
            print(json.dumps({"round": rnd, "done": done, "total": len(todo),
                              "frames": len(frames), "fails": len(fails)}), flush=True)
            if i + a.chunk < len(todo):
                time.sleep(a.chunk_sleep)
        if frames:
            new = pd.concat(frames, ignore_index=True)
            merged = pd.concat([merged, new], ignore_index=True)
            merged["timestamps"] = pd.to_datetime(merged["timestamps"])
            merged = (merged.drop_duplicates(subset=["ts_code", "timestamps"], keep="last")
                      .sort_values(["ts_code", "timestamps"]).reset_index(drop=True))
        total_fail.update(fails)
        todo = sorted(fails)
    # persist even when some codes still fail: fresh codes must not be lost
    tmp = panel.with_suffix(".tmp.parquet")
    merged.to_parquet(tmp, index=False)
    tmp.replace(panel)
    merged["timestamps"] = pd.to_datetime(merged["timestamps"])
    last_bar = merged.groupby("ts_code")["timestamps"].max()
    summary = {
        "panel": str(panel), "cutoff": a.cutoff, "source": a.source, "rows": int(len(merged)),
        "fresh_codes": int((last_bar >= cutoff).sum()),
        "stale_codes": int((last_bar < cutoff).sum()),
        "failures": {k: total_fail[k] for k in sorted(total_fail)[:30]},
        "failure_count": len(total_fail),
    }
    panel.with_suffix(".gapfill_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
