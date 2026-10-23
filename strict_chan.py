"""Online recursive Chan structure with immutable confirmation events.

R0 is an explicitly declared indivisible floor of frozen OHLC strokes.
R1+ segments use the pinned MIT feature-sequence algorithm (including gaps),
not fixed groups of three strokes. R2+ centres contain completed segments.
Calendar frequency and recursive structural rank are separate dimensions.
"""
from __future__ import annotations
import numpy as np

from chan_core import ChanEngine
from chan_vendor.line import Line
from chan_vendor.enums import BI_DIR
from chan_vendor.eigen_fx import CEigenFX

RULE_VERSION = 'chan-recursive-1.1.0'
HISTORY_START = '2022-01-01'


class FrozenStrokeEngine(ChanEngine):
    def __init__(self, bars, on_stroke=None, **kwargs):
        super().__init__(bars, **kwargs)
        self.frozen = []
        self.on_stroke = on_stroke

    def _source_extreme(self, mi, kind):
        bar = self.merged[mi]
        price = bar.high if kind > 0 else bar.low
        values = (self.high if kind > 0 else self.low)[bar.start_idx:bar.end_idx+1]
        matches = np.flatnonzero(np.isclose(values, price, rtol=0, atol=1e-8))
        if not len(matches): raise ValueError('merged extreme has no observation')
        return float(price), bar.start_idx+int(matches[-1])

    def _on_fractal(self, f):
        before = len(self.strokes)
        super()._on_fractal(f)
        if len(self.strokes)>before and len(self.strokes)>=2:
            s = self.strokes[-2]
            assert not s.provisional
            # The endpoint's own fractal confirmation is NOT the time at which
            # the stroke freezes. Only the new opposite stroke proves that.
            unit = Line(len(self.frozen), 0, s.start_idx, s.end_idx,
                        s.start_price, s.end_price, f.avail_idx,
                        evidence={'endpoint_fractal_idx':s.confirm_idx,
                                  'frozen_by_opposite_fractal_idx':f.avail_idx})
            self.frozen.append(unit)
            if self.on_stroke: self.on_stroke(unit)


class SegmentStream:
    """Append-only counterpart to upstream SegListChan, with no virtual tail.

    Only frozen child lines are exposed, including through .next. Feature
    sequence end tests therefore cannot walk into future or provisional data.
    The conservative upstream nested-gap condition is deliberately retained.
    """
    def __init__(self, rank, observed_tail=None):
        self.rank, self.units, self.lines = rank, [], []
        self.bootstrap_start = 0
        self.observed_tail = observed_tail

    def append(self, unit):
        assert unit.idx == len(self.units)
        if self.units:
            assert self.units[-1].dir != unit.dir
            self.units[-1].next = unit; unit.pre = self.units[-1]
        self.units.append(unit)
        created = []
        search = self.lines[-1].child_end+1 if self.lines else self.bootstrap_start
        while search<len(self.units):
            up, down = CEigenFX(BI_DIR.UP), CEigenFX(BI_DIR.DOWN)
            last_dir = self.lines[-1].dir if self.lines else None
            restart = None
            for bi in self.units[search:]:
                found = None
                if bi.is_down() and last_dir != BI_DIR.UP:
                    if up.add(bi): found = up
                elif bi.is_up() and last_dir != BI_DIR.DOWN:
                    if down.add(bi): found = down
                # First direction follows the established characteristic pair,
                # not whichever tentative fractal happens to appear first.
                if not self.lines:
                    if up.ele[1] is not None and bi.is_down():
                        last_dir=BI_DIR.DOWN; down.clear()
                    elif down.ele[1] is not None and bi.is_up():
                        up.clear(); last_dir=BI_DIR.UP
                    if up.ele[1] is None and last_dir==BI_DIR.DOWN and bi.is_down(): last_dir=None
                    elif down.ele[1] is None and last_dir==BI_DIR.UP and bi.is_up(): last_dir=None
                if not found: continue
                test = found.can_be_end(self.units)
                if test is None: return created
                if not test:
                    restart=found.lst[1].idx
                    break
                end = found.GetPeakBiIdx()
                start = self.lines[-1].child_end+1 if self.lines else self.bootstrap_start
                if not self.lines:
                    while start<=end and self.units[start].dir!=found.dir: start+=1
                if end-start<2 or self.units[start].dir!=self.units[end].dir:
                    restart=end+1
                    if not self.lines: self.bootstrap_start=restart
                    break
                first, last=self.units[start], self.units[end]
                values=self.units[start:end+1]
                valid=(last.end_price>first.start_price and last.end_price>=max(x._high() for x in values)) if last.is_up() else (last.end_price<first.start_price and last.end_price<=min(x._low() for x in values))
                # A frozen child arrives with a delay. Its not-yet-released
                # successor may already have exceeded this candidate endpoint.
                # Those observations are known NOW, even though they cannot yet
                # be used as a completed feature element. Ignoring them freezes
                # an impossible endpoint and can strand every subsequent line.
                if valid and self.observed_tail is not None:
                    bound=self.observed_tail(self.units[-1].end_idx+1,
                                             self.units[-1].available_idx,last.is_up())
                    if bound is not None:
                        valid=bound<=last.end_price+1e-8 if last.is_up() else bound>=last.end_price-1e-8
                if not valid:
                    restart=end+1
                    if not self.lines: self.bootstrap_start=restart
                    break
                line=Line(len(self.lines),self.rank,first.start_idx,last.end_idx,
                          first.start_price,last.end_price,self.units[-1].available_idx,start,end,
                          {'feature_gap':bool(found.ele[1].gap),
                           'feature_elements':[[x.idx for x in e.lst] for e in found.ele],
                           'last_available_child':self.units[-1].idx,
                           'bootstrap':not bool(self.lines)})
                self.lines.append(line);created.append(line)
                restart=end+1
                break
            if restart is None: return created
            if restart<=search: raise RuntimeError('feature sequence did not advance')
            search=restart
        return created


