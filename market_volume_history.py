#!/usr/bin/env python3
"""Market/industry turnover history from the canonical 30-minute panel.

Aggregates the trailing trading days of the canonical panel into daily market
volume/amount and per-industry volume series.  Industry membership comes from
the newest published intraday prediction CSV (ts_code -> industry).  Read-only
research output; prints a single JSON document.
"""
from __future__ import annotations
import argparse, glob, json, os
from pathlib import Path
import pandas as pd


def resolve_panel(root: Path) -> Path | None:
    data = root / "finetune_lora" / "data"
    paths = []
    for p in data.glob("astock_30min_ohlcva_full_*.parquet"):
        name = p.name.lower()
        if "_em_" in name or "_tushare_" in name or "backfill" in name:
            continue
        paths.append(p)
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def limit_threshold(code: str) -> float:
    """Limit-up daily-return threshold by board (rounded price ⇒ slightly under nominal)."""
    sym = str(code).split(".")[0]
    if sym.startswith(("300", "301", "688", "689")):
        return 0.197
    if sym.startswith(("43", "83", "87", "92")):
        return 0.297
    return 0.097


def build_streaks(px, rets, industry_map, name_map, days):
    """Consecutive limit-up chains per stock and consecutive up-days per industry.

    All streaks end at the latest day in the window.  NaN returns (suspension or
    missing bar) break a chain.  Close-to-close on the 15:00 bar, i.e. the same
    daily-return definition as the rest of this tool.
    """
    thr = pd.Series({c: limit_threshold(c) for c in px.index})
    hit = rets.ge(thr, axis=0)
    chains = {}
    for code in px.index:
        n = 0
        for d in reversed(days):
            v = rets.at[code, d]
            if pd.isna(v) or not bool(hit.at[code, d]):
                break
            n += 1
        if n >= 2:
            chains[code] = n
    ind_of = {c: str(industry_map.get(c) or "未知") for c in px.index}
    rows = []
    for ind in sorted(set(ind_of.values())):
        members = [c for c in px.index if ind_of[c] == ind]
        if not members:
            continue
        limit_today = int(hit.loc[members, days[-1]].sum())
        ret_today = float(rets.loc[members, days[-1]].mean()) if len(days) > 1 else None
        up_streak, up_cum = 0, 0.0
        for d in reversed(days):
            m = float(rets.loc[members, d].mean())
            if pd.isna(m) or m <= 0:
                break
            up_streak += 1
            up_cum += m
        heat = [int(hit.loc[members, d].sum()) for d in days]
        cs = sorted(
            ({"code": c, "name": name_map.get(c, c), "chain": chains[c]} for c in members if c in chains),
            key=lambda x: -x["chain"])
        rows.append({
            "industry": ind, "stocks": len(members), "limit_today": limit_today,
            "ret_today": ret_today, "up_streak": up_streak,
            "up_cum": round(up_cum, 4) if up_streak else 0.0,
            "heat": heat, "chain_stocks": cs,
        })
    rows.sort(key=lambda r: (-len(r["chain_stocks"]), -r["limit_today"], -r["up_streak"], -(r["ret_today"] or 0)))
    stock_rows = sorted(
        ({"code": c, "name": name_map.get(c, c), "industry": ind_of[c], "chain": n}
         for c, n in chains.items()),
        key=lambda x: -x["chain"])
    return {"asof": days[-1], "industries": rows, "stocks": stock_rows,
            "market_limit_today": int(hit[days[-1]].sum())}


