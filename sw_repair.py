"""Fill actual exchange-day holes, retaining official rows and provenance."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import re
import time
import threading
import urllib.parse
import urllib.request
import numpy as np
import pandas as pd

TZ=timezone(timedelta(hours=8))
CALENDAR=json.loads((Path(__file__).parent/'trade_calendar_sse.json').read_text())
REQUEST_LOCK=threading.Lock()
LAST_REQUEST=0.
POW_HEADER=None
POW_EXPIRES=0.
API_BACKOFF_UNTIL=0.


def exchange_dates(start, end):
    a,b=str(start).replace('-','')[:8],str(end).replace('-','')[:8]
    if b>CALENDAR['open_dates'][-1]:raise ValueError('交易所日历需要更新；不能按工作日猜测交易日')
    return [d for d in CALENDAR['open_dates'] if a<=d<=b]


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False));tmp.replace(path)


def response_provenance(root,url,raw):
    sha=hashlib.sha256(raw).hexdigest();path=Path(root)/'repairs'/'batches'/(sha+'.json')
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    return {'url':url,'sha256':sha,'retrieved_at':datetime.now(TZ).isoformat(),
            'batch_file':str(path.relative_to(root))}


def download(url):
    global LAST_REQUEST,POW_HEADER,POW_EXPIRES,API_BACKOFF_UNTIL
    for attempt in range(3):
        try:
            headers={'User-Agent':'StockLabResearch/1.0'}
            if 'api.ashareapi.com/' in url:
                if time.monotonic()<API_BACKOFF_UNTIL:raise ValueError('补源限流退避中；保留真实缺口')
                # Provider-documented anonymous challenge: 15 requests/minute,
                # still subject to the same daily data quota. No paid account.
                with REQUEST_LOCK:
                    wait=max(0.,4.3-(time.monotonic()-LAST_REQUEST))
                    if wait:time.sleep(wait)
                    if time.monotonic()>=POW_EXPIRES:
                        challenge=json.load(urllib.request.urlopen(urllib.request.Request('https://api.ashareapi.com/v1/challenge',headers=headers),timeout=25))
                        key=challenge['challenge'];nonce=0;limit=1<<(256-int(challenge['difficulty']))
                        while int.from_bytes(hashlib.sha256((key+':'+str(nonce)).encode()).digest(),'big')>=limit:nonce+=1
                        POW_HEADER=key+'.'+str(nonce);POW_EXPIRES=time.monotonic()+min(90,int(challenge['expires_in'])-10)
                    headers['X-PoW']=POW_HEADER;LAST_REQUEST=time.monotonic()
                    raw=urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=25).read()
            else:raw=urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=25).read()
            return raw
        except Exception as exc:
            if getattr(exc,'code',None)==429:
                API_BACKOFF_UNTIL=time.monotonic()+60
                raise ValueError('补源限流，保留缺口和上次快照；稍后再补') from exc
            if attempt==2:raise
            time.sleep(attempt+1)


def prefetch(root,frames,catalog,asof,fetch=download):
    """Documented batches of <=5 indices, only windows surrounding holes."""
    root=Path(root);groups={};expected=exchange_dates('20220101',asof)
    for code,frame in frames.items():
        if code not in catalog or not catalog[code].get('published_at_verification') or not len(frame) or frame.dt.iloc[-1].strftime('%Y%m%d')!=asof:continue
        actual=set(frame.dt.dt.strftime('%Y%m%d'))
        for year in sorted({d[:4] for d in expected if d not in actual}):
            wanted=[d for d in expected if d.startswith(year) and d not in actual]
            path=root/'repairs'/'raw'/(code+'_'+year+'.json')
            old=json.loads(path.read_text()) if path.exists() else {}
            dates={str(r.get('date','')).replace('-','') for r in old.get('data',[])}
            if set(wanted)<=dates:continue
            groups.setdefault(year,[]).append((code,wanted))
    for year,members in sorted(groups.items()):
        for offset in range(0,len(members),5):
            batch=members[offset:offset+5];wanted=[d for _,dates in batch for d in dates]
            start=max(pd.Timestamp(year+'0101'),pd.Timestamp(min(wanted))-pd.Timedelta(days=45)).strftime('%Y-%m-%d')
            end=min(pd.Timestamp(asof),pd.Timestamp(year+'1231'),pd.Timestamp(max(wanted))+pd.Timedelta(days=45)).strftime('%Y-%m-%d')
            codes=['pt01'+c for c,_ in batch]
            url='https://api.ashareapi.com/v1/kline?'+urllib.parse.urlencode({'code':','.join(codes),'period':'day','start':start,'end':end,'count':250})
            try:
                raw=fetch(url);data=json.loads(raw)
                if not data.get('ok') or not isinstance(data.get('data'),list):raise ValueError('补源批量响应无效')
                sha=hashlib.sha256(raw).hexdigest();available=datetime.now(TZ).isoformat()
                original=root/'repairs'/'batches'/(sha+'.json');original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(raw)
                for code,_ in batch:
                    rows=[r for r in data['data'] if r.get('code')=='pt01'+code] if len(batch)>1 else data['data']
                    if not rows:continue
                    path=root/'repairs'/'raw'/(code+'_'+year+'.json')
                    atomic_json(path,{**data,'data':rows})
                    atomic_json(path.with_suffix('.meta.json'),{'url':url,'sha256':sha,'retrieved_at':available,
                                                               'batch_file':str(original.relative_to(root)),'query_end':end})
                print('trend repair batch',year,offset+len(batch),'/',len(members),flush=True)
            except Exception as exc:
                print('trend repair batch failed',year,','.join(c for c,_ in batch),str(exc),flush=True)
                if '限流' in str(exc):return


def identity(code,name,root,fetch=download):
    cache=Path(root)/'repairs'/'identity'/(code+'.json')
    if cache.exists():
        data=json.loads(cache.read_text())
        if data.get('code')==code and data.get('verified_name')==name:return data
    url='https://qt.gtimg.cn/q=pt01'+code
    raw=fetch(url);text=raw.decode('gb18030',errors='strict')
    fields=text.split('"')[1].split('~')
    norm=lambda s:re.sub(r'[ⅡⅢIVⅠI\s]','',s)
    if len(fields)<4 or norm(fields[1])!=norm(name):raise ValueError('补源标识与申万行业名称不一致')
    data={'code':code,'verified_name':name,'provider_name':fields[1],'source':url,
          'sha256':hashlib.sha256(raw).hexdigest(),'verified_at':datetime.now(TZ).isoformat()}
    atomic_json(cache,data);return data


def repair(code,name,official,asof,root,fetch=download):
    root=Path(root);official=official.copy().sort_values('dt').reset_index(drop=True)
    original={d.strftime('%Y%m%d'):i for i,d in enumerate(official.dt)}
    expected=exchange_dates('20220101',asof)
    missing=[d for d in expected if d not in original]
    result={'code':code,'missing_before':missing,'filled':[],'conflicts':[],
            'remaining':missing,'errors':[],'provider':'官方原行优先；A股数据API同指数补缺',
            'calendar_source':CALENDAR['source'],'observed_at':datetime.now(TZ).isoformat()}
    if not missing:return official,result
    try:result['identity']=identity(code,name,root,fetch)
    except Exception as exc:
        result['errors'].append({'stage':'identity','reason':str(exc)})
        atomic_json(root/'repairs'/'audit'/(code+'.json'),result)
        return official,result
    added=[]
    for year in sorted({d[:4] for d in missing}):
        try:
            end=min(asof,year+'1231')
            url='https://api.ashareapi.com/v1/kline?'+urllib.parse.urlencode({'code':'pt01'+code,'period':'day','start':year+'-01-01','end':end[:4]+'-'+end[4:6]+'-'+end[6:],'count':250})
            cache=root/'repairs'/'raw'/(code+'_'+year+'.json')
            meta=cache.with_suffix('.meta.json')
            data=json.loads(cache.read_text()) if cache.exists() else None
            wanted=[d for d in missing if d.startswith(year)]
            cached_dates={str(r.get('date','')).replace('-','') for r in (data or {}).get('data',[])}
            if data is None or not set(wanted)<=cached_dates:
                raw=fetch(url);data=json.loads(raw)
                if not data.get('ok') or not isinstance(data.get('data'),list):raise ValueError('补源返回无效行情')
                atomic_json(cache,data)
                atomic_json(meta,response_provenance(root,url,raw))
            provenance=json.loads(meta.read_text())
            common=[];valid={};volume_ratios=[]
            for row in data['data']:
                if row.get('code') and row['code']!='pt01'+code:raise ValueError('缓存内补源代码不匹配')
                date=str(row['date']).replace('-','')
                if date not in expected:continue
                px={k:float(row[j]) for k,j in [('open','open'),('high','high'),('low','low'),('close','last')]}
                if not all(np.isfinite(list(px.values()))) or px['low']<=0 or px['high']<max(px.values()) or px['low']>min(px.values()):raise ValueError('补源OHLC不合法')
                valid[date]=(px,row)
                if date in original:
                    old=official.iloc[original[date]]
                    matches={k:abs(px[k]-float(old[k]))<=.011 for k in px}
                    # Identity is a series test, with a 0.1 basis-point numeric
                    # tolerance for high-valued indices. The tighter O/H/L/C
                    # audit still records such differences and vetoes buys.
                    matches['close_identity']=abs(px['close']-float(old['close']))<=max(.011,abs(float(old['close']))*1e-5)
                    common.append(matches)
                    if not all(matches.values()):result['conflicts'].append({'date':date,'official':{k:float(old[k]) for k in px},'supplement':px,'kept':'official'})
                    if float(row.get('volume',0))>0 and float(old.volume)>0:volume_ratios.append(float(old.volume)/float(row['volume']))
            # A code match alone is insufficient. Verify the same price series in
            # the requested historical window before accepting any missing candle.
            # OHLC disagreements are retained as evidence, not silently resolved.
            # The independently verified code/name and >=20 matching closes
            # establish the index identity. Opens are also revised by sources:
            # any O/H/L/C disagreement remains a strict-signal veto, rather
            # than treating a differing open as a different index entirely.
            if len(common)<20 or np.mean([x['close_identity'] for x in common])<.99:
                # A short surrounding window can over-weight a single revised
                # quote. Expand to the full year, retaining the same 99%
                # threshold and keeping every disputed original row.
                if provenance['url']!=url:
                    raw=fetch(url);wider=json.loads(raw)
                    if not wider.get('ok') or not isinstance(wider.get('data'),list):raise ValueError('补源扩展核验窗口无效')
                    atomic_json(cache,wider)
                    atomic_json(meta,response_provenance(root,url,raw))
                    return repair(code,name,official,asof,root,fetch)
                raise ValueError('补源与申万官方重叠价格核验未通过')
            agreement=float(np.mean([all(x.values()) for x in common]))
            mult=float(np.median(volume_ratios)) if volume_ratios else None
            for date in wanted:
                if date not in valid:continue
                px,row=valid[date]
                item={'dt':pd.Timestamp(date)+pd.Timedelta(hours=15),**px,
                      'volume':float(row.get('volume',0))*mult if mult is not None else np.nan}
                added.append(item)
                result['filled'].append({'date':date,**px,'available_at':provenance['retrieved_at'],
                                        'source':provenance['url'],'sha256':provenance['sha256'],
                                        'overlap_count':len(common),'ohlc_agreement':agreement,
                                        'close_agreement':float(np.mean([x['close_identity'] for x in common])),
                                        'volume_multiplier':mult})
        except Exception as exc:
            result['errors'].append({'year':year,'reason':str(exc)})
    out=pd.concat([official,pd.DataFrame(added)],ignore_index=True).sort_values('dt').reset_index(drop=True) if added else official
    actual=set(out.dt.dt.strftime('%Y%m%d'))
    result['remaining']=[d for d in expected if d not in actual]
    atomic_json(root/'repairs'/'audit'/(code+'.json'),result)
    return out,result