class CenterStream:
    """Three completed lower-rank movements; extension closes on a 3-buy/sell.

    Only closed centres are frozen/emitted. An active centre is a preview.
    Entry/exit movements are excluded from the centre's DD/GG envelope.
    """
    def __init__(self, rank):
        self.rank,self.units,self.closed,self.active,self.seek = rank,[],[],None,0

    def append(self, line):
        self.units.append(line)
        if self.active is None:
            while self.seek+2<len(self.units):
                trio=self.units[self.seek:self.seek+3]
                zd,zg=max(x._low() for x in trio),min(x._high() for x in trio)
                if zg>zd:
                    self.active={'id':len(self.closed),'rank':self.rank,'first':self.seek,
                                 'formed_child':self.seek+2,'zd':zd,'zg':zg,
                                 'formed_idx':trio[-1].available_idx}
                    break
                self.seek+=1
        if self.active is None:return
        a=self.active
        if len(self.units)<a['first']+5:return
        leave,pullback=self.units[-2:]
        direction=-1 if leave.is_down() and leave.end_price<a['zd'] and pullback._high()<a['zd'] else 1 if leave.is_up() and leave.end_price>a['zg'] and pullback._low()>a['zg'] else 0
        if not direction:return
        end=leave.idx-1
        if end<a['formed_child']:return
        inside=self.units[a['first']:end+1]
        record={**a,'last':end,'dd':min(x._low() for x in inside),'gg':max(x._high() for x in inside),
                'start_idx':inside[0].start_idx,'end_idx':inside[-1].end_idx,
                'confirmed_idx':pullback.available_idx,'exit_child':leave.idx,'pullback_child':pullback.idx,
                'exit_direction':direction,'provisional':False}
        if self.closed:
            previous=self.closed[-1]
            record['relation_to_previous']='downtrend' if record['gg']<previous['dd'] else 'uptrend' if record['dd']>previous['gg'] else 'expansion_pending'
        else:record['relation_to_previous']='bootstrap'
        self.closed.append(record);self.active=None;self.seek=pullback.idx

    def preview(self):
        if self.active is None:return None
        inside=self.units[self.active['first']:]
        return {**self.active,'last':len(self.units)-1,'dd':min(x._low() for x in inside),
                'gg':max(x._high() for x in inside),'start_idx':inside[0].start_idx,
                'end_idx':inside[-1].end_idx,'provisional':True}


def _geometry(line):
    return {k:v for k,v in vars(line).items() if k not in ['pre','next']}


