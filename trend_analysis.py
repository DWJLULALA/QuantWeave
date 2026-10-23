#!/usr/bin/env python3
"""Observed index/industry trends, isolated from forecasts and portfolio gates.

Official CSI all-share sector indices provide real OHLC rather than an
invented aggregate of stock highs. Daily ranks use one completed close date.
Retrospective ZigZag labels are display-only and never enter ranking.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
from types import SimpleNamespace
import uuid
import urllib.request

import numpy as np
import pandas as pd

from chan_core import ChanEngine, macd, resample_period
from market_wave_rules import wilder_adx, choppiness, zigzag_pivots, zigzag_labels
from sw_repair import repair as repair_sw, exchange_dates, prefetch as prefetch_sw
from strict_events import snapshot as strict_snapshot
from strict_chan import HISTORY_START, RULE_VERSION

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "data" / "trends"
TZ = timezone(timedelta(hours=8))
MARKETS = {"000001": "上证指数", "000300": "沪深300", "000905": "中证500"}
SECTORS = dict(zip([str(c).zfill(6) for c in range(986, 996)],
                   ["全指能源", "全指材料", "全指工业", "全指可选", "全指消费",
                    "全指医药", "金融地产", "全指信息", "全指通信", "全指公用"]))
SW_CATALOG = json.loads((HERE / 'sw_catalog.json').read_text())
SW = {r['code']: r for r in SW_CATALOG['industries']}
ASSETS = {**MARKETS, **SECTORS, **{c:r['name'] for c,r in SW.items()}}
TAXONOMIES = {'sw1': '申万一级（2021版）', 'sw2': '申万二级（2021版）', 'csi10': '中证全指行业（10类）'}
LEVELS = ("D", "W", "M", "60F", "30F")
RANK_METHOD = "40%缠论结构（日70%/周30%）+30%平滑浪型倾向+20%行业20日相对强弱分位+10%行业60日相对强弱分位"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, np.datetime64)):
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        value = value.item()
    if isinstance(value, float):
        return round(value, 6) if np.isfinite(value) else None
    return value


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    temp.write_text(json.dumps(clean(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    temp.replace(path)


def fetch_json(url, cache):
    cache = Path(cache)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"})
        raw = urllib.request.urlopen(request, timeout=18).read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid quote response")
        write_json(cache, data)
        return data, {"url": url, "cached": False, "sha256": hashlib.sha256(raw).hexdigest()}
    except Exception as exc:
        if cache.exists():
            raw = cache.read_bytes()
            return json.loads(raw), {"url": url, "cached": True, "warning": str(exc), "sha256": hashlib.sha256(raw).hexdigest()}
        raise


def load_daily(code, root, now):
    url = f"https://yunhq.sse.com.cn:32042/v1/csip/dayk/{code}?begin=0&end=-1&period=day"
    obj, source = fetch_json(url, root / "raw" / f"{code}_D.json")
    if str(obj.get("code")) != code or not obj.get("kline"):
        raise ValueError("official index unavailable: " + code)
    bars = pd.DataFrame([r[:6] for r in obj["kline"]], columns=["dt", "open", "high", "low", "close", "volume"])
    bars["dt"] = pd.to_datetime(bars.dt.astype(str), format="%Y%m%d") + pd.Timedelta(hours=15)
    bars = bars[(bars.dt <= pd.Timestamp(now).tz_localize(None)) & (bars.dt >= HISTORY_START)]
    return validate_bars(bars), source


def load_minute(code, level, root, now):
    key = {"30F": "m30", "60F": "m60"}[level]
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh{code},{key},,640"
    obj, source = fetch_json(url, root / "raw" / f"{code}_{level}.json")
    rows = obj.get("data", {}).get("sh" + code, {}).get(key, [])
    if not rows:
        raise ValueError("分钟行情暂不可用")
    bars = pd.DataFrame([r[:6] for r in rows], columns=["dt", "open", "close", "high", "low", "volume"])
    bars["dt"] = pd.to_datetime(bars.dt, format="%Y%m%d%H%M")
    bars = bars[bars.dt <= pd.Timestamp(now).tz_localize(None)]
    # Preserve the original minute bootstrap as the provider's rolling window
    # advances. A corrected overlap is a data revision, never a new signal
    # supposedly known at its old endpoint.
    archive=root/'minute_archive'/(code+'_'+level+'.json')
    if archive.exists():
        old=pd.DataFrame(json.loads(archive.read_text()))
        old['dt']=pd.to_datetime(old.dt)
        bars=pd.concat([old,bars],ignore_index=True).drop_duplicates('dt',keep='last')
    bars=validate_bars(bars)
    write_json(archive,bars.to_dict('records'))
    return bars, source


def load_sw_daily(code, root, now):
    """Official index OHLC. Never synthesize missing candles from returns."""
    url = f'https://www.swsresearch.com/institute-sw/api/index_publish/trend/?swindexcode={code}&period=DAY'
    cache = root / 'raw' / (code + '_D.json')
    source = {'url':url, 'cached':False, 'provider':'申万宏源研究'}
    last_error = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            try:
                raw = urllib.request.urlopen(req, timeout=25).read()
                source['tls_verified'] = True
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, ssl.SSLCertVerificationError): raise
                # SWS currently omits its certificate chain. This compatibility
                # fallback is limited to this fixed public-data endpoint.
                raw = urllib.request.urlopen(req, context=ssl._create_unverified_context(), timeout=25).read()
                source['tls_verified'] = False
            obj = json.loads(raw)
            if str(obj.get('code')) != '200' or not obj.get('data'): raise ValueError('申万日线为空')
            if any(str(r.get('swindexcode')) != code for r in obj['data']): raise ValueError('申万代码不匹配')
            write_json(cache, obj)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if isinstance(exc, ValueError): break
    if last_error is not None:
        if not cache.exists(): raise last_error
        raw = cache.read_bytes(); obj = json.loads(raw)
        source.update(cached=True, warning=str(last_error))
    source['sha256'] = hashlib.sha256(raw).hexdigest()
    frame = pd.DataFrame(obj['data']).rename(columns={'bargaindate':'dt','openindex':'open','maxindex':'high','minindex':'low','closeindex':'close','bargainamount':'volume'})
    frame['dt'] = pd.to_datetime(frame.dt).dt.normalize() + pd.Timedelta(hours=15)
    # A rolling 1000-row start changes the structural bootstrap every day.
    # Keep a declared, fixed history origin for reproducible recursive replay.
    frame = frame[(frame.dt <= pd.Timestamp(now).tz_localize(None)) & (frame.dt >= HISTORY_START)].sort_values('dt')
    return validate_bars(frame[['dt','open','high','low','close','volume']]), source


def validate_bars(bars):
    bars = bars.copy().sort_values("dt").reset_index(drop=True)
    if bars.empty or bars.dt.duplicated().any():
        raise ValueError("empty or duplicate index bars")
    for col in ("open", "high", "low", "close", "volume"):
        bars[col] = pd.to_numeric(bars[col], errors="raise")
    if not np.isfinite(bars[["open", "high", "low", "close"]]).all().all():
        raise ValueError("non-finite OHLC")
    if (bars.low <= 0).any() or (bars.high < bars[["open", "close", "low"]].max(axis=1) - 1e-8).any() or (bars.low > bars[["open", "close", "high"]].min(axis=1) + 1e-8).any():
        raise ValueError("invalid OHLC")
    return bars


def completed_period(bars, period):
    result = resample_period(bars, period)
    final = bars.dt.iloc[-1]
    ends = result.dt.dt.to_period("W-FRI" if period == "W" else "M").dt.end_time.dt.normalize()
    return result[ends <= final.normalize()].reset_index(drop=True)


class TrendChan(ChanEngine):
    def _source_extreme(self, mi, kind):
        bar = self.merged[mi]
        price = bar.high if kind > 0 else bar.low
        values = (self.high if kind > 0 else self.low)[bar.start_idx:bar.end_idx + 1]
        matches = np.flatnonzero(np.isclose(values, price, rtol=0, atol=1e-8))
        if not len(matches):
            raise ValueError("merged extreme has no source observation")
        return float(price), bar.start_idx + int(matches[-1])


def bottom_diagnosis(bars, engine):
    """A recent local low and observed confirmation, never a strict 1-buy.

    MACD comparison is a stroke-level observation, not recursive trend
    divergence. A local low expires after 20 bars or is invalidated by a new low.
    """
    low_idx = int(bars.tail(20).low.iloc[::-1].idxmin())
    low = float(bars.low.iloc[low_idx])
    f = next((f for f in reversed(engine.fractals)
              if f.kind == -1 and f.ext_idx == low_idx), None)
    confirmed = bool(f)
    upper = max(m.high for m in engine.merged[f.mi-1:f.mi+2]) if f else None
    reclaimed = bool(f and len(bars)-1 > f.avail_idx and (bars.close.tail(2) > upper).all())
    # Compare adjacent downward strokes ending at the selected recent low.
    downs = [s for s in engine.strokes if s.direction < 0 and s.end_idx <= low_idx]
    pair = downs[-2:] if len(downs)>=2 and downs[-1].end_idx==low_idx else []
    if engine.points and downs:
        last = engine.points[-1]
        if last['kind']>0 and last['ext_idx']<low_idx:
            pair = [downs[-1], SimpleNamespace(start_idx=last['ext_idx'], end_idx=low_idx,
                                               start_price=last['price'], end_price=low)]
        elif last['kind']<0 and low_idx>last['ext_idx'] and len(downs)>=2 and engine.strokes[-1].direction<0:
            s=downs[-1]
            pair = [downs[-2], SimpleNamespace(start_idx=s.start_idx, end_idx=low_idx,
                                               start_price=s.start_price, end_price=low)]
    evidence = None
    if pair:
        a, c = pair
        area = lambda s: float(np.maximum(-engine.hist[s.start_idx:s.end_idx+1], 0).sum())
        aa, ca = area(a), area(c)
        if c.end_price < a.end_price and c.start_price < a.start_price and aa > 0:
            evidence = {'previous_low':a.end_price, 'current_low':c.end_price,
                        'previous_start':bars.dt.iloc[a.start_idx], 'previous_end':bars.dt.iloc[a.end_idx],
                        'current_start':bars.dt.iloc[c.start_idx], 'current_end':bars.dt.iloc[c.end_idx],
                        'negative_area_ratio':ca/aa,
                        'dif_previous':float(engine.dif[a.end_idx]), 'dif_current':float(engine.dif[c.end_idx]),
                        'weakening':bool(ca < aa and engine.dif[c.end_idx] > engine.dif[a.end_idx])}
    weak = bool(evidence and evidence['weakening'])
    status = 'watch_1buy' if confirmed and weak else 'fractal' if confirmed else 'weakening' if weak else 'waiting'
    labels = {'watch_1buy':'底分型＋背驰候选', 'fractal':'底分型已确认', 'weakening':'动能减弱待确认', 'waiting':'近期低点待确认'}
    return clean({'status':status, 'label':labels[status], 'low':low,
                  'low_dt':bars.dt.iloc[low_idx], 'confirm_dt':f.dt if f else None,
                  'bars_since_low':len(bars)-1-low_idx, 'fractal_confirmed':confirmed,
                  'fractal_upper':upper, 'upper_reclaimed_2closes':reclaimed,
                  'rebound':float(bars.close.iloc[-1]/low-1), 'momentum':evidence,
                  'strict_1buy_confirmed':None, 'strict_1buy_evaluated':False,
                  'next_step':'等待次级别走势完成，并核验递归中枢与趋势背驰；跌破低点失效' if confirmed and weak else '底分型仅确认局部转折；继续核验背驰与次级别结构' if confirmed else '等待包含处理后的右侧K线完成；新低将延伸下跌结构',
                  'method':'最近20根K线最低点；包含处理后底分型；相邻下降笔（含末端延伸）创新低且负MACD面积与DIF同时减弱仅记背驰候选。本项自身不构成严格1买，递归证据另行逐项核验。'})


def level_snapshot(bars, level):
    if len(bars) < 8:
        return {"available": False, "reason": "已完成K线不足", "level": level}
    engine = TrendChan(bars)
    states = engine.run()
    completed = [s for s in engine.strokes if not s.provisional]
    zone = None
    if len(completed) >= 3:
        trio = completed[-3:]
        zd, zg = max(s.low for s in trio), min(s.high for s in trio)
        if zg > zd:
            zone = {"start": bars.dt.iloc[trio[0].start_idx], "end": bars.dt.iloc[trio[-1].end_idx], "zd": zd, "zg": zg}
    close = float(bars.close.iloc[-1])
    phase = (1 if close > zone["zg"] else -1 if close < zone["zd"] else 0) if zone else (engine.strokes[-1].direction if engine.strokes else 0)
    strokes = [{"start": bars.dt.iloc[s.start_idx], "end": bars.dt.iloc[s.end_idx],
                "start_price": s.start_price, "end_price": s.end_price,
                "provisional": s.provisional, "confirm_dt": s.confirm_dt}
               for s in engine.strokes[-160:]]
    live = None
    if engine.points:
        p = engine.points[-1]
        subset = bars.iloc[p["ext_idx"]:]
        col = "low" if p["kind"] > 0 else "high"
        loc = subset[col].idxmin() if col == "low" else subset[col].idxmax()
        live = {"start": bars.dt.iloc[p["ext_idx"]], "start_price": p["price"],
                "end": bars.dt.iloc[loc], "end_price": bars[col].iloc[loc]}
    if engine.strokes:
        s = engine.strokes[-1]
        sub = bars.iloc[s.end_idx:]
        col = "high" if s.direction > 0 else "low"
        loc = sub[col].idxmax() if col == "high" else sub[col].idxmin()
        if (bars[col].iloc[loc] - s.end_price) * s.direction > 1e-8:
            live = {"start": bars.dt.iloc[s.start_idx], "start_price": s.start_price,
                    "end": bars.dt.iloc[loc], "end_price": bars[col].iloc[loc], "replaces_last_provisional": True}
    chart = bars.copy()
    chart["dif"], chart["dea"], chart["hist"] = macd(chart.close)
    return clean({"available": True, "level": level, "asof": bars.dt.iloc[-1],
                  "bars": chart.tail(1200).to_dict("records"), "timeline": bars.dt.tolist(),
                  "strokes": strokes, "zone": zone, "live": live,
                  "phase": phase, "has_structure": bool(engine.strokes),
                  "bottom": bottom_diagnosis(bars, engine),
                  "divergence_candidate": int(states.iloc[-1]["div"]), "close": close})


def wave_series(bars):
    """Seven causal votes with slow/fast smoothing. Labels are display-only."""
    close, high, low = bars.close, bars.high, bars.low
    ma20, ma60, ma120 = [close.rolling(n).mean() for n in (20, 60, 120)]
    mom20, mom5 = close.pct_change(20), close.pct_change(5)
    ema10 = close.ewm(span=10, adjust=False).mean()
    adx, chop = wilder_adx(high, low, close), choppiness(high, low, close)
    atr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1).ewm(alpha=.1, adjust=False).mean()
    upper, lower = close + 3*atr, close - 3*atr
    fu, fl, direction = upper.iloc[0], lower.iloc[0], 1
    threshold = np.maximum(.04, 2.5*close.pct_change().rolling(20).std().fillna(.016)).to_numpy()
    confirmed, ext_i, ext_px, zz_direction = [(0, float(close.iloc[0]))], 0, float(close.iloc[0]), 0
    fast, slow, rows = None, None, []
    for i in range(len(bars)):
        px = float(close.iloc[i])
        if i:
            fu = min(float(upper.iloc[i]), fu) if close.iloc[i-1] <= fu else float(upper.iloc[i])
            fl = max(float(lower.iloc[i]), fl) if close.iloc[i-1] >= fl else float(lower.iloc[i])
            if px > fu: direction = 1
            elif px < fl: direction = -1
        # A pivot becomes visible only when this observed price confirms a
        # reversal. Filtering final-history pivots by extreme date is unsafe.
        if zz_direction == 0:
            move = (px-ext_px)/ext_px
            if abs(move) >= threshold[i]: zz_direction, ext_i, ext_px = (1 if move>0 else -1), i, px
        elif zz_direction > 0:
            if px >= ext_px: ext_i, ext_px = i, px
            elif (ext_px-px)/ext_px >= threshold[i]: confirmed.append((ext_i,ext_px)); zz_direction,ext_i,ext_px=-1,i,px
        else:
            if px <= ext_px: ext_i,ext_px=i,px
            elif (px-ext_px)/ext_px >= threshold[i]: confirmed.append((ext_i,ext_px)); zz_direction,ext_i,ext_px=1,i,px
        if i < 130:
            continue
        highs, lows = [], []
        tail=confirmed[-10:]
        for a,b,c in zip(tail,tail[1:],tail[2:]):
            if b[1]>max(a[1],c[1]): highs.append(b[1])
            if b[1]<min(a[1],c[1]): lows.append(b[1])
        dow=0
        if len(highs)>=2 and len(lows)>=2:
            dow=1 if highs[-1]>highs[-2] and lows[-1]>lows[-2] else -1 if highs[-1]<highs[-2] and lows[-1]<lows[-2] else 0
        elliott=0
        if len(confirmed)>=6:
            p=[v for _,v in confirmed[-6:]];legs=np.diff(p)
            elliott=int(legs[0]>0 and legs[1]<0 and legs[2]>0 and legs[3]<0 and legs[4]>0 and p[2]>p[0] and p[4]>p[1] and legs[2]>=min(legs[0],legs[4]) and 1<=legs[2]/legs[0]<=2.618)
        votes=[dow,direction,1 if adx.iloc[i]>25 else -1 if adx.iloc[i]<20 else 0,
               1 if chop.iloc[i]<38.2 else -1 if chop.iloc[i]>61.8 else 0,
               1 if ma20.iloc[i]>ma60.iloc[i]>ma120.iloc[i] else -1 if ma20.iloc[i]<ma60.iloc[i]<ma120.iloc[i] else 0,
               1 if mom20.iloc[i]>.02 else -1 if mom20.iloc[i]<-.02 else 0,elliott]
        raw=(float(np.mean(votes))+1)/2
        fraw=(float(np.mean([direction,1 if chop.iloc[i]<45 else -1,1 if mom5.iloc[i]>0 else -1,1 if px>ema10.iloc[i] else -1]))+1)/2
        slow=raw if slow is None else .9*slow+.1*raw
        fast=fraw if fast is None else .6*fast+.4*fraw
        span=float(close.iloc[max(0,i-119):i+1].max()-close.iloc[max(0,i-119):i+1].min())
        pos=(px-float(close.iloc[max(0,i-119):i+1].min()))/span if span>0 else .5
        rows.append({"signal_date":bars.dt.iloc[i].strftime("%Y%m%d"),"p_impulse":slow,"p_fast":fast,"p_raw":raw,"index_pos":pos,"votes":votes})
    # This retrospective overlay is intentionally computed after all causal
    # outputs and is not consumed by rank_industries.
    labels=zigzag_labels(zigzag_pivots(close.to_numpy(),threshold),len(close))
    for r,i in zip(rows,range(130,len(bars))):
        r["label"]=float(labels[i]) if np.isfinite(labels[i]) else None
    return clean(rows)


def rank_industries(assets, benchmark, asof, codes=None, allow_gaps=False):
    rows, excluded = [], []
    b = pd.Series({r["date"]: r["close"] for r in benchmark}).sort_index()
    for code in (SECTORS if codes is None else codes):
        item = assets.get(code)
        unpublished = code in SW and SW[code].get('published_at_verification') is False
        if not item:
            excluded.append({"code":code,"reason":"目录保留；源站未发布该行业指数当期行情" if unpublished else "行情获取失败"}); continue
        px = pd.Series({r["date"]:r["close"] for r in item["daily_prices"]}).reindex(b.index)
        if asof not in px.index or pd.isna(px.loc[asof]) or len(px.loc[:asof].dropna())<140:
            excluded.append({"code":code,"reason":"目录保留；源站未发布该行业指数当期行情" if unpublished else "同一排名日无完整行情或历史不足"}); continue
        window = px.loc[:asof].tail(61)
        if window.isna().any() and not allow_gaps:
            excluded.append({"code":code,"reason":"近60日行情不连续"}); continue
        if not allow_gaps and item['summary'].get('data_quality',{}).get('structure_complete') is False:
            excluded.append({'code':code,'reason':'结构计算历史存在缺失交易日'});continue
        common = pd.concat([px.rename('p'),b.rename('b')],axis=1).loc[:asof]
        if len(common)<61 or common.p.iloc[[-1,-21,-61]].isna().any():
            excluded.append({'code':code,'reason':'20/60日相对强弱端点行情缺失'});continue
        rel=lambda n:(common.p.iloc[-1]/common.p.iloc[-n-1])/(common.b.iloc[-1]/common.b.iloc[-n-1])-1
        row=dict(item["summary"])
        row.update({"asof":asof,"rs20":float(rel(20)),"rs60":float(rel(60)),
                    'rank_scope':'reference' if allow_gaps else 'complete'})
        rows.append(row)
    if not rows: return [],excluded
    for metric in ['rs20','rs60']:
        s=pd.Series([r[metric] for r in rows]);q=(s.rank(method='average')-1)/max(len(rows)-1,1) if len(rows)>1 else pd.Series([.5])
        for row,score in zip(rows,q):row[metric+'_percentile']=float(score)
    for row in rows:
        structure=(.7*row['phase_d']+.3*row['phase_w']+1)/2
        row['trend_score']=100*(.4*structure+.3*row['p_impulse']+.2*row['rs20_percentile']+.1*row['rs60_percentile'])
    rows.sort(key=lambda r:(-r['trend_score'],r['code']))
    for i,row in enumerate(rows):row['rank']=i+1
    return clean(rows),excluded


def build(root=DEFAULT_DATA, now=None):
    root=Path(root);now=now or datetime.now(TZ)
    daily,sources,failures={},{},[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs={pool.submit(load_sw_daily if c in SW else load_daily,c,root,now):c for c in ASSETS}
        for future in as_completed(jobs):
            code=jobs[future]
            try:daily[code],sources[code]=future.result()
            except Exception as exc:failures.append({'code':code,'level':'D','reason':str(exc)})
    if '000300' not in daily:raise ValueError('沪深300基准不可用，保留上次快照')
    asof=daily['000300'].dt.iloc[-1].strftime('%Y%m%d')
    # Use a single completed daily close for every ranking input.
    for code in daily:daily[code]=daily[code][daily[code].dt.dt.strftime('%Y%m%d')<=asof].reset_index(drop=True)
    prefetch_sw(root,daily,SW,asof)
    repairs={}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={pool.submit(repair_sw,c,ASSETS[c],f,asof,root):c for c,f in daily.items()
              if c in SW and SW[c].get('published_at_verification') and len(f) and f.dt.iloc[-1].strftime('%Y%m%d')==asof}
        for future in as_completed(jobs):
            code=jobs[future]
            try:
                daily[code],repairs[code]=future.result()
                sources[code]['repair']={'audit_file':'repairs/audit/'+code+'.json',
                                         'filled':len(repairs[code]['filled']),
                                         'remaining':len(repairs[code]['remaining']),
                                         'conflicts':len(repairs[code]['conflicts'])}
                print('trend repair',code,'filled',len(repairs[code]['filled']),'remaining',len(repairs[code]['remaining']),flush=True)
            except Exception as exc:failures.append({'code':code,'level':'repair','reason':str(exc)})
    minute={}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs={pool.submit(load_minute,c,l,root,now):(c,l) for c in daily if c not in SW for l in ['30F','60F']}
        for future in as_completed(jobs):
            c,l=jobs[future]
            try:minute[(c,l)],sources[c+'_'+l]=future.result()
            except Exception as exc:failures.append({'code':c,'level':l,'reason':str(exc)})
    generation=now.strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]
    dest=root/'generations'/generation;dest.mkdir(parents=True)
    assets={}
    for code,bars in sorted(daily.items()):
        if len(bars)<140 or bars.dt.iloc[-1].strftime('%Y%m%d')!=asof:continue
        print('trend index',code,ASSETS[code],flush=True)
        levels={}
        for level,frame in [('D',bars),('W',completed_period(bars,'W')),('M',completed_period(bars,'M'))]+[(l,minute[(code,l)]) for l in ['60F','30F'] if (code,l) in minute]:
            levels[level]=level_snapshot(frame,level)
        wave=wave_series(bars)
        summary={'code':code,'name':ASSETS[code],'close':float(bars.close.iloc[-1]),
                 'asof':bars.dt.iloc[-1].strftime('%Y%m%d'),'ret1':float(bars.close.iloc[-1]/bars.close.iloc[-2]-1),
                 'ret20':float(bars.close.iloc[-1]/bars.close.iloc[-21]-1),'ret60':float(bars.close.iloc[-1]/bars.close.iloc[-61]-1),
                 'p_impulse':wave[-1]['p_impulse'],'p_fast':wave[-1]['p_fast'],
                 'phase_d':levels['D'].get('phase',0),'phase_w':levels['W'].get('phase',0),
                 'position120':wave[-1]['index_pos'],'zone':levels['D'].get('zone'),
                 'bottom':levels['D'].get('bottom'), 'available_levels':list(levels),
                 **{k:v for k,v in SW.get(code,{}).items() if k not in ['code','name']}}
        calendar = exchange_dates(HISTORY_START,asof) if code in SW else daily['000300'].dt.dt.strftime('%Y%m%d').tolist()
        expected = calendar[-61:]
        actual = set(bars.dt.dt.strftime('%Y%m%d'))
        missing = [d for d in expected if d not in actual]
        audit=repairs.get(code,{})
        summary['data_quality'] = {'complete':not missing, 'missing_dates':missing, 'source':sources[code]['url'],
                                   'repaired_dates':[r['date'] for r in audit.get('filled',[])],
                                   'conflict_count':len(audit.get('conflicts',[])),
                                   'repair_errors':audit.get('errors',[])}
        # A later missing daily bar must not retroactively invalidate a closed
        # month. Check the actual daily inputs only up to each chart's cutoff.
        for c in levels.values():
            if not c.get('available'): continue
            cutoff = c['asof'][:10].replace('-','')
            start = HISTORY_START.replace('-','') if code in SW else bars.dt.iloc[0].strftime('%Y%m%d')
            absent = [d for d in calendar if start<=d<=cutoff and d not in actual]
            c['data_quality'] = {'complete':not absent, 'missing_dates':absent, 'source':sources[code]['url']}
            if absent and c.get('bottom'):
                c['bottom'].update(status='data_gap', label='数据缺口，暂停确认', fractal_confirmed=False,
                                   confirm_dt=None, upper_reclaimed_2closes=False, strict_1buy_confirmed=None, next_step='补齐真实日线后再核验底分型和背驰')
            level=c['level']
            frame=bars if level=='D' else completed_period(bars,level) if level in ['W','M'] else minute[(code,level)]
            # Minute histories have a separate declared floor and do not use a
            # daily completeness claim to imply complete intraday nesting.
            all_gaps=[d for d in calendar if start<=d<=asof and d not in actual]
            recursive=strict_snapshot(code,level,frame,root,now,not absent,audit.get('conflicts',[]),daily=bars,gaps=all_gaps)
            c['recursive']=clean(recursive)
            centres=[z for z in recursive['centres'].get('2',[]) if not z['provisional']]
            frozen=recursive['layers'].get('1',[])
            c['phase']=(1 if c['close']>centres[-1]['zg'] else -1 if c['close']<centres[-1]['zd'] else 0) if centres else ((1 if frozen[-1]['end_price']>frozen[-1]['start_price'] else -1) if frozen else 0)
            c['phase_method']='最近已结束R2中枢边界；不足时使用最近冻结R1线段方向；均不足为0'
            active=[s for s in recursive['signals'] if s['status']=='active']
            c['bottom'].update(strict_1buy_confirmed=bool(active) if not absent else None,
                               strict_1buy_evaluated=not absent,strict_signals=clean(active),
                               recursive_assessment=recursive['assessment'])
        summary['bottom'] = levels['D'].get('bottom')
        summary.update(phase_d=levels['D'].get('phase',0),phase_w=levels['W'].get('phase',0),
                       phase_method=levels['D'].get('phase_method'))
        summary['data_quality']['structure_complete'] = levels['D'].get('data_quality',{}).get('complete',False)
        summary['data_quality']['complete'] &= summary['data_quality']['structure_complete']
        assets[code]={'summary':summary,'levels':levels,'wave':wave,
                      'daily_prices':[{'date':d.strftime('%Y%m%d'),'close':float(p)} for d,p in zip(bars.dt,bars.close)]}
    groups={}
    for key,name in TAXONOMIES.items():
        codes = list(SECTORS) if key=='csi10' else [c for c,r in SW.items() if r['taxonomy']==key]
        ranked, excluded = rank_industries(assets,assets['000300']['daily_prices'],asof,codes)
        reference, _ = rank_industries(assets,assets['000300']['daily_prices'],asof,codes,allow_gaps=True)
        groups[key]={'id':key,'name':name,'industries':ranked,'excluded':excluded,
                     'count':len(ranked),'requested':len(codes),
                     'reference_industries':reference,
                     'catalog':[{'code':c,'name':ASSETS[c],**SW.get(c,{})} for c in codes],
                     'unranked':[assets[r['code']]['summary'] for r in excluded if r['code'] in assets]}
    ranked,excluded=groups['sw1']['industries'],groups['sw1']['excluded']
    if not any(g['reference_industries'] for g in groups.values()):
        raise ValueError('无可比较行业，保留上次快照')
    # Same-date benchmark position overlays, with no forward fill.
    maps={c:{r['signal_date']:r['index_pos'] for r in assets[c]['wave']} for c in ['000300','000001'] if c in assets}
    for code,item in assets.items():
        for row in item['wave']:
            row['csi300_pos']=maps.get('000300',{}).get(row['signal_date'])
            row['sse_pos']=maps.get('000001',{}).get(row['signal_date'])
        write_json(dest/(code+'.json'),item)
    payload={'available':True,'generation':generation,'generated_at':now.isoformat(timespec='seconds'),
             'asof':asof,'industry_taxonomy':TAXONOMIES['sw1'],'ranking_method':RANK_METHOD,
             'ranking_uses_retrospective_labels':False,'p_method':'七规则倾向值；EMA α0.9；敏感规则EMA α0.6；非校准上涨概率',
             'markets':[assets[c]['summary'] for c in MARKETS if c in assets],
             'industries':ranked,'industry_count':len(ranked),'industry_requested':31,
             'classifications':groups, 'default_classification':'sw1', 'sw_catalog':SW_CATALOG,
             'strict_rule_version':RULE_VERSION,'history_start':HISTORY_START,
             'repair_summary':{'filled_bars':sum(len(a.get('filled',[])) for a in repairs.values()),
                               'remaining_bars':sum(len(a.get('remaining',[])) for a in repairs.values()),
                               'conflicting_bars':sum(len(a.get('conflicts',[])) for a in repairs.values()),
                               'calendar':'上交所交易日历；节假日不补K线'},
             'excluded':excluded,'failures':failures,'sources':sources}
    write_json(root/'snapshot.json',payload)
    return clean(payload)


def load_snapshot(root=DEFAULT_DATA):
    path=Path(root)/'snapshot.json'
    if not path.exists():return {'available':False,'reason':'尚未生成趋势数据'}
    value=json.loads(path.read_text())
    age=(datetime.now(TZ)-datetime.fromisoformat(value['generated_at'])).total_seconds()
    value['refresh_recommended']=age>1800
    return value


def load_asset(code, level='D', root=DEFAULT_DATA):
    if code not in ASSETS or level not in LEVELS:raise ValueError('不支持的指数或周期')
    manifest=load_snapshot(root)
    if not manifest.get('available'):return manifest
    generation=manifest['generation']
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}_[a-f0-9]{6}',generation):raise ValueError('无效的数据版本')
    path=Path(root)/'generations'/generation/(code+'.json')
    if not path.exists():return {'available':False,'reason':'该指数尚无完整数据'}
    item=json.loads(path.read_text())
    if item['summary'].get('asof') != manifest['asof']:
        return {'available':False,'reason':'源站未发布当期行情；历史截止 '+str(item['summary'].get('asof','未知')),
                'summary':item['summary']}
    chart=item['levels'].get(level)
    if not chart or not chart.get('available'):return {'available':False,'reason':'该周期已完成行情不足或暂未获取','summary':item['summary']}
    return {'available':True,'generation':generation,'summary':item['summary'],'chart':chart,
            'wave':item['wave'][-600:],'p_method':manifest['p_method']}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,default=DEFAULT_DATA)
    args=parser.parse_args();result=build(args.data_dir)
    print(json.dumps({'asof':result['asof'],'industries':result['industry_count'],'generation':result['generation']},ensure_ascii=False))
