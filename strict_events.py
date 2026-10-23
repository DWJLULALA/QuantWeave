"""Presentation and append-only observations for recursive structure events."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import numpy as np
from strict_chan import analyze, RULE_VERSION


def native(value):
    if isinstance(value,dict):return {str(k):native(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [native(v) for v in value]
    if isinstance(value,(np.floating,np.integer,np.bool_)):return value.item()
    return value


def snapshot(code,level,bars,root,now,complete=True,conflicts=None,daily=None,gaps=None):
    model=native(analyze(bars))
    dt=lambda i:bars.dt.iloc[int(i)].strftime('%Y-%m-%d %H:%M')
    line=lambda u:{**u,'start':dt(u['start_idx']),'end':dt(u['end_idx']),
                   'confirmed_at':dt(u['available_idx'])}
    for rank,units in model['layers'].items():model['layers'][rank]=[line(u) for u in units]
    for centres in model['centres'].values():
        for z in centres:
            z.update(start=dt(z['start_idx']),end=dt(z['end_idx']),formed_at=dt(z['formed_idx']),
                     confirmed_at=dt(z['confirmed_idx']) if 'confirmed_idx' in z else None)
    model['floor_candidates']=[{**s,'point_at':dt(s['point_idx']),'confirmed_at':dt(s['confirmed_idx']),
                                'invalidated_at':dt(s['invalidated_idx']) if s.get('invalidated_idx') is not None else None}
                               for s in model['signals'] if not s['strict_recursive']]
    for s in model['diagnostics']:
        s.update(point_at=dt(s['point_idx']),confirmed_at=dt(s['confirmed_idx']),
                 failed_checks=[k for k,v in s['evidence']['checks'].items() if not v])
    ledger=Path(root)/'signal_ledger'/(code+'_'+level+'.jsonl')
    known={};invalidated={};withdrawn=set();records=[];previous_asof=None
    if ledger.exists():
        for row in ledger.read_text().splitlines():
            if not row.strip():continue
            item=json.loads(row);records.append(item)
            if item['event']=='observed':known[item['id']]=item
            elif item['event']=='invalidated':invalidated[item['id']]=item
            elif item['event']=='source_revision_withdrawal':withdrawn.add(item['id'])
            elif item['event']=='source_revision_reinstated':withdrawn.discard(item['id'])
            elif item['event']=='checkpoint':previous_asof=item['asof']
    now_s=now.isoformat(timespec='seconds');asof=dt(len(bars)-1)
    # Identical input prefixes can be independently replayed from these hashes.
    fingerprint=hashlib.sha256(bars[['dt','open','high','low','close']].to_csv(index=False).encode()).hexdigest()
    events=[];blocked=[];new=[]
    for raw in model['signals']:
        if not raw['strict_recursive']:continue
        point,confirmation=dt(raw['point_idx']),dt(raw['confirmed_idx'])
        key=f'{RULE_VERSION}:{code}:{level}:R{raw["rank"]}:{point}:{confirmation}:{raw["stop"]:.8f}'
        sid=hashlib.sha256(key.encode()).hexdigest()[:20]
        A=raw['evidence']['A'];window_start=dt(A['start_idx'])[:10].replace('-','')
        window_end=confirmation[:10].replace('-','')
        uncertain=[r for r in (conflicts or []) if window_start<=r['date']<=window_end]
        prefix_complete=not any(d<=window_end for d in gaps) if gaps is not None else complete
        item={'id':sid,'rank':raw['rank'],'base_frequency':level,'rule_version':RULE_VERSION,
              'point_at':point,'confirmed_at':confirmation,'stop':raw['stop'],
              'evidence':raw['evidence'],'strict_recursive':True,
              'intraday_nesting_verified':False,'data_complete':prefix_complete}
        if not prefix_complete or uncertain:
            item.update(blocked_by='data_gap' if not prefix_complete else 'source_ohlc_conflict',conflicts=uncertain)
            blocked.append(item);continue
        if sid not in known:
            observation={'event':'observed','id':sid,'observed_at':now_s,'signal':item,
                         'backfilled':previous_asof is None or confirmation<=previous_asof,
                         'input_sha256':fingerprint}
            known[sid]=observation;new.append(observation)
        if sid in withdrawn:
            new.append({'event':'source_revision_reinstated','id':sid,'observed_at':now_s})
            withdrawn.discard(sid)
        first=known[sid]
        item.update(observed_at=first['observed_at'],backfilled=first['backfilled'],status='active',invalidated_at=None)
        observed_bars=daily if daily is not None else bars
        later=observed_bars[(observed_bars.dt>confirmation)&(observed_bars.low<item['stop']-1e-8)]
        if len(later):
            invalid_at=later.dt.iloc[0].strftime('%Y-%m-%d %H:%M')
            if sid not in invalidated:
                record={'event':'invalidated','id':sid,'invalidated_at':invalid_at,'observed_at':now_s,'stop':item['stop']}
                invalidated[sid]=record;new.append(record)
        if sid in invalidated:item.update(status='invalidated',invalidated_at=invalidated[sid]['invalidated_at'])
        elif any(d>window_end for d in (gaps or [])):item['status']='monitoring_gap'
        elif any(r['date']>window_end for r in (conflicts or [])):item['status']='monitoring_conflict'
        events.append(item)
    current={r['id'] for r in events}
    for sid,old in known.items():
        if sid in current:continue
        if sid not in withdrawn:
            new.append({'event':'source_revision_withdrawal','id':sid,'observed_at':now_s,
                        'reason':'当前输入重算不再满足原规则或数据校验；原观测保留'})
        historic=dict(old['signal']);historic.update(status='withdrawn',observed_at=old['observed_at'],
                     backfilled=old['backfilled'],invalidated_at=invalidated.get(sid,{}).get('invalidated_at'))
        events.append(historic)
    new.append({'event':'checkpoint','asof':asof,'observed_at':now_s,'input_sha256':fingerprint,
                'rule_version':RULE_VERSION,'complete':complete})
    ledger.parent.mkdir(parents=True,exist_ok=True)
    with ledger.open('a') as f:
        for row in new:f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        f.flush()
    model.update(signals=events,blocked_candidates=blocked,evaluated=complete,input_sha256=fingerprint,
                 label='严格递归1买（K线确认笔基底）' if complete else '输入缺口，严格递归确认暂停',
                 scope='R0=当前频率的冻结笔；R1=特征序列线段；R2及以上=递归线段中枢。日K递归层级不等同于真实30分钟区间套。')
    model['counts']={'segments':{k:len(v) for k,v in model['layers'].items()},
                     'closed_centres':{k:sum(not z['provisional'] for z in v) for k,v in model['centres'].items()}}
    model['assessment']='data_gap' if not complete else 'insufficient_structure' if model['counts']['closed_centres'].get('2',0)<2 else 'evaluated'
    return model