class RecursiveChan:
    def __init__(self, bars, max_rank=4):
        self.bars=bars.reset_index(drop=True);self.max_rank=max_rank
        self.layers=[[] for _ in range(max_rank+1)]
        self.segments={r:SegmentStream(r,self._observed_tail) for r in range(1,max_rank+1)}
        self.centres={r:CenterStream(r) for r in range(1,max_rank+1)}
        self.signals=[];self.diagnostics=[]
        self._signal_keys=set()
        self._diagnostic_keys={}
        self._pending={r:{} for r in range(1,max_rank+1)}
        self._closed_seen={r:0 for r in range(1,max_rank+1)}
        self.engine=FrozenStrokeEngine(self.bars,on_stroke=lambda u:self._push(0,u))

    def _observed_tail(self, start, available, is_up):
        values=(self.bars.high if is_up else self.bars.low).iloc[start:available+1]
        return float(values.max() if is_up else values.min()) if len(values) else None

    def _push(self, rank, unit):
        assert unit.available_idx>=unit.end_idx
        self.layers[rank].append(unit)
        if rank+1<=self.max_rank:
            self.centres[rank+1].append(unit)
            # Centre R(n+1) contains movements R(n). The old detector used
            # R(n+1) parent segments as their exits, making the proof misaligned.
            # A centre closes AFTER its exit: evaluate that stored exit when its
            # pullback supplies the final evidence, not only when c first arrives.
            self._scan(rank+1,unit.available_idx)
        if rank+1<=self.max_rank:
            for parent in self.segments[rank+1].append(unit):self._push(rank+1,parent)

    def _scan(self, rank, asof_idx):
        stream=self.centres[rank]
        if len(stream.closed)<2:return
        for B in stream.closed[max(1,self._closed_seen[rank]):]:
            c=stream.units[B['exit_child']]
            if c.is_down():self._pending[rank][B['start_idx']]=c
        self._closed_seen[rank]=len(stream.closed)
        for key,c in list(self._pending[rank].items()):
            proof=self._detect(rank,c,asof_idx)
            # Only missing internal confirmation can become true later. The
            # frozen outer geometry/momentum and a price already broken cannot.
            retry={'two_descending_subcentres','terminal_sublevel_divergence'}
            if proof is None or all(proof['evidence']['checks'].values()) or any(
                    not value and name not in retry for name,value in proof['evidence']['checks'].items()):
                self._pending[rank].pop(key,None)

    def _momentum(self, entering, leaving):
        area=lambda u:float(np.maximum(-self.engine.hist[u.start_idx:u.end_idx+1],0).sum())
        a,c=area(entering),area(leaving)
        return {'entry_negative_area':a,'exit_negative_area':c,'area_ratio':c/a if a>0 else None,
                'entry_dif':float(self.engine.dif[entering.end_idx]),'exit_dif':float(self.engine.dif[leaving.end_idx]),
                'weakening':bool(a>0 and c<a and self.engine.dif[leaving.end_idx]>self.engine.dif[entering.end_idx])}

    def _detect(self, rank, c, asof_idx=None):
        available=max(c.available_idx,asof_idx if asof_idx is not None else c.available_idx)
        candidate=self._proof(rank,c,self.centres[rank],available)
        if candidate is None:return
        key=(rank,candidate['evidence']['B']['start_idx'],c.end_idx)
        if key in self._signal_keys:return candidate
        if all(candidate['evidence']['checks'].values()):
            candidate['strict_recursive']=rank>=2
            self.signals.append(candidate)
            self._signal_keys.add(key)
            if rank<self.max_rank:self._scan(rank+1,available)
        elif key in self._diagnostic_keys:
            self.diagnostics[self._diagnostic_keys[key]]=candidate
        else:
            self._diagnostic_keys[key]=len(self.diagnostics)
            self.diagnostics.append(candidate)
        return candidate

    def _terminal_proof(self, rank, parent, available):
        """Decompose c locally, using only frozen observed child movements.

        A global active centre may cross the start of c. It is not c's internal
        decomposition. Re-index a bounded child stream and exclude its entry
        movement, then prove a terminal exit at the SAME endpoint recursively.
        Right-side children are allowed only up to the current known time.
        """
        children=[u for u in self.layers[rank-1]
                  if u.start_idx>=parent.start_idx and u.available_idx<=available]
        stream=CenterStream(rank)
        stream.seek=1  # first movement enters c's first internal centre
        for i,u in enumerate(children):
            stream.append(Line(i,u.rank,u.start_idx,u.end_idx,u.start_price,u.end_price,
                               u.available_idx,evidence=dict(u.evidence)))
        matching=[z for z in stream.closed
                  if z['start_idx']>=parent.start_idx and z['end_idx']<=parent.end_idx
                  and stream.units[z['exit_child']].end_idx==parent.end_idx]
        if not matching:return None
        B=matching[-1]
        # Later right-side centres must not replace the pair preceding this exit.
        stream.closed=[z for z in stream.closed if z['end_idx']<=B['end_idx']]
        return self._proof(rank,stream.units[B['exit_child']],stream,available)

    def _proof(self, rank, c, stream, available):
        centres=[z for z in stream.closed
                 if z['confirmed_idx']<=available and z['end_idx']<=c.start_idx]
        if len(centres)<2:return None
        A,B=centres[-2:]
        checks={'same_rank_centres':True,'child_rank_matches_centres':c.rank==rank-1,
                'consecutive_downward_exits':A['exit_direction']==B['exit_direction']==-1,
                'envelopes_descend_without_overlap':B['gg']<A['dd']}
        units=stream.units
        checks['no_bootstrap_segment_in_centres']=not any(u.evidence.get('bootstrap',False) for u in units[A['first']:B['last']+1])
        exit_unit=units[B['exit_child']];pull=units[B['pullback_child']]
        checks['c_starts_at_B_exit']=c.start_idx==exit_unit.start_idx and c.end_idx==exit_unit.end_idx
        checks['third_sell']=pull._high()<B['zd'] and exit_unit.is_down()
        entering=[x for x in units if x.is_down() and x.end_idx==units[B['first']].start_idx and x.available_idx<=available]
        if not entering:return None
        b=entering[-1]
        checks['new_low']=c.end_price<b.end_price and c.end_price<min(A['dd'],B['dd'])
        momentum=self._momentum(b,c);checks['trend_divergence']=momentum['weakening']
        checks['next_sublevel_complete']=c.available_idx<=available
        lower=None
        if rank>=2:
            lower=self._terminal_proof(rank-1,c,available)
            checks['two_descending_subcentres']=bool(lower and lower['evidence']['checks']['envelopes_descend_without_overlap'])
            checks['terminal_sublevel_divergence']=bool(lower and all(lower['evidence']['checks'].values()) and lower['point_idx']==c.end_idx)
        stop=c.end_price
        checks['no_new_low_before_confirmation']=bool((self.bars.low.iloc[c.end_idx+1:available+1]>=stop-1e-8).all())
        evidence={'A':dict(A),'B':dict(B),'entry':_geometry(b),'exit':_geometry(c),'momentum':momentum,
                  'checks':checks,'floor':'confirmed OHLC strokes','rank':rank}
        if lower is not None:evidence['sublevel']=lower
        candidate={'rank':rank,'point_idx':c.end_idx,'confirmed_idx':available,'stop':stop,'evidence':evidence}
        return candidate

    def run(self):
        self.engine.run()
        out=[]
        for s in self.signals:
            p=dict(s);p['invalidated_idx']=None
            later=np.flatnonzero(self.bars.low.to_numpy()[s['confirmed_idx']+1:]<s['stop']-1e-8)
            if len(later):p['invalidated_idx']=s['confirmed_idx']+1+int(later[0])
            out.append(p)
        return {'rule_version':RULE_VERSION,'history_start':self.bars.dt.iloc[0].strftime('%Y-%m-%d'),
                'history_policy_start':HISTORY_START,'base_floor':'OHLC确认笔',
                'layers':{str(r):[_geometry(u) for u in self.layers[r]] for r in range(1,self.max_rank+1)},
                'centres':{str(r):self.centres[r].closed+([self.centres[r].preview()] if self.centres[r].active else []) for r in range(2,self.max_rank+1)},
                'signals':out,'diagnostics':self.diagnostics[-30:],
                'intraday_nesting_verified':False}


def analyze(bars, max_rank=4):
    return RecursiveChan(bars,max_rank).run()
