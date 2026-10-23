"""Versioned, causal teaching replays. Not a factor/portfolio backtest."""
from pathlib import Path
import argparse
import hashlib
import json
import numpy as np
import pandas as pd
from strict_chan import RecursiveChan, RULE_VERSION
from chan_core import macd
from chan_phase_engine import chan_phases
from trend_analysis import wave_series, clean, write_json

HERE=Path(__file__).resolve().parent
SPECS=[
    ('bull2014','2014 · 牛市启动与延续','2014-06-03','2015-06-12','2014-07-31',
     [('2014-06-25','底分型确认'),('2014-07-24','敏感转强'),('2014-07-31','平滑转强'),('2014-11-24','趋势延续'),('2015-06-12','高位行情')],
     '平滑线7月31日才越过50分，比6月20日收盘已高8.63%；随后直到2015年6月12日未再跌破中线。它在这段行情里稳定了趋势判断，同时牺牲最早入场位置。'),
    ('turn2015','2015 · 牛熊转换','2015-05-04','2015-09-30','2015-06-26',
     [('2015-06-12','高位行情'),('2015-06-18','敏感下穿'),('2015-06-26','慢线仍偏高'),('2015-07-13','反弹与分型'),('2015-08-07','慢线下穿'),('2015-08-24','反弹后再破低')],
     '敏感线6月18日跌破50分时，指数较6月12日收盘已跌7.37%；慢线到8月7日才下穿，累计已跌27.53%。慢线混合了方向和不区分涨跌的趋势强度，不能解释成上涨概率。'),
    ('range2021','2021 · 区间震荡','2021-03-01','2021-12-31','2021-06-15',
     [('2021-05-26','慢线转强'),('2021-06-10','敏感上穿'),('2021-06-15','敏感反向'),('2021-08-20','慢线下穿'),('2021-08-24','慢线又上穿'),('2021-10-19','慢线再下穿')],
     '窗口内敏感线穿越50分23次，慢线6次。穿越次数不是亏损次数；频繁翻转揭示了震荡时的交易摩擦和假突破风险，大盘门控也可能过滤独立行业行情。'),
    ('false2022','2022 · 放量反弹仍然失败','2022-03-01','2022-05-31','2022-03-21',
     [('2022-03-16','放量反弹'),('2022-03-21','底分型确认'),('2022-03-22','敏感上穿'),('2022-03-25','敏感下穿'),('2022-04-25','跌破3023.30'),('2022-04-27','再创新低')],
     '3月16日确实放量反弹：额比约1.22，不是缺少成交量。3月21日确认3023.30的底分型，但4月25日仍破低。反弹成交活跃能证明参与增强，不能证明下降趋势已经结束。'),
    ('jump2024','2024 · 急涨与巨量回撤','2024-09-02','2024-11-15','2024-10-08',
     [('2024-09-23','底分型与敏感转强'),('2024-09-24','量价改善'),('2024-09-25','风险提示也会过早'),('2024-09-26','放量突破'),('2024-09-30','慢线仍滞后'),('2024-10-08','巨量高开低走'),('2024-10-09','放量下跌')],
     '9月24日额比约1.82，9月26日放量突破此前20日高点，比慢线更早提供转强证据。10月8日额比约4.39，却开盘至收盘跌5.02%；巨量代表分歧与交换，不能当成净流入。9月25日同类风险警示后行情仍上涨，警示不等于自动清仓。')]


def volume_features(bars):
    f=bars.copy()
    volume=f['volume'] if 'volume' in f else f.get('vol',pd.Series(np.nan,index=f.index))
    amount=f.get('amount',pd.Series(np.nan,index=f.index))
    f['volume_ratio20']=volume/volume.rolling(20,min_periods=20).mean().shift(1).replace(0,np.nan)
    f['amount_ratio20']=amount/amount.rolling(20,min_periods=20).mean().shift(1).replace(0,np.nan)
    f['amount_yi']=amount/100000  # Tushare index_daily: CNY 1,000
    f['volume_yi']=volume/1000000  # hands of 100 shares -> 100 million shares
    spread=(f.high-f.low).replace(0,np.nan)
    f['clv']=((2*f.close-f.high-f.low)/spread).fillna(0)
    f['prior20_high']=f.high.rolling(20,min_periods=20).max().shift(1)
    f['ret_pct']=f.close.pct_change(fill_method=None)*100
    f['open_ret_pct']=(f.close/f.open-1)*100
    f['volume_rebound']=(f.ret_pct>0)&(f.volume_ratio20>=1.2)&(f.amount_ratio20>=1.2)&(f.clv>=.5)
    f['volume_breakout']=f.volume_rebound&(f.close>f.prior20_high)
    f['volume_rejection']=(f.amount_ratio20>=2)&((f.open_ret_pct<0)|(f.clv<0))
    return f


def synthetic_bars():
    """Deterministic nested proof fixture, explicitly not exchange history."""
    macro=[170,150,160,153,158,130,140,132,139,110,120,112,119,100,108,102,107,104,109,105,111]
    points=[macro[0]]
    for a,z in zip(macro,macro[1:]):
        points.extend(a+(z-a)*t for t in [.25,.10,.20,.12,.85,.70,.80,.72,1])
    close=np.interp(np.arange((len(points)-1)*8+1),np.arange(len(points))*8,points)
    return pd.DataFrame({'dt':pd.bdate_range('2010-01-01',periods=len(close)),
                         'open':close,'close':close,'high':close+.001,'low':close-.001,
                         'volume':np.nan,'amount':np.nan})


def geometry(u):
    return [u.start_idx,u.end_idx,u.start_price,u.end_price,u.available_idx]


