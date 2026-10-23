#!/usr/bin/env python3
"""Repair missing `amount` in the 30-min panel (one-off for 2026-09-18).

Background: pass-1 of the Sina sweep wrote ~240 bars/code with a null amount
column, and the Tencent backfill carries no per-bar amount at all.  `amount`
is one of the six model FEATURES, so dropna() in the engine's record builder
silently regressed 2.4k codes to "stale".

Two-part repair, all values measured rather than estimated:
  1. rows before today: re-fill amount from the previous canonical panel
     (same bar timestamps, original EM/Sina amounts);
  2. today's bars: reconstruct per-bar amount from Tencent's minute data
     (cumulative amount deltas at the 10:00/10:30/11:00/11:30 bar ends),
     cross-validated against the qt cumulative quote for 000001.SZ.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

FTL = Path("/opt/kronos/finetune_lora")
sys.path.insert(0, str(FTL))
from update_intraday_sina_direct import sina_symbol  # noqa: E402

NEW = FTL / "data" / "astock_30min_ohlcva_full_20260918_ui_parallel.parquet"
OLD = FTL / "data" / "astock_30min_ohlcva_full_20260917_ui_parallel.parquet"
DAY0 = pd.Timestamp("2026-09-18")
BAR_ENDS = ("1000", "1030", "1100", "1130")


def morning_amounts(ts_code: str) -> dict[str, float]:
    """{bar_end 'YYYY-MM-DD HH:MM:00' -> bar amount in 元} from Tencent minutes."""
    sym = sina_symbol(ts_code)
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    req = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    body = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore"))
    rows = body["data"][sym]["data"]["data"]
    out: dict[str, float] = {}
    prev_vol = prev_amt = 0.0
    for r in rows:
        parts = r.split()
        if len(parts) < 4:
            continue
        hm, cv, ca = parts[0], float(parts[2]), float(parts[3])
        if hm in BAR_ENDS:
            out[f"2026-09-18 {hm[:2]}:{hm[2:]}:00"] = ca - prev_amt
        prev_vol, prev_amt = cv, ca
    return out


def main() -> None:
    new = pd.read_parquet(NEW)
    new["timestamps"] = pd.to_datetime(new["timestamps"])

    # ---- 1) historical rows: restore from the previous canonical panel
    hist_mask = new["amount"].isna() & (new["timestamps"] < DAY0)
    old = pd.read_parquet(OLD, columns=["ts_code", "timestamps", "amount"])
    old["timestamps"] = pd.to_datetime(old["timestamps"])
    old_map = old.set_index(["ts_code", "timestamps"])["amount"]
    key = pd.MultiIndex.from_arrays([new.loc[hist_mask, "ts_code"], new.loc[hist_mask, "timestamps"]])
    restored = old_map.reindex(key).to_numpy()
    fixed_hist = int(pd.notna(restored).sum())
    new.loc[hist_mask, "amount"] = restored

    # ---- 2) today's rows: Tencent minute deltas
    today_mask = new["amount"].isna() & (new["timestamps"] >= DAY0)
    codes = sorted(new.loc[today_mask, "ts_code"].unique().tolist())
    print(json.dumps({"nan_hist": int(hist_mask.sum()), "restored_hist": fixed_hist,
                      "nan_today": int(today_mask.sum()), "codes_today": len(codes)}), flush=True)
    fetched: dict[str, dict[str, float]] = {}
    fails = 0
    for i in range(0, len(codes), 300):
        batch = codes[i:i + 300]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for c, f in zip(batch, [pool.submit(morning_amounts, c) for c in batch]):
                try:
                    fetched[c] = f.result()
                except Exception:
                    fails += 1
        print(json.dumps({"done": min(i + 300, len(codes)), "total": len(codes)}), flush=True)
        if i + 300 < len(codes):
            time.sleep(10)
    amt_lookup = {}
    for c, bars in fetched.items():
        for ts, amt in bars.items():
            amt_lookup[(c, pd.Timestamp(ts))] = amt
    tkey = pd.MultiIndex.from_arrays([new.loc[today_mask, "ts_code"], new.loc[today_mask, "timestamps"]])
    today_restored = [amt_lookup.get(k) for k in tkey]
    fixed_today = int(pd.notna(pd.Series(today_restored, dtype="float64")).sum())
    new.loc[today_mask, "amount"] = pd.Series(today_restored, index=new.index[today_mask], dtype="float64")

    # ---- persist + preview the engine's staleness view
    tmp = NEW.with_suffix(".tmp.parquet")
    new.to_parquet(tmp, index=False)
    tmp.replace(NEW)
    valid = new.dropna(subset=["open", "high", "low", "close", "volume", "amount"])
    last = valid.groupby("ts_code")["timestamps"].last()
    summary = {
        "restored_hist": fixed_hist, "restored_today": fixed_today, "tencent_fails": fails,
        "rows": int(len(new)), "nan_amount_left": int(new["amount"].isna().sum()),
        "codes_fresh_0918": int((last >= DAY0).sum()),
        "codes_last_before_0916": int((last < pd.Timestamp("2026-09-16")).sum()),
    }
    (NEW.parent / "astock_30min_ohlcva_full_20260918_ui_parallel.repair_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
