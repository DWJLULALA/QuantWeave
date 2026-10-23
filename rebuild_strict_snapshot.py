"""Recompute structure from an immutable complete snapshot; no network calls.

Writes a separate generation/ledger. Caller reviews validation before promotion.
"""
from pathlib import Path
from datetime import datetime
import argparse
import copy
import json
import shutil
import uuid
import numpy as np
import pandas as pd
from strict_chan import analyze,RULE_VERSION
from strict_events import snapshot as strict_snapshot
from trend_analysis import rank_industries,write_json,TZ,SW
from sw_repair import exchange_dates


def stable_signal(s):return {k:v for k,v in s.items() if k!='invalidated_idx'}

def rebuild(source,output):
    old=json.loads((source/'snapshot.json').read_text())
    original_generation=old['generation'];now=datetime.now(TZ)
    generation=now.strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]
    dest=output/'generations'/generation;dest.mkdir(parents=True)
    if (source/'signal_ledger').exists():shutil.copytree(source/'signal_ledger',output/'signal_ledger',dirs_exist_ok=True)
    assets={p.stem:json.loads(p.read_text()) for p in (source/'generations'/original_generation).glob('*.json')}
    checks=0;raw_signals=[];active=[];blocked=[];floor=[];geometry=[]
    for code,item in sorted(assets.items()):
        daily=pd.DataFrame(item['levels']['D']['bars']);daily['dt']=pd.to_datetime(daily.dt)
        assert len(daily)==len(item['levels']['D']['timeline']),f'{code} truncated daily input'
        expected=exchange_dates(old['history_start'],old['asof'])
        actual=set(daily.dt.dt.strftime('%Y%m%d'));gaps=[d for d in expected if d not in actual]
        audit=source/'repairs/audit'/(code+'.json')
        conflicts=json.loads(audit.read_text()).get('conflicts',[]) if audit.exists() else []
        for level,chart in item['levels'].items():
            if not chart.get('available'):continue
            bars=pd.DataFrame(chart['bars']);bars['dt']=pd.to_datetime(bars.dt)
            assert len(bars)==len(chart['timeline']),f'{code}/{level} truncated input'
            raw=analyze(bars)
            # One independent prefix per actual chart frequency, not a mock.
            n=max(10,len(bars)-max(3,len(bars)//7))
            prefix=analyze(bars.iloc[:n])
            for rank in raw['layers']:
                assert prefix['layers'][rank]==[u for u in raw['layers'][rank] if u['available_idx']<n],(code,level,rank,'segments')
            for rank in raw['centres']:
                assert [z for z in prefix['centres'][rank] if not z['provisional']]==[z for z in raw['centres'][rank] if not z['provisional'] and z['confirmed_idx']<n],(code,level,rank,'centres')
            assert [stable_signal(s) for s in prefix['signals']]==[stable_signal(s) for s in raw['signals'] if s['confirmed_idx']<n],(code,level,'signals')
            checks+=1
            rec=strict_snapshot(code,level,bars,output,now,chart.get('data_quality',{}).get('complete',True),
                                conflicts,daily=daily,gaps=gaps)
            chart['recursive']=rec
            centres=[z for z in rec['centres'].get('2',[]) if not z['provisional']]
            lines=rec['layers'].get('1',[])
            chart['phase']=(1 if chart['close']>centres[-1]['zg'] else -1 if chart['close']<centres[-1]['zd'] else 0) if centres else ((1 if lines[-1]['end_price']>lines[-1]['start_price'] else -1) if lines else 0)
            signals=[s for s in rec['signals'] if s['status']=='active']
            chart['bottom'].update(strict_1buy_confirmed=bool(signals) if rec['evaluated'] else None,
                                    strict_1buy_evaluated=rec['evaluated'],strict_signals=signals,recursive_assessment=rec['assessment'])
            for s in raw['signals']:
                target=raw_signals if s['strict_recursive'] else floor
                target.append({'code':code,'name':item['summary']['name'],'level':level,'rank':s['rank'],
                               'point':str(bars.dt.iloc[s['point_idx']]),'confirmed':str(bars.dt.iloc[s['confirmed_idx']]),'stop':s['stop']})
            active.extend({'code':code,'level':level,**s} for s in signals)
            blocked.extend({'code':code,'level':level,**s} for s in rec['blocked_candidates'])
            if level=='D':geometry.append({'code':code,'r1':len(lines),'r2_centres':len(centres)})
        summary=item['summary'];summary['bottom']=item['levels']['D']['bottom']
        summary.update(phase_d=item['levels']['D']['phase'],phase_w=item['levels']['W']['phase'])
        print('replayed',code,summary['name'],flush=True)
        write_json(dest/(code+'.json'),item)
    manifest=copy.deepcopy(old)
    for key,g in manifest['classifications'].items():
        codes=[r['code'] for r in g['catalog']]
        g['industries'],g['excluded']=rank_industries(assets,assets['000300']['daily_prices'],old['asof'],codes)
        g['reference_industries'],_=rank_industries(assets,assets['000300']['daily_prices'],old['asof'],codes,allow_gaps=True)
        g['count']=len(g['industries'])
        g['unranked']=[assets[r['code']]['summary'] for r in g['excluded'] if r['code'] in assets]
    manifest.update(generation=generation,generated_at=now.isoformat(timespec='seconds'),strict_rule_version=RULE_VERSION,
                    markets=[assets[r['code']]['summary'] for r in old['markets']],
                    industries=manifest['classifications']['sw1']['industries'],
                    industry_count=manifest['classifications']['sw1']['count'],
                    excluded=manifest['classifications']['sw1']['excluded'],
                    recomputed_from=original_generation)
    write_json(output/'snapshot.json',manifest)
    result={'previous_generation':original_generation,'generation':generation,'rule_version':RULE_VERSION,
            'asof':old['asof'],'assets':len(assets),'independent_prefix_checks':checks,'mismatches':0,
            'raw_strict_signals':raw_signals,'active_strict_signals':active,'blocked_candidates':blocked,
            'floor_candidates':floor,'daily_structure_counts':geometry,
            'input_ohlc_unchanged':True,'no_network_calls':True}
    write_json(output/'recognition_validation.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ['raw_strict_signals','active_strict_signals','blocked_candidates','floor_candidates','daily_structure_counts']},ensure_ascii=False),flush=True)
    print(json.dumps({'raw_strict':len(raw_signals),'active_strict':len(active),'blocked':len(blocked),'floor':len(floor)}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();rebuild(a.source,a.output)
