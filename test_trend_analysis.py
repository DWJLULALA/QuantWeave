"""Causality, calendar and comparable industry ranking contracts."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trend_analysis as trend


def sample_bars(n=260):
    dates=pd.bdate_range('2025-01-02',periods=n)+pd.Timedelta(hours=15)
    close=100+np.arange(n)*.05+6*np.sin(np.arange(n)/7)+2*np.cos(np.arange(n)/19)
    return pd.DataFrame({'dt':dates,'open':close+.1,'high':close+1,'low':close-1,'close':close,'volume':1000})


class TrendContracts(unittest.TestCase):
    def test_sw2021_catalog_has_full_hierarchy_not_only_published_indices(self):
        first={c:r for c,r in trend.SW.items() if r['taxonomy']=='sw1'}
        second={c:r for c,r in trend.SW.items() if r['taxonomy']=='sw2'}
        self.assertEqual((len(first),len(second)),(31,134))
        self.assertTrue(all(r['parent_code'] in first and r['parent_name']==first[r['parent_code']]['name'] for r in second.values()))
        self.assertEqual(second['801081']['parent_name'],'电子')

    def test_bottom_fractal_waits_for_closed_right_bar_and_new_low_invalidates(self):
        close=np.array([110,108,106,104,102,100,102,104,105],float)
        bars=pd.DataFrame({'dt':pd.bdate_range('2026-01-01',periods=9)+pd.Timedelta(hours=15),'open':close,'close':close,'high':close+1,'low':close-1,'volume':100})
        engine=trend.TrendChan(bars.iloc[:7]);engine.run()
        self.assertFalse(trend.bottom_diagnosis(bars.iloc[:7],engine)['fractal_confirmed'])
        engine=trend.TrendChan(bars);engine.run();out=trend.bottom_diagnosis(bars,engine)
        self.assertTrue(out['fractal_confirmed']);self.assertFalse(out['strict_1buy_confirmed'])
        self.assertEqual(out['confirm_dt'],bars.dt.iloc[7].strftime('%Y-%m-%d %H:%M'))
        new=bars.iloc[[-1]].copy();new['dt']=new.dt+pd.Timedelta(days=1)
        new[['open','close','high','low']]=[97,97,98,96]
        extended=pd.concat([bars,new],ignore_index=True)
        engine=trend.TrendChan(extended);engine.run()
        self.assertFalse(trend.bottom_diagnosis(extended,engine)['fractal_confirmed'])

    def test_live_downward_extension_can_only_be_momentum_candidate(self):
        bars=sample_bars(20);bars.loc[18,'low']=95
        hist=np.zeros(20);hist[1:6]=-10;hist[10:19]=-2
        dif=np.zeros(20);dif[5]=-10;dif[18]=-5
        engine=SimpleNamespace(fractals=[],merged=[],hist=hist,dif=dif,
            strokes=[SimpleNamespace(direction=-1,start_idx=1,end_idx=5,start_price=120,end_price=100)],
            points=[{'kind':1,'ext_idx':10,'price':110}])
        result=trend.bottom_diagnosis(bars,engine)
        self.assertEqual(result['status'],'weakening')
        self.assertTrue(result['momentum']['weakening'])
        self.assertFalse(result['fractal_confirmed'])
        self.assertIsNone(result['strict_1buy_confirmed'])

    def test_reference_rank_keeps_exact_benchmark_horizon_despite_gap(self):
        bars=sample_bars(170)
        prices=[{'date':d.strftime('%Y%m%d'),'close':float(p)} for d,p in zip(bars.dt,bars.close)]
        own=[{'date':r['date'],'close':r['close']*(1.001**i)} for i,r in enumerate(prices)]
        assets={'801081':{'daily_prices':own[:-5]+own[-4:],'summary':{'code':'801081','phase_d':-1,'phase_w':0,'p_impulse':.3}}}
        strict,_=trend.rank_industries(assets,prices,prices[-1]['date'],['801081'])
        reference,_=trend.rank_industries(assets,prices,prices[-1]['date'],['801081'],allow_gaps=True)
        self.assertEqual(strict,[])
        self.assertAlmostEqual(reference[0]['rs20'],1.001**20-1,places=6)
        self.assertAlmostEqual(reference[0]['rs60'],1.001**60-1,places=6)
        self.assertEqual(reference[0]['rank_scope'],'reference')

    def test_published_snapshot_never_confirms_bottom_from_incomplete_ohlc(self):
        bars=sample_bars(330);gapped=bars.drop(index=325).reset_index(drop=True)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(trend,'ASSETS',{'000300':'沪深300','801081':'半导体'}), \
             patch.object(trend,'SW',{'801081':trend.SW['801081']}), \
             patch.object(trend,'SECTORS',{}), \
             patch.object(trend,'load_daily',return_value=(bars,{'url':'benchmark'})), \
             patch.object(trend,'load_sw_daily',return_value=(gapped,{'url':'official-sw'})), \
             patch.object(trend,'repair_sw',side_effect=lambda code,name,f,*args:(f,{'filled':[],'remaining':[],'conflicts':[]})), \
             patch.object(trend,'prefetch_sw'), \
             patch.object(trend,'exchange_dates',return_value=bars.dt.dt.strftime('%Y%m%d').tolist()), \
             patch.object(trend,'load_minute',side_effect=ValueError('unavailable')):
            snapshot=trend.build(Path(tmp),datetime(2026,10,19,tzinfo=trend.TZ))
            group=snapshot['classifications']['sw2']
            self.assertEqual(group['industries'],[])
            row=group['reference_industries'][0]
            self.assertEqual(row['bottom']['status'],'data_gap')
            self.assertFalse(row['bottom']['fractal_confirmed'])
            self.assertIsNone(row['bottom']['confirm_dt'])
            self.assertFalse(row['bottom']['strict_1buy_evaluated'])
            self.assertIsNone(row['bottom']['strict_1buy_confirmed'])
            chart=trend.load_asset('801081','D',Path(tmp))
            self.assertEqual(chart['chart']['bottom'],row['bottom'])
            month=trend.load_asset('801081','M',Path(tmp))
            self.assertTrue(month['chart']['data_quality']['complete'])
            self.assertNotEqual(month['chart']['bottom']['status'],'data_gap')
            self.assertFalse(trend.load_asset('801081','30F',Path(tmp))['available'])

    def test_wave_past_probabilities_do_not_change_when_future_is_added(self):
        bars=sample_bars(260)
        full=trend.wave_series(bars)
        for n in [170,210,250]:
            prefix=trend.wave_series(bars.iloc[:n])
            columns=['signal_date','p_impulse','p_fast','p_raw','index_pos','votes']
            self.assertEqual([{k:r[k] for k in columns} for r in full[:len(prefix)]],
                             [{k:r[k] for k in columns} for r in prefix])
        self.assertTrue(all(0<=r['p_impulse']<=1 and 0<=r['p_fast']<=1 for r in full))

    def test_unfinished_week_and_month_are_excluded(self):
        bars=sample_bars(260)
        bars['dt']=pd.bdate_range('2025-10-01',periods=260)+pd.Timedelta(hours=15)
        cutoff=pd.Timestamp('2026-08-19 15:00')
        f=bars[bars.dt<=cutoff]
        week=trend.completed_period(f,'W');month=trend.completed_period(f,'M')
        self.assertEqual(week.dt.iloc[-1],pd.Timestamp('2026-08-14 15:00'))
        self.assertEqual(month.dt.iloc[-1],pd.Timestamp('2026-07-31 15:00'))

    def test_daily_quote_does_not_admit_current_unclosed_bar(self):
        rows=[[20261015,100,102,99,101,1000],[20261016,101,103,100,102,1000],[20261019,102,104,101,103,1000]]
        with tempfile.TemporaryDirectory() as tmp, patch.object(trend,'fetch_json',return_value=({'code':'000001','kline':rows},{})):
            bars,_=trend.load_daily('000001',Path(tmp),datetime(2026,10,19,14,0,tzinfo=trend.TZ))
            self.assertEqual(bars.dt.iloc[-1],pd.Timestamp('2026-10-16 15:00'))

    def test_latest_provisional_move_can_extend_before_next_fractal(self):
        bars=sample_bars(260)
        out=trend.level_snapshot(bars,'D')
        self.assertTrue(out['available'])
        self.assertTrue(out['strokes'][-1]['provisional'])
        self.assertTrue(all(s['confirm_dt']>=s['end'] for s in out['strokes']))
        self.assertTrue(out['live'])

    def test_ranking_uses_one_date_and_ignores_retrospective_labels(self):
        bars=sample_bars(170)
        prices=[{'date':d.strftime('%Y%m%d'),'close':float(p)} for d,p in zip(bars.dt,bars.close)]
        asof=prices[-1]['date']
        def asset(code,growth):
            return {'daily_prices':[{'date':p['date'],'close':p['close']*(1+growth)**i} for i,p in enumerate(prices)],
                    'summary':{'code':code,'name':code,'phase_d':1,'phase_w':0,'p_impulse':.6,'label':0}}
        assets={'000986':asset('000986',.001),'000987':asset('000987',-.001)}
        rows,excluded=trend.rank_industries(assets,prices,asof)
        self.assertEqual(rows[0]['code'],'000986')
        self.assertTrue(all(r['asof']==asof and 0<=r['trend_score']<=100 for r in rows))
        assets['000986']['summary']['label']=1
        again,_=trend.rank_industries(assets,prices,asof)
        self.assertEqual([r['trend_score'] for r in rows],[r['trend_score'] for r in again])
        assets['000986']['daily_prices']=assets['000986']['daily_prices'][:-1]
        rows,_=trend.rank_industries(assets,prices,asof)
        self.assertEqual([r['code'] for r in rows],['000987'])

    def test_missing_intermediate_day_does_not_silently_change_60day_horizon(self):
        bars=sample_bars(170)
        prices=[{'date':d.strftime('%Y%m%d'),'close':float(p)} for d,p in zip(bars.dt,bars.close)]
        assets={'000986':{'daily_prices':prices[:-5]+prices[-4:],
                         'summary':{'code':'000986','phase_d':1,'phase_w':1,'p_impulse':.6}}}
        rows,excluded=trend.rank_industries(assets,prices,prices[-1]['date'])
        self.assertEqual(rows,[])
        self.assertIn('不连续',excluded[0]['reason'])

    def test_asset_loader_rejects_unknown_symbol_level_and_path_traversal(self):
        for code,level in [('../config','D'),('000001','HACK'),('999999','D')]:
            with self.assertRaises(ValueError):trend.load_asset(code,level)

    def test_stopped_industry_history_is_not_presented_as_current_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);generation='20261019T090000_abcdef'
            trend.write_json(root/'snapshot.json',{'available':True,'generation':generation,'generated_at':'2026-10-19T09:00:00+08:00','asof':'20261016'})
            trend.write_json(root/'generations'/generation/'801011.json',{'summary':{'name':'林业Ⅱ','asof':'20211130'},'levels':{}})
            out=trend.load_asset('801011','D',root)
            self.assertFalse(out['available'])
            self.assertIn('历史截止 20211130',out['reason'])

    def test_invalid_ohlc_fails_instead_of_drawing_fabricated_candles(self):
        bars=sample_bars(10);bars.loc[2,'high']=1
        with self.assertRaisesRegex(ValueError,'invalid OHLC'):trend.validate_bars(bars)


if __name__=='__main__':unittest.main()
