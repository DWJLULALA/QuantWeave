"""Contracts for online structures, strict evidence and immutable events."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from chan_vendor.line import Line
from strict_chan import FrozenStrokeEngine,SegmentStream,CenterStream,RecursiveChan,analyze
import strict_events
from sw_repair import repair,exchange_dates
from test_trend_analysis import sample_bars
from trend_analysis import TZ


def units(prices,rank=0):
    return [Line(i,rank,i,i+1,float(a),float(b),i+4) for i,(a,b) in enumerate(zip(prices,prices[1:]))]


class RecursiveContracts(unittest.TestCase):
    def test_stroke_freeze_is_later_than_endpoint_fractal_and_prefix_stable(self):
        bars=sample_bars(380);full=FrozenStrokeEngine(bars);full.run()
        self.assertGreater(len(full.frozen),5)
        self.assertTrue(all(u.available_idx>u.evidence['endpoint_fractal_idx'] for u in full.frozen))
        geometry=lambda u:(u.start_idx,u.end_idx,u.start_price,u.end_price,u.available_idx)
        for n in range(60,381,11):
            p=FrozenStrokeEngine(bars.iloc[:n]);p.run()
            self.assertEqual([geometry(u) for u in p.frozen],[geometry(u) for u in full.frozen if u.available_idx<n])

    def test_feature_sequence_gap_and_no_gap_confirmations_are_append_only(self):
        # Seeded alternating movements exercise containment and nested gaps.
        rng=np.random.default_rng(9);prices=[100.]
        for i in range(300):prices.append(prices[-1]+(-1 if i%2 else 1)*rng.uniform(1,15))
        original=units(prices);stream=SegmentStream(1);seen=[];gaps=set()
        for unit in original:
            created=stream.append(unit)
            for line in created:
                self.assertGreaterEqual(line.child_end-line.child_start,2)
                self.assertGreaterEqual(line.available_idx,unit.available_idx)
                gaps.add(line.evidence['feature_gap'])
            shapes=[(u.start_idx,u.end_idx,u.start_price,u.end_price,u.available_idx) for u in stream.lines]
            self.assertEqual(shapes[:len(seen)],seen);seen=shapes
        self.assertGreater(len(seen),10);self.assertEqual(gaps,{True,False})
        # Rebuilding each prefix must not expose next links to the full future.
        for n in range(30,len(original),17):
            p=SegmentStream(1)
            for u in units(prices[:n+1]):p.append(u)
            expected=[x for x in seen if x[-1]<=n+3]
            self.assertEqual([(u.start_idx,u.end_idx,u.start_price,u.end_price,u.available_idx) for u in p.lines],expected)

    def test_center_waits_for_third_sell_and_excludes_exit_from_envelope(self):
        stream=CenterStream(2)
        seq=units([110,100,108,101,109,98,99,95,97],1)
        for u in seq[:5]:stream.append(u)
        self.assertEqual(len(stream.closed),0)
        stream.append(seq[5])
        self.assertEqual(len(stream.closed),1)
        z=deepcopy(stream.closed[0]);self.assertEqual((z['zd'],z['zg']),(101,108))
        self.assertEqual((z['dd'],z['gg']),(100,110));self.assertEqual(z['confirmed_idx'],seq[5].available_idx)
        for u in seq[6:]:stream.append(u)
        self.assertEqual(stream.closed[0],z)

    def proof_fixture(self):
        bars=sample_bars(140);bars[['open','close']]=90.;bars.high=91.;bars.low=89.
        model=RecursiveChan(bars);u=units([150,140,148,138,145,135,142,132,139,129,136,126,133,123,130,120,127,117,124,114,121,111,118,108,115,105,112,102,109,99,106,96,103,93,100,90,97,87,94,84,91,81,88,78,85,75,82,72,79,69],1)
        # Explicit completed structures isolate the strict evidence predicate.
        A={'first':2,'last':5,'gg':145.,'dd':130.,'zd':135.,'zg':140.,'start_idx':2,'end_idx':6,'confirmed_idx':10,'exit_direction':-1,'exit_child':6,'pullback_child':7}
        B={'first':12,'last':15,'gg':120.,'dd':100.,'zd':105.,'zg':115.,'start_idx':12,'end_idx':16,'confirmed_idx':20,'exit_direction':-1,'exit_child':16,'pullback_child':17}
        u[16]=Line(16,1,50,100,110,50,120);u[17]=Line(17,1,100,110,50,90,125)
        u[12]=Line(12,1,30,35,110,115,40)
        u[11]=Line(11,1,10,30,150,95,45)
        model.centres[2].units=u;model.centres[2].closed=[A,B]
        model.layers[2]=[Line(0,2,10,30,150,95,45)]
        c=u[16]
        subA={'start_idx':60,'end_idx':70,'gg':90.,'dd':80.}
        subB={'start_idx':80,'end_idx':90,'gg':75.,'dd':65.}
        model.centres[1].closed=[{**subA,'confirmed_idx':75},{**subB,'confirmed_idx':95}]
        model.signals=[{'rank':1,'point_idx':100,'confirmed_idx':115,'evidence':{'A':subA,'B':subB}}]
        model._momentum=lambda *_:{'weakening':True,'entry_negative_area':100.,'exit_negative_area':40.}
        # This fixture isolates the outer predicate. Mock-free recursive OHLC
        # coverage lives in test_strict_capability, including the positive path.
        def lower_proof(*_):
            if len(model.centres[1].closed)<2:return None
            return {'rank':1,'point_idx':100,'confirmed_idx':115,
                    'evidence':{'A':subA,'B':subB,'checks':{
                        'envelopes_descend_without_overlap':subB['gg']<subA['dd'],
                        'terminal':any(s['rank']==1 and s['point_idx']==100 for s in model.signals)}}}
        model._terminal_proof=lower_proof
        return model,c

    def test_center_reentry_extends_envelope_instead_of_creating_a_trend(self):
        stream=CenterStream(2);seq=units([110,100,108,101,109,95,106,90,95],1)
        for u in seq[:6]:stream.append(u)
        self.assertFalse(stream.closed)
        self.assertEqual(stream.preview()['dd'],95)
        for u in seq[6:]:stream.append(u)
        self.assertEqual(len(stream.closed),1)
        self.assertEqual((stream.closed[0]['zd'],stream.closed[0]['zg'],stream.closed[0]['dd']),(101,108,95))

    def test_unobserved_future_crash_cannot_change_confirmed_history(self):
        bars=sample_bars(1300);cut=900;original=analyze(bars)
        changed=bars.copy();changed.loc[cut:,'close']*=.4;changed.loc[cut:,'open']=changed.close.iloc[cut:]
        changed.loc[cut:,'high']=changed.close.iloc[cut:]+1;changed.loc[cut:,'low']=changed.close.iloc[cut:]-1
        after=analyze(changed)
        for rank in original['layers']:
            known=lambda m:[u for u in m['layers'][rank] if u['available_idx']<cut]
            self.assertEqual(known(original),known(after))

    def test_strict_buy_needs_full_envelopes_terminal_sublevel_and_no_intervening_low(self):
        m,c=self.proof_fixture();m._detect(2,c)
        self.assertTrue(m.signals[-1].get('strict_recursive'))
        for change,failed in [
            (lambda m:setattr(m,'signals',[]),'terminal_sublevel_divergence'),
            (lambda m:m.centres[2].closed[-1].update(gg=135.),'envelopes_descend_without_overlap'),
            (lambda m:m.bars.loc.__setitem__((110,'low'),49.),'no_new_low_before_confirmation'),
            (lambda m:m.centres[1].closed.clear(),'two_descending_subcentres')]:
            m,c=self.proof_fixture();change(m);m._detect(2,c)
            self.assertFalse(m.diagnostics[-1]['evidence']['checks'][failed])
            self.assertFalse(any(s.get('strict_recursive') for s in m.signals))

    def test_confirmation_tail_center_cannot_replace_the_centers_before_c(self):
        m,c=self.proof_fixture();later={**m.centres[2].closed[-1],
            'first':25,'last':28,'start_idx':75,'end_idx':85,'confirmed_idx':115,
            'exit_child':29,'pullback_child':30}
        m.centres[2].closed.append(later);m._detect(2,c)
        self.assertTrue(m.signals[-1].get('strict_recursive'))
        self.assertEqual(m.signals[-1]['evidence']['B']['end_idx'],16)

    def test_recursive_geometry_and_signals_equal_every_observable_prefix(self):
        bars=sample_bars(1300);full=analyze(bars)
        for n in range(180,1301,43):
            prefix=analyze(bars.iloc[:n])
            for rank in full['layers']:
                self.assertEqual(prefix['layers'][rank],[u for u in full['layers'][rank] if u['available_idx']<n])
            for rank in full['centres']:
                self.assertEqual([z for z in prefix['centres'][rank] if not z['provisional']],
                                 [z for z in full['centres'][rank] if not z['provisional'] and z['confirmed_idx']<n])
            stable=lambda s:{k:v for k,v in s.items() if k!='invalidated_idx'}
            self.assertEqual([stable(s) for s in prefix['signals']],
                             [stable(s) for s in full['signals'] if s['confirmed_idx']<n])

    def test_ledger_distinguishes_point_confirmation_first_observed_and_invalidation(self):
        bars=sample_bars(40);bars.low=99.;raw={'rank':2,'point_idx':10,'confirmed_idx':20,'stop':90.,'strict_recursive':True,'evidence':{'A':{'start_idx':1},'B':{'end_idx':10}}}
        mock={'layers':{},'centres':{},'signals':[raw],'diagnostics':[]}
        now=datetime(2026,10,19,10,tzinfo=TZ)
        with TemporaryDirectory() as tmp,patch.object(strict_events,'analyze',side_effect=lambda *_:deepcopy(mock)):
            first=strict_events.snapshot('801081','D',bars.iloc[:25],tmp,now)['signals'][0]
            self.assertTrue(first['backfilled']);self.assertGreater(first['confirmed_at'],first['point_at'])
            gapped=strict_events.snapshot('801081','D',bars,tmp,now,complete=False,gaps=[bars.dt.iloc[30].strftime('%Y%m%d')])
            self.assertEqual(gapped['signals'][0]['status'],'monitoring_gap')
            self.assertEqual(gapped['signals'][0]['confirmed_at'],first['confirmed_at'])
            bars.loc[31,'low']=89.
            second=strict_events.snapshot('801081','D',bars,tmp,now)['signals'][0]
            self.assertEqual(second['status'],'invalidated')
            for k in ['id','point_at','confirmed_at','observed_at','stop']:self.assertEqual(first[k],second[k])
            strict_events.snapshot('801081','D',bars,tmp,now)
            ledger=Path(tmp,'signal_ledger/801081_D.jsonl').read_text().splitlines()
            self.assertEqual(sum(json.loads(r)['event']=='observed' for r in ledger),1)
            self.assertEqual(sum(json.loads(r)['event']=='invalidated' for r in ledger),1)
            blocked=strict_events.snapshot('801081','D',bars,tmp,now,conflicts=[{'date':bars.dt.iloc[5].strftime('%Y%m%d')}])
            self.assertEqual(blocked['signals'][0]['status'],'withdrawn')
            self.assertEqual(len(blocked['blocked_candidates']),1)


class RepairContracts(unittest.TestCase):
    def test_calendar_does_not_fabricate_mid_autumn_holiday(self):
        self.assertNotIn('20260925',exchange_dates('20260924','20260928'))
        self.assertIn('20260928',exchange_dates('20260924','20260928'))

    def test_repair_keeps_primary_conflicts_and_records_first_available_date(self):
        bars=sample_bars(60);days=bars.dt.dt.strftime('%Y%m%d').tolist();missing=days[30]
        original=bars.drop(index=30).reset_index(drop=True)
        data=[]
        for i,r in bars.iterrows():
            data.append({'date':r['dt'].strftime('%Y-%m-%d'),'open':str(r.open),'high':str(r.high+(.2 if i==15 else 0)),
                         'low':str(r.low),'last':str(r.close),'volume':'100'})
        def fetch(url):
            if 'qt.gtimg' in url:return 'v="1~半导体~801081~1";'.encode('gb18030')
            return json.dumps({'ok':True,'data':data}).encode()
        with TemporaryDirectory() as tmp,patch('sw_repair.exchange_dates',return_value=days):
            f,a=repair('801081','半导体',original,days[-1],Path(tmp),fetch)
            self.assertEqual(len(f),len(bars));self.assertEqual(a['remaining'],[])
            self.assertEqual([r['date'] for r in a['filled']],[missing])
            self.assertEqual(f.high.iloc[15],bars.high.iloc[15])
            self.assertEqual(a['conflicts'][0]['kept'],'official')
            self.assertTrue(a['filled'][0]['available_at'].startswith(datetime.now(TZ).date().isoformat()))
            self.assertIn('sha256',a['filled'][0])

    def test_wrong_identity_or_price_series_cannot_fill_gaps(self):
        bars=sample_bars(60);days=bars.dt.dt.strftime('%Y%m%d').tolist()
        with TemporaryDirectory() as tmp,patch('sw_repair.exchange_dates',return_value=days):
            f,a=repair('801081','半导体',bars.drop(index=30),days[-1],Path(tmp),lambda _: 'v="1~其他行业~801081~1";'.encode('gb18030'))
            self.assertEqual(len(f),59);self.assertEqual(a['filled'],[]);self.assertTrue(a['errors'])

    def test_same_name_but_unmatched_prices_cannot_fill_gaps(self):
        bars=sample_bars(60);days=bars.dt.dt.strftime('%Y%m%d').tolist()
        rows=[{'date':r['dt'].strftime('%Y-%m-%d'),'open':float(r.open+100),'high':float(r.high+100),
               'low':float(r.low+100),'last':float(r.close+100),'volume':100} for _,r in bars.iterrows()]
        def fetch(url):
            if 'qt.gtimg' in url:return 'v="1~半导体~801081~1";'.encode('gb18030')
            return json.dumps({'ok':True,'data':rows}).encode()
        with TemporaryDirectory() as tmp,patch('sw_repair.exchange_dates',return_value=days):
            f,a=repair('801081','半导体',bars.drop(index=30),days[-1],Path(tmp),fetch)
            self.assertEqual(len(f),59);self.assertFalse(a['filled']);self.assertTrue(a['remaining'])

    def test_short_window_revision_expands_comparison_without_relaxing_threshold(self):
        bars=sample_bars(220);days=bars.dt.dt.strftime('%Y%m%d').tolist();original=bars.drop(index=30)
        rows=[{'date':r['dt'].strftime('%Y-%m-%d'),'open':float(r.open),'high':float(r.high),
               'low':float(r.low),'last':float(r.close+(.02 if i==15 else 0)),'volume':100}
              for i,r in bars.iterrows()]
        calls=[]
        def fetch(url):
            calls.append(url)
            if 'qt.gtimg' in url:return 'v="1~半导体~801081~1";'.encode('gb18030')
            return json.dumps({'ok':True,'data':rows}).encode()
        with TemporaryDirectory() as tmp,patch('sw_repair.exchange_dates',return_value=days):
            cache=Path(tmp,'repairs/raw/801081_2025.json');cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({'ok':True,'data':rows[:60]}))
            cache.with_suffix('.meta.json').write_text(json.dumps({'url':'batch-window','retrieved_at':'2026-10-19','sha256':'initial'}))
            f,a=repair('801081','半导体',original,days[-1],Path(tmp),fetch)
            self.assertFalse(a['remaining']);self.assertEqual(len(f),220)
            self.assertGreaterEqual(a['filled'][0]['close_agreement'],.99)
            self.assertEqual(len([u for u in calls if 'kline' in u]),1)
            self.assertEqual(f.close.iloc[15],bars.close.iloc[15])

    def test_small_relative_quote_precision_is_still_recorded_as_a_strict_conflict(self):
        bars=sample_bars(60)
        for col in ['open','high','low','close']:bars[col]+=20000
        days=bars.dt.dt.strftime('%Y%m%d').tolist()
        rows=[{'date':r['dt'].strftime('%Y-%m-%d'),'open':float(r.open),'high':float(r.high),
               'low':float(r.low),'last':float(r.close+(.02 if i==15 else 0)),'volume':100} for i,r in bars.iterrows()]
        def fetch(url):
            if 'qt.gtimg' in url:return 'v="1~半导体~801081~1";'.encode('gb18030')
            return json.dumps({'ok':True,'data':rows}).encode()
        with TemporaryDirectory() as tmp,patch('sw_repair.exchange_dates',return_value=days):
            f,a=repair('801081','半导体',bars.drop(index=30),days[-1],Path(tmp),fetch)
            self.assertFalse(a['remaining']);self.assertEqual(len(a['conflicts']),1)
            self.assertEqual(a['conflicts'][0]['date'],days[15])
            self.assertEqual(f.close.iloc[15],bars.close.iloc[15])

if __name__=='__main__':unittest.main()
