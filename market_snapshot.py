#!/usr/bin/env python3
"""Small real-data adapter for the QuantWeave (run with aloha Python).

It intentionally exposes raw Kronos ranking and per-stock history separately
from the portfolio gate. A NO_TRADE portfolio therefore still has a useful
stock ranking to inspect.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd

DEFAULT_ROOT = "/opt/kronos"

def root_paths(root):
    s=Path(root)/"factor_horizon"/"shadow_live"
    panels=sorted((s/"data"/"panels").glob("astock_daily_ohlcva_*.parquet"))
    return s, panels, s/"curves"/"rp10_current.parquet", s/"data"/"stock_basic_all.parquet"

def clean(v):
    if pd.isna(v): return None
    return v.item() if hasattr(v,"item") else (v.isoformat() if hasattr(v,"isoformat") else v)

def snapshot(root, limit=100, date=None, industry=None):
    shadow, panels, curve, basic = root_paths(root)
    if not curve.exists(): raise FileNotFoundError(curve)
    c=pd.read_parquet(curve)
    signal=str(date or c.signal_date.max()).replace("-","")
    c=c[c.signal_date.astype(str)==signal]
    ecols=[f"k_e{i}" for i in range(1,11)]
    for col in ecols: c[col]=pd.to_numeric(c[col],errors="coerce")
    c["kronos_score"]=c[ecols].mean(axis=1); c["kronos_dispersion"]=c[ecols].std(axis=1)
    if basic.exists():
        b=pd.read_parquet(basic)
        keep=[x for x in ["ts_code","name","industry","market"] if x in b]
        c=c.merge(b[keep].drop_duplicates("ts_code"),on="ts_code",how="left")
    if industry: c=c[c.get("industry",pd.Series(index=c.index)).fillna("")==industry]
    c=c.sort_values("kronos_score",ascending=False).head(limit).reset_index(drop=True)
    rows=[]
    for i,r in c.iterrows():
        rows.append({"rank":int(i+1),"ts_code":r.ts_code,"name":clean(r.get("name")),"industry":clean(r.get("industry")),"market":clean(r.get("market")),"kronos_score":clean(r.kronos_score),"kronos_dispersion":clean(r.kronos_dispersion),"kronos_e": [clean(r.get(x)) for x in ecols],"confidence":None,"score_method":"mean of normalized Kronos E1-E10; raw ranking"})
    groups=c.dropna(subset=["kronos_score"]).groupby(c.get("industry",pd.Series("未分类",index=c.index))).agg(count=("ts_code","size"),mean_score=("kronos_score","mean")).reset_index()
    groups.columns=["industry","count","mean_score"]
    return {"available":True,"signal_date":signal,"model":"rp10_current","score_method":"mean of normalized Kronos E1-E10; raw ranking","portfolio_gate_separate":True,"rows":rows,"industries":[{k:clean(v) for k,v in r.items()} for r in groups.to_dict("records")],"panel_count":len(panels)}

def stock(root, symbol, limit=90):
    shadow, panels, curve, basic = root_paths(root); symbol=symbol.upper()
    name=industry=None
    if basic.exists():
        b=pd.read_parquet(basic,filters=[["ts_code","=",symbol]])
        if len(b): name=clean(b.iloc[0].get("name"));industry=clean(b.iloc[0].get("industry"))
    frames=[]
    for p in panels[-limit:]:
        d=pd.read_parquet(p,columns=["ts_code","timestamps","open","high","low","close","volume"],filters=[["ts_code","=",symbol]])
        if len(d): frames.append(d)
    if not frames: raise LookupError(f"未找到 {symbol} 的行情")
    o=pd.concat(frames).sort_values("timestamps").drop_duplicates(["ts_code","timestamps"])
    o["close"]=pd.to_numeric(o.close,errors="coerce");o["ema_close"]=o.close.ewm(alpha=.12,adjust=False).mean()
    factors=pd.read_parquet(curve,filters=[["ts_code","=",symbol]]) if curve.exists() else pd.DataFrame()
    ecols=[f"k_e{i}" for i in range(1,11)]
    fh=[]
    if len(factors):
        factors["kronos"]=factors[ecols].mean(axis=1);factors=factors.sort_values("signal_date");factors["kronos_ema"]=factors.kronos.ewm(alpha=.12,adjust=False).mean()
        fh=[{"date":clean(r.signal_date),"kronos":clean(r.kronos),"kronos_ema":clean(r.kronos_ema)} for _,r in factors.tail(limit).iterrows()]
        last=factors.iloc[-1]; horizon=[{"horizon":i,"score":clean(last.get(f"k_e{i}")),"raw_mean":clean(last.get(f"k_mu_raw_e{i}")),"raw_sigma":clean(last.get(f"k_sigma_raw_e{i}"))} for i in range(1,11)]
        signal=str(last.signal_date)
    else: signal=None;horizon=[]
    return {"available":True,"symbol":symbol,"name":name,"industry":industry,"signal_date":signal,"ema_decay":.88,"ohlc":[{k:clean(v) for k,v in r.items()} for r in o.tail(limit).to_dict("records")],"factor_history":fh,"horizon":horizon,"warnings":["EMA uses decay 0.88 (alpha 0.12); this is descriptive and does not alter frozen portfolio weights."]}

def main():
    p=argparse.ArgumentParser();p.add_argument("--project-root",default=DEFAULT_ROOT);p.add_argument("--mode",choices=["snapshot","stock"],default="snapshot");p.add_argument("--symbol");p.add_argument("--limit",type=int,default=100);p.add_argument("--date");p.add_argument("--industry");a=p.parse_args()
    out=snapshot(a.project_root,a.limit,a.date,a.industry) if a.mode=="snapshot" else stock(a.project_root,a.symbol,a.limit)
    print(json.dumps(out,ensure_ascii=False,default=clean))
if __name__=="__main__": main()