def compute(bars, windows):
    model=RecursiveChan(bars)
    capture=set(i for lo,hi in windows for i in range(lo,hi+1))
    tails={}
    for t in range(len(bars)):
        model.engine._append_or_merge(t)
        if t in capture:
            s=model.engine.strokes[-1] if model.engine.strokes else None
            tails[t]=[s.start_idx,s.end_idx,s.start_price,s.end_price] if s else []
    return model,tails


def export_case(model,tails,bars,lo,hi,waves,phases,synthetic=False):
    idx=range(lo,hi+1);vf=volume_features(bars)
    rows=[]
    for i in idx:
        r=bars.iloc[i];v=vf.iloc[i];w=waves.get(r['dt'].strftime('%Y%m%d'),{})
        rows.append([i,('第'+str(i+1)+'根') if synthetic else r['dt'].strftime('%Y-%m-%d'),
                     r.open,r.high,r.low,r.close,
                     None if synthetic else w.get('p_impulse',np.nan)*100,
                     None if synthetic else w.get('p_fast',np.nan)*100,
                     int(phases[i]) if np.isfinite(phases[i]) else 0,tails[i],
                     v.volume_yi,v.amount_yi,v.volume_ratio20,v.amount_ratio20,v.clv,
                     v.prior20_high,v.ret_pct,bool(v.volume_rebound),bool(v.volume_breakout),
                     bool(v.volume_rejection)])
    facts=[{'point':f.ext_idx,'available':f.avail_idx,'low':f.price}
           for f in model.engine.fractals if f.kind==-1 and lo<=f.avail_idx<=hi]
    signals=[]
    for s in model.signals:
        if not lo<=s['confirmed_idx']<=hi:continue
        x=dict(s);later=np.flatnonzero(bars.low.to_numpy()[s['confirmed_idx']+1:]<s['stop']-1e-8)
        x['invalidated_idx']=s['confirmed_idx']+1+int(later[0]) if len(later) else None
        signals.append(x)
    return {'rows':rows,'strokes':[geometry(u) for u in model.layers[0] if u.end_idx>=lo and u.start_idx<=hi and u.available_idx<=hi],
            'segments':[geometry(u) for u in model.layers[1] if u.end_idx>=lo and u.start_idx<=hi and u.available_idx<=hi],
            'centres':[{k:z[k] for k in ('rank','start_idx','end_idx','zd','zg','confirmed_idx')}
                       for z in model.centres[2].closed if z['end_idx']>=lo and z['start_idx']<=hi and z['confirmed_idx']<=hi],
            'fractals':facts,'signals':signals,'synthetic':synthetic}


def build(history,output):
    bars=pd.read_pickle(history).copy()
    bars['dt']=pd.to_datetime(bars.trade_date);bars['volume']=bars.vol
    bars=bars.sort_values('dt').reset_index(drop=True)
    windows=[(int(bars.index[bars.dt>=s[2]][0]),int(bars.index[bars.dt<=s[3]][-1])) for s in SPECS]
    model,tails=compute(bars,windows)
    waves={r['signal_date']:r for r in wave_series(bars)}
    phases=chan_phases(bars.close.to_numpy(),macd(bars.close)[0].to_numpy())[0]
    cases=[]
    for spec,(lo,hi) in zip(SPECS,windows):
        ident,name,_,_,default,events,lesson=spec
        c=export_case(model,tails,bars,lo,hi,waves,phases)
        c.update(id=ident,name=name,asset='上证指数 000001.SH',default=default,events=events,lesson=lesson)
        cases.append(c)
    demo=synthetic_bars();dm,dt=compute(demo,[(280,1150)])
    c=export_case(dm,dt,demo,280,1150,{},np.zeros(len(demo)),True)
    strict=next(s for s in dm.signals if s['strict_recursive'])
    c.update(id='proof',name='模拟样例 · 严格一买完整证明',asset='模拟价格序列 · 非真实行情',
             default='第'+str(strict['confirmed_idx']+1)+'根',
             events=[['第'+str(strict['point_idx']+1)+'根','低点发生'],['第'+str(strict['confirmed_idx'])+'根','确认前一根'],
                     ['第'+str(strict['confirmed_idx']+1)+'根','严格递归一买确认']],
             lesson='该样例用于验证识别能力，不是交易所行情或盈利案例。两层下降中枢、末端递归背驰和确认前不破低全部成立后才发出R2信号；低点第937根，直到第1059根收盘后确认。')
    cases.append(c)
    payload={'available':True,'rule_version':RULE_VERSION,'asset':'000001.SH','cases':cases,
             'meta':{'warmup':str(bars.dt.iloc[0].date()),'last_history':str(bars.dt.iloc[-1].date()),'n_bars':len(bars),
                     'data_sha256':hashlib.sha256(Path(history).read_bytes()).hexdigest(),
                     'volume_baseline':'过去20个交易日均值，排除当日；Tushare volume=手、amount=千元',
                     'historical_factors_available':False,'is_portfolio_backtest':False,
                     'sources':['https://tushare.pro/document/2?doc_id=95','https://blog.sina.com.cn/s/blog_486e105c01000c16.html'],
                     'scope':'日K冻结笔基底的递归结构；不等于实际分钟区间套。P为原七/四规则倾向分，不是校准概率。'}}
    write_json(output,payload)
    return payload


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--history',type=Path,required=True)
    p.add_argument('--output',type=Path,default=HERE/'data/chan_classic/cases.json');a=p.parse_args()
    result=build(a.history,a.output)
    print(json.dumps({'cases':len(result['cases']),'rule_version':result['rule_version'],'bytes':a.output.stat().st_size}))
