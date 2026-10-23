#!/usr/bin/env python3
"""Kronos intraday timing and paper-position monitor.

Reads the frozen OOS prediction parquet (no model inference), calculates a causal
EMA/rank, emits the executable A-share T+1 timing rules, and appends each run to
an audit history.  It is deliberately read-only with respect to broker accounts.
"""
from __future__ import annotations
import argparse, json, glob, hashlib
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

DEFAULT_ROOT = "/opt/kronos"
DEFAULT_OUT = "/opt/quantweave/runs/intraday"

def clean(v):
    if v is None: return None
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, (np.integer,)): return int(v)
    if pd.isna(v): return None
    return v.item() if hasattr(v, "item") else str(v) if isinstance(v, pd.Timestamp) else v

def prediction_files(root: Path, explicit: str | None = None):
    if explicit and Path(explicit).exists(): return [Path(explicit)]
    base = root / "finetune_lora" / "output"
    paths = sorted(base.glob("oos_intraday*/summary_lora/oos_predictions_with_trade_filter.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    if paths: return [paths[0]]
    paths = sorted(base.glob("oos_intraday*/lora_shard*/oos_predictions_raw.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:2]

def load_predictions(root: Path, explicit: str | None = None):
    paths = prediction_files(root, explicit)
    if not paths: raise FileNotFoundError("未找到 Kronos 分时预测 parquet")
    cols = ["ts_code","context_end_ts","context_end_date","pred_end_ts","target_trade_date","pred_return","pred_return_last","pred_return_std","last_close","pred_close_path","actual_return","trade_open","trade_close","trade_buyable","trade_open_close_return"]
    frames=[]
    for p in paths:
        if p.suffix.lower() == ".csv":
            f = pd.read_csv(p)
            if "context_end_ts" not in f.columns and "observed_latest_bar" in f.columns:
                f["context_end_ts"] = f["observed_latest_bar"]
            for c in ("pred_end_ts", "target_trade_date", "actual_return", "trade_open", "trade_close", "trade_buyable", "trade_open_close_return"):
                if c not in f.columns: f[c] = np.nan
            frames.append(f[[c for c in cols if c in f.columns]])
        else:
            use=[c for c in cols if c in pd.read_parquet(p, engine="pyarrow").columns]
            frames.append(pd.read_parquet(p, columns=use, engine="pyarrow"))
    d=pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code","context_end_ts"])
    d["context_end_ts"]=pd.to_datetime(d.context_end_ts); d["context_end_date"]=d.context_end_ts.dt.strftime("%Y-%m-%d"); d["pred_end_ts"]=pd.to_datetime(d["pred_end_ts"], errors="coerce") if "pred_end_ts" in d else pd.NaT
    # The live CSV has a target session but intentionally no realized label.
    # Keep it future/unmatured until that session has actually ended.
    if "target_trade_date" in d:
        target = pd.to_datetime(d["target_trade_date"], errors="coerce")
        d["pred_end_ts"] = d["pred_end_ts"].fillna(target + pd.Timedelta(hours=15))
    d["pred_return"]=pd.to_numeric(d.pred_return, errors="coerce")
    d=d.sort_values(["ts_code","context_end_ts"]).dropna(subset=["pred_return"])
    return d, [str(p) for p in paths]

def causal_ema(d, alpha):
    d=d.copy(); d["ema_signal"]=d.groupby("ts_code", sort=False)["pred_return"].transform(lambda s: s.ewm(alpha=alpha, adjust=False).mean())
    d["signal_rank"]=d.groupby("context_end_ts")["ema_signal"].rank(pct=True)
    return d

def _panel_path(root: Path, predictions: str | None):
    """Resolve the completed panel recorded by the live inference manifest."""
    if predictions:
        p = Path(predictions).with_name("manifest.json")
        if p.exists():
            try:
                value = json.loads(p.read_text(encoding="utf-8")).get("panel_path")
                if value and Path(value).exists(): return Path(value)
            except Exception: pass
    panels = sorted((root / "finetune_lora" / "data").glob("astock_30min_ohlcva_full_*.parquet"), key=lambda x: x.stat().st_mtime)
    return panels[-1] if panels else None

def _position_market(root: Path, predictions: str | None, code: str, entry_day: str, asof_ts: pd.Timestamp):
    """Count completed bars from the actual panel, enforcing T+1."""
    panel = _panel_path(root, predictions)
    if panel is None or not entry_day: return None, None
    try:
        f = pd.read_parquet(panel, columns=["ts_code", "timestamps", "close"], filters=[["ts_code", "=", code]])
        if f.empty: return None, None
        f["timestamps"] = pd.to_datetime(f["timestamps"]); f["close"] = pd.to_numeric(f["close"], errors="coerce")
        f = f[(f["timestamps"] <= asof_ts) & (f["close"] > 0)].sort_values("timestamps")
        if f.empty: return None, None
        entry_cut = pd.Timestamp(entry_day) + pd.Timedelta(hours=15)
        completed = f[f["timestamps"] > entry_cut]
        return int(len(completed)), clean(f.iloc[-1]["close"])
    except Exception:
        return None, None

def timing(root, outdir, date=None, alpha=.25, topk=5, positions_path=None, predictions=None, state_dir=None):
    d, sources=load_predictions(root, predictions); latest=str(date or d.context_end_date.max())[:10]
    if date and len(latest)==8: latest=f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
    d=causal_ema(d, alpha); day=d[d.context_end_date==latest].copy()
    if day.empty: raise ValueError(f"没有 {latest} 的分时预测")
    # 14:30 signal is the documented executable leg; fall back to the latest bar.
    day["bar_hm"]=day.context_end_ts.dt.strftime("%H:%M")
    has_1430=bool((day.bar_hm=="14:30").any())
    entry=day[day.bar_hm=="14:30"].copy() if has_1430 else day.sort_values("context_end_ts").groupby("ts_code",as_index=False).tail(1)
    entry=entry.sort_values(["ema_signal","signal_rank"],ascending=False).drop_duplicates("ts_code")
    entry["buy_action"]=np.where(has_1430 & (entry.ema_signal>0)&(entry.signal_rank>=.95),"BUY_TAIL_14:45-14:55","WATCH")
    entry["stability_status"]="unverified"
    # Matured rows provide the only honest verification signal; never use future labels for today's action.
    asof_ts=day.context_end_ts.max(); hist=d[(d.pred_end_ts<=asof_ts)&d.actual_return.notna()].copy() if "actual_return" in d else pd.DataFrame()
    if not hist.empty:
        hs=hist.groupby("ts_code").agg(verified_n=("actual_return","size"),verified_win=("actual_return",lambda s: float((s>0).mean())),verified_mean=("actual_return","mean"),recent_ema=("ema_signal","last")).reset_index()
        entry=entry.merge(hs,on="ts_code",how="left")
        stable=(entry.verified_n.fillna(0)>=3)&(entry.verified_win.fillna(0)>=.55)&(entry.verified_mean.fillna(0)>0)&(entry.ema_signal>0)
        broken=(entry.ema_signal<0)|(entry.verified_n.fillna(0)>=3)&(entry.verified_win.fillna(0)<.4)
        entry.loc[stable,"stability_status"]="stable_verified"; entry.loc[broken,"stability_status"]="breakdown"
    else:
        entry["verified_n"]=0;entry["verified_win"]=None;entry["verified_mean"]=None
    # Position monitor: signal exit, -5% stop, or max 8 completed bars (T+1).
    positions=[]
    if positions_path and Path(positions_path).exists():
        positions=json.loads(Path(positions_path).read_text(encoding="utf-8"))
    monitored=[]
    for pos in positions if isinstance(positions,list) else []:
        code=str(pos.get("ts_code","")); rows=d[(d.ts_code==code)&(d.context_end_ts<=asof_ts)].sort_values("context_end_ts"); last=rows.iloc[-1] if len(rows) else None
        entry_day=str(pos.get("entry_day", ""))[:10]
        hold_bars, panel_px = _position_market(root, predictions, code, entry_day, asof_ts)
        entry_px=float(pos.get("entry_px",0) or 0); px=panel_px if panel_px is not None else (clean(last.get("last_close")) if last is not None else None)
        ret=(float(px)/entry_px-1) if px and entry_px else None
        sig=clean(last.get("ema_signal")) if last is not None else None
        reason="HOLD"
        if entry_day and entry_day >= latest: reason="LOCKED_T1"
        elif ret is not None and ret<=-.05: reason="SELL_STOP_LOSS"
        elif sig is not None and sig<0: reason="SELL_SIGNAL"
        elif hold_bars is not None and hold_bars>=8: reason="SELL_MAX_HOLD"
        monitored.append({**pos,"last_price":px,"return":ret,"ema_signal":sig,"hold_bars_completed":hold_bars,"action":reason})
    selected=entry[(entry.buy_action=="BUY_TAIL_14:45-14:55")&(entry.stability_status!="breakdown")].head(topk)
    latest_rows=[]
    for _,r in entry.head(max(topk,100)).iterrows():
        latest_rows.append({k:clean(r.get(k)) for k in ["ts_code","bar_hm","pred_return","pred_return_last","pred_return_std","last_close","pred_close_path","target_trade_date","ema_signal","signal_rank","buy_action","stability_status","verified_n","verified_win","verified_mean","trade_open","trade_close","pred_end_ts"]})
    candidates=[{k:clean(r.get(k)) for k in ["ts_code","pred_return","pred_return_last","pred_return_std","last_close","pred_close_path","ema_signal","signal_rank","stability_status","verified_n","verified_win","verified_mean"]} for _,r in selected.iterrows()]
    outdir.mkdir(parents=True,exist_ok=True)
    state = Path(state_dir) if state_dir else outdir
    state.mkdir(parents=True, exist_ok=True)
    h=state/"intraday_prediction_history.jsonl"
    previous=None
    if h.exists():
        try:
            previous=json.loads(h.read_text(encoding="utf-8").splitlines()[-1])
        except Exception: pass
    old={x.get("ts_code"):x for x in (previous or {}).get("latest",[])}
    current_state=latest_rows[:100]
    current={x.get("ts_code"):x for x in current_state}
    changes=[]
    for row in current_state:
        before=old.get(row["ts_code"])
        if before is None: changes.append({"ts_code":row["ts_code"],"change":"entered_latest100","to":row})
        elif any(abs(float(before.get(k) or 0)-float(row.get(k) or 0))>1e-9 for k in ("ema_signal","signal_rank")) or before.get("buy_action")!=row.get("buy_action"):
            changes.append({"ts_code":row["ts_code"],"change":"signal_changed","from":before,"to":row})
    for code, before in old.items():
        if code not in current: changes.append({"ts_code":code,"change":"left_latest100","from":before})
    payload={"available":True,"model":"KRN-260912-ID30-C1-LOSS-F8A44007","signal_date":latest,"generated_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"ema_alpha":alpha,"entry_rule":"14:30信号；14:45–14:55尾盘买入，T+1可卖","exit_rule":"T+1后：EMA信号<0、亏损≥5%、或最多持有8个bar，先到先卖","sources":sources,"latest":latest_rows,"buy_candidates":candidates,"changes":changes,"positions":monitored,"stable_verified_count":int((entry.stability_status=="stable_verified").sum()),"warnings":["stable_verified 只使用已成熟的 actual_return；当前信号不以未来标签筛选。","该输出为纸上模拟/提醒，不提交实盘订单。"]}
    snap=outdir/f"intraday_timing_{latest.replace('-','')}.json"; snap.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=clean),encoding="utf-8")
    key=hashlib.sha1(json.dumps(payload["latest"],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12]
    with h.open("a",encoding="utf-8") as f: f.write(json.dumps({"snapshot":str(snap),"signal_date":latest,"fingerprint":key,"generated_at":payload["generated_at"],"latest":current_state,"buy_candidates":payload["buy_candidates"],"changes":changes},ensure_ascii=False)+"\n")
    return payload

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--project-root",default=DEFAULT_ROOT); ap.add_argument("--output-dir",default=DEFAULT_OUT); ap.add_argument("--predictions"); ap.add_argument("--date"); ap.add_argument("--ema-alpha",type=float,default=.25); ap.add_argument("--top-k",type=int,default=5); ap.add_argument("--positions"); ap.add_argument("--state-dir")
    a=ap.parse_args(); print(json.dumps(timing(Path(a.project_root),Path(a.output_dir),a.date,a.ema_alpha,a.top_k,a.positions,a.predictions,a.state_dir),ensure_ascii=False,indent=2,default=clean))
if __name__=="__main__": main()