def latest_industry_map(runs_dir: Path) -> dict:
    csvs = sorted(glob.glob(str(runs_dir / "intraday_live_*" / "intraday_next_session_predictions.csv")), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    for csv in csvs:
        try:
            f = pd.read_csv(csv, usecols=lambda c: c in {"ts_code", "industry"})
        except Exception:
            continue
        f = f.dropna(subset=["ts_code"])
        f["industry"] = f.get("industry", "").fillna("").astype(str).str.strip()
        f = f[f["industry"] != ""]
        if len(f):
            return dict(zip(f["ts_code"], f["industry"]))
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default="/opt/kronos")
    ap.add_argument("--runs-dir", default="/opt/quantweave/runs")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top-industries", type=int, default=40)
    a = ap.parse_args()
    root, runs_dir = Path(a.project_root), Path(a.runs_dir)
    panel = resolve_panel(root)
    if panel is None:
        print(json.dumps({"available": False, "reason": "未找到规范分时面板"}))
        return

    import pyarrow.parquet as pq
    pf = pq.ParquetFile(panel)
    ts_idx = pf.schema.names.index("timestamps")
    stats_max = []
    for i in range(pf.num_row_groups):
        st = pf.metadata.row_group(i).column(ts_idx).statistics
        if st and st.max is not None:
            stats_max.append(pd.Timestamp(st.max))
    if not stats_max:
        print(json.dumps({"available": False, "reason": "面板没有时间戳"}))
        return
    hi = max(stats_max)
    lo = hi - pd.Timedelta(days=int(a.days) * 2 + 6)

    frames = []
    for i in range(pf.num_row_groups):
        st = pf.metadata.row_group(i).column(ts_idx).statistics
        if st and st.max is not None and pd.Timestamp(st.max) < lo:
            continue
        frames.append(pf.read_row_group(i, columns=["ts_code", "timestamps", "volume", "amount", "close"]).to_pandas())
    f = pd.concat(frames, ignore_index=True)
    f["timestamps"] = pd.to_datetime(f["timestamps"], errors="coerce")
    f["volume"] = pd.to_numeric(f["volume"], errors="coerce")
    f["amount"] = pd.to_numeric(f["amount"], errors="coerce")
    f = f.dropna(subset=["timestamps"])
    f["day"] = f["timestamps"].dt.strftime("%Y-%m-%d")
    days = sorted(f["day"].unique())[-int(a.days):]
    f = f[f["day"].isin(days)]

    market = f.groupby("day").agg(volume=("volume", "sum"), amount=("amount", "sum"), stocks=("ts_code", "nunique")).reset_index()
    # industry daily equal-weight return from 15:00 close vs previous session close
    daily_close = f.sort_values("timestamps").groupby(["ts_code", "day"], as_index=False)["close"].last()
    px = daily_close.pivot(index="ts_code", columns="day", values="close").sort_index(axis=1)
    rets = px.pct_change(axis=1).stack().rename("ret").reset_index()
    industry_map = latest_industry_map(runs_dir)
    f["industry"] = f["ts_code"].map(industry_map).fillna("未知")
    ind = f.groupby(["industry", "day"]).agg(volume=("volume", "sum")).reset_index()
    ind_ret = rets.merge(f[["ts_code", "industry"]].drop_duplicates(), on="ts_code", how="left")
    ind_ret["industry"] = ind_ret["industry"].fillna("未知")
    ret_lookup = ind_ret.groupby(["industry", "day"])["ret"].mean()
    market_ret = ind_ret.groupby("day")["ret"].mean().reindex(days)
    top_names = (f[f["day"] == days[-1]].groupby("industry")["volume"].sum().sort_values(ascending=False).head(int(a.top_industries)).index.tolist())
    industries = {}
    for name in top_names:
        g = ind[ind["industry"] == name].set_index("day").reindex(days)
        industries[name] = [None if pd.isna(v) else float(v) for v in g["volume"]]
    other = ind[~ind["industry"].isin(top_names)].groupby("day")["volume"].sum().reindex(days)
    industries["其余行业合计"] = [None if pd.isna(v) else float(v) for v in other]

    industry_daily = {}
    amounts = f.groupby(["industry", "day"])["amount"].sum()
    for name in industries:
        industry_daily[name] = {
            "ret": [None if pd.isna(ret_lookup.get((name, d))) else float(ret_lookup.get((name, d))) for d in days],
            "volume": industries[name],
            "amount": [None if pd.isna(amounts.get((name, d))) else float(amounts.get((name, d))) for d in days],
        }
    # limit-up chains & industry strength streaks (need per-stock close matrix)
    streaks = None
    try:
        name_map = {}
        basic_path = os.getenv("STOCKLAB_STOCK_BASIC", "/opt/kronos/factor_horizon/shadow_live/data/stock_basic_all.parquet")
        if Path(basic_path).exists():
            basic = pd.read_parquet(basic_path, columns=["ts_code", "name"])
            name_map = dict(zip(basic["ts_code"], basic["name"].astype(str)))
        px_full = daily_close.pivot(index="ts_code", columns="day", values="close").sort_index(axis=1)
        rets_full = px_full.pct_change(axis=1)
        streaks = build_streaks(px_full, rets_full, industry_map, name_map, days)
    except Exception as exc:  # streaks are additive; never break the volume payload
        streaks = {"error": f"{type(exc).__name__}: {exc}"}
    payload = {
        "available": True,
        "panel": str(panel),
        "streaks": streaks,
        "dates": days,
        "market": [{"date": r.day, "volume": float(r.volume), "amount": float(r.amount), "stocks": int(r.stocks)} for r in market.itertuples()],
        "market_ret": [None if pd.isna(v) else float(v) for v in market_ret],
        "industries": industries,
        "industry_daily": industry_daily,
        "top_industries": top_names,
        "industry_coverage": float(f["ts_code"].map(lambda c: c in industry_map).mean()),
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
