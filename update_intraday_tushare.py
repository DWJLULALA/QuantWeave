#!/usr/bin/env python3
"""Refresh a 30-minute panel through the Tushare-compatible proxy.

The token is resolved at runtime from TUSHARE_PROXY_TOKEN or the existing
Kronos TOKEN assignment; it is never written to output, summaries, or logs.
"""
from __future__ import annotations
import argparse, json, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
import requests

API_URL = "https://t.xiaodefa.top/"
COLS = ["ts_code", "timestamps", "open", "high", "low", "close", "volume", "amount"]

def args():
    p=argparse.ArgumentParser()
    p.add_argument("--input-panel",required=True); p.add_argument("--output-panel",required=True)
    p.add_argument("--as-of-date",required=True); p.add_argument("--start-date",default="")
    p.add_argument("--cutoff",default=""); p.add_argument("--workers",type=int,default=4)
    p.add_argument("--retries",type=int,default=2); p.add_argument("--limit-codes",type=int,default=0)
    return p.parse_args()

def token():
    v=os.getenv("TUSHARE_PROXY_TOKEN") or os.getenv("TUSHARE_TOKEN")
    if v: return v
    return None

def safe_message(value, secret):
    import re
    text=str(value).replace(secret, "[REDACTED]")
    return re.sub(r"(?i)token[^\s,，;；]{0,100}", "token=[REDACTED]", text)[:240]

def fetch(code, start, end, cutoff, retries):
    t=token()
    if not t: return code,None,"token_not_configured"
    payload={"api_name":"stk_mins","token":t,
             "params":{"ts_code":code,"freq":"30min","start_date":start,"end_date":end},
             "fields":"ts_code,trade_time,open,close,high,low,vol,amount"}
    last=None
    for attempt in range(retries+1):
        try:
            r=requests.post(API_URL,json=payload,headers={"Accept-Encoding":"gzip"},timeout=60); r.raise_for_status()
            body=r.json()
            if body.get("code")!=0: return code,None,f"api_code_{body.get('code')}: {safe_message(body.get('msg',''),t)}"
            data=body.get("data") or {}; fields=data.get("fields") or []; items=data.get("items") or []
            frame=pd.DataFrame(items,columns=fields)
            if frame.empty: return code,None,"empty_items"
            frame=frame.rename(columns={"trade_time":"timestamps","vol":"volume"})
            frame["ts_code"]=code; frame["timestamps"]=pd.to_datetime(frame["timestamps"],errors="coerce")
            for c in ["open","high","low","close","volume","amount"]: frame[c]=pd.to_numeric(frame[c],errors="coerce")
            frame=frame[COLS].dropna(subset=["timestamps","open","high","low","close"])
            if cutoff: frame=frame[frame["timestamps"]<=pd.Timestamp(cutoff)]
            return code,frame,None
        except Exception as e:
            # Proxy error bodies can echo the credential or a token fragment.
            # Never persist that response in the per-code failure summary.
            msg=str(e).replace(t, "[REDACTED]")
            import re
            msg=re.sub(r"(?i)(token(?:不对|错误)?[^\s,，;；]{0,80})", "token=[REDACTED]", msg)
            last=f"{type(e).__name__}: {msg[:240]}"
            if attempt<retries: time.sleep(0.7*(attempt+1))
    return code,None,last

def main():
    a=args(); old=pd.read_parquet(a.input_panel)
    codes=sorted(old["ts_code"].dropna().astype(str).unique())
    if a.limit_codes>0: codes=codes[:a.limit_codes]
    end=f"{a.as_of_date} 15:00:00"
    start=a.start_date or f"{(pd.Timestamp(a.as_of_date)-pd.Timedelta(days=7)).strftime('%Y-%m-%d')} 09:00:00"
    frames=[]; failures=[]; workers=max(1,min(a.workers,16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fs=[pool.submit(fetch,c,start,end,a.cutoff,a.retries) for c in codes]
        for i,f in enumerate(as_completed(fs),1):
            code,frame,error=f.result()
            if frame is not None and not frame.empty: frames.append(frame)
            else: failures.append({"ts_code":code,"error":error})
            if i%100==0 or i==len(codes): print({"done":i,"total":len(codes),"frames":len(frames),"failures":len(failures)},flush=True)
    new=pd.concat(frames,ignore_index=True) if frames else old.iloc[:0].copy()
    combined=pd.concat([old,new],ignore_index=True); combined["timestamps"]=pd.to_datetime(combined["timestamps"])
    combined=combined.drop_duplicates(["ts_code","timestamps"],keep="last").sort_values(["ts_code","timestamps"]).reset_index(drop=True)
    out=Path(a.output_panel); out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix(".tmp.parquet"); combined.to_parquet(tmp,index=False); tmp.replace(out)
    max_ts=combined["timestamps"].max()
    summary={"source":"tushare_proxy","input_panel":str(a.input_panel),"output_panel":str(out),
             "codes":len(codes),"old_rows":len(old),"new_rows_fetched":len(new),"combined_rows":len(combined),
             "max_timestamp":str(max_ts),"latest_codes":int(combined.loc[combined["timestamps"]==max_ts,"ts_code"].nunique()),
             "failure_count":len(failures),"failures":failures[:100]}
    out.with_suffix(".summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
if __name__=="__main__": main()
