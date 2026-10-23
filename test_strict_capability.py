"""Regression cases for recognition capability, beyond no-repainting checks."""
from pathlib import Path
import unittest
import numpy as np
import pandas as pd
from strict_chan import RecursiveChan
from chan_vendor.line import Line


class RecognitionCapability(unittest.TestCase):
    @staticmethod
    def nested_market():
        # Clearly synthetic teaching input. Every macro movement contains two
        # separated subcentres; terminal declines lose momentum. No mocks.
        macro=[170,150,160,153,158,130,140,132,139,110,120,112,119,100,
               108,102,107,104,109,105,111]
        points=[macro[0]]
        for a,z in zip(macro,macro[1:]):
            points.extend(a+(z-a)*t for t in [.25,.10,.20,.12,.85,.70,.80,.72,1])
        close=np.interp(np.arange((len(points)-1)*8+1),np.arange(len(points))*8,points)
        return pd.DataFrame({'dt':pd.bdate_range('2010-01-01',periods=len(close)),
                             'open':close,'close':close,'high':close+.001,'low':close-.001})

    def test_complete_ohlc_pipeline_recognizes_recursive_buy_and_keeps_it_causal(self):
        bars=self.nested_market()
        full=RecursiveChan(bars).run()
        buys=[s for s in full['signals'] if s['strict_recursive']]
        self.assertTrue(buys,'positive capability must reach signals from raw OHLC')
        b=buys[0]
        self.assertEqual((b['rank'],b['point_idx'],b['stop']),(2,936,99.999))
        self.assertGreater(b['confirmed_idx'],b['point_idx'])
        self.assertTrue(all(b['evidence']['checks'].values()))
        self.assertTrue(all(b['evidence']['sublevel']['evidence']['checks'].values()))
        # The mark is unavailable one bar before the last required evidence.
        early=RecursiveChan(bars.iloc[:b['confirmed_idx']]).run()
        self.assertFalse(any(s['strict_recursive'] for s in early['signals']))
        ontime=RecursiveChan(bars.iloc[:b['confirmed_idx']+1]).run()
        stable=lambda s:{k:v for k,v in s.items() if k!='invalidated_idx'}
        self.assertEqual([stable(s) for s in ontime['signals']],
                         [stable(s) for s in full['signals'] if s['confirmed_idx']<=b['confirmed_idx']])
        changed=bars.copy();cut=b['confirmed_idx']+4
        for k in ('open','high','low','close'):changed.loc[cut:,k]*=.3
        after=RecursiveChan(changed).run()
        self.assertEqual([stable(s) for s in after['signals'] if s['confirmed_idx']<cut],
                         [stable(s) for s in full['signals'] if s['confirmed_idx']<cut])
        again=next(s for s in after['signals'] if s['strict_recursive'])
        self.assertEqual(again['invalidated_idx'],cut)
        self.assertEqual((again['point_idx'],again['confirmed_idx'],again['stop']),
                         (b['point_idx'],b['confirmed_idx'],b['stop']))

    def test_monotonic_decline_has_no_recursive_buy(self):
        bars=self.nested_market();v=np.linspace(200,50,len(bars))
        bars['open']=bars['close']=v;bars['high']=v+.01;bars['low']=v-.01
        self.assertFalse(RecursiveChan(bars).run()['signals'])

    def test_observed_unreleased_tail_cannot_contradict_frozen_segment_endpoint(self):
        bars = pd.read_csv(Path(__file__).parent/'tests/fixtures/sse_unreleased_tail.csv', parse_dates=['dt'])
        model = RecursiveChan(bars)
        model.engine.run()
        for line in model.layers[1]:
            last_child = model.layers[0][line.evidence['last_available_child']]
            tail = bars.iloc[last_child.end_idx+1:line.available_idx+1]
            if tail.empty: continue
            if line.is_up(): self.assertLessEqual(tail.high.max(),line.end_price+1e-8)
            else: self.assertGreaterEqual(tail.low.min(),line.end_price-1e-8)

    def test_closed_stroke_centres_emit_floor_buy_at_actual_confirmation(self):
        # Two disjoint descending centres. c is a child movement, not its parent.
        prices=[150,130,140,132,139,110,120,112,119,100,108]
        stride=30
        close=np.interp(np.arange((len(prices)-1)*stride+5),np.arange(len(prices))*stride,prices)
        bars=pd.DataFrame({'dt':pd.bdate_range('2020-01-01',periods=len(close)),
                           'open':close,'close':close,'high':close+.01,'low':close-.01})
        model=RecursiveChan(bars)
        # Frozen point lows align with their OHLC lows.
        for i,(a,b) in enumerate(zip(prices,prices[1:])):
            start,end=i*stride,(i+1)*stride
            model._push(0,Line(i,0,start,end,a,b-.01 if b<a else b+.01,end+2))
        buys=[s for s in model.signals if s['rank']==1]
        self.assertTrue(buys,'completed lower-rank centre exits must reach detection')
        signal=buys[-1]
        self.assertEqual(signal['point_idx'],9*stride)
        self.assertEqual(signal['confirmed_idx'],10*stride+2)
        self.assertFalse(signal['strict_recursive'])

if __name__=='__main__': unittest.main()
