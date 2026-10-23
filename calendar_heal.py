#!/usr/bin/env python3
"""补齐交易日历缓存：查询 Tushare trade_cal 近 N 天，写入缺失的 SSE_*.parquet。

用途：数据更新链路依赖每日顺带生成的日历文件；一旦某天更新被跳过，
日历出现缺口会让后续规划器 blocked（calendar_unknown/不能跨缺口）。
本脚本按官方 trade_cal 补齐缺口，是流水线守卫前的自愈步骤。
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path("/opt/kronos/finetune_lora/stagewise_rankic_v3")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from update_data_through_date import API_URL, TushareHTTP, resolve_token  # noqa: E402

CAL_DIR = Path("/opt/kronos/factor_horizon/shadow_live/data/cache/trade_cal")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end-date", required=True, help="YYYYMMDD")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--token-file", type=Path)
    ap.add_argument("--legacy-token-source", type=Path)
    ap.add_argument("--api-url", default=API_URL)
    args = ap.parse_args()
    start = (pd.Timestamp(args.end_date) - pd.Timedelta(days=args.days)).strftime("%Y%m%d")
    token = resolve_token(args)
    client = TushareHTTP(token, args.api_url)
    cal = client.query(
        "trade_cal",
        {"exchange": "SSE", "start_date": start, "end_date": args.end_date},
        "cal_date,is_open,pretrade_date",
    )
    if cal.empty:
        raise RuntimeError("trade_cal returned empty")
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for _, row in cal.iterrows():
        d = str(row["cal_date"])
        target = CAL_DIR / f"SSE_{d}_{d}.parquet"
        if target.exists():
            continue
        df = pd.DataFrame([{"exchange": "SSE", "cal_date": d,
                            "is_open": int(row["is_open"]),
                            "pretrade_date": str(row.get("pretrade_date", ""))}])
        df.to_parquet(target)
        written.append(d)
    print(json.dumps({"written": written}, ensure_ascii=False))


if __name__ == "__main__":
    main()
