#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波动门控（M1 思路）样本外验证：高波动=趋势期持有，低波动=震荡期空仓。
对比：方向门控（浪型）、买入持有。拆分 2005-2017 训练阈值 / 2018+ 样本外。"""
import json
import numpy as np
import pandas as pd

d = json.load(open("/opt/quantweave/data/market_wave.json"))
rows = d["daily"]
df = pd.DataFrame({"date": [r["date"] for r in rows],
                   "close": [float(r["close"]) for r in rows],
                   "wave_phase": [1 if (r.get("p_impulse") is not None and r["p_impulse"] >= 0.5) else 0 for r in rows]})
df = df.sort_values("date").reset_index(drop=True)
ret = df["close"].pct_change()
vol20 = (ret.rolling(20).std() * np.sqrt(244)) * 100

SPLIT = "20180101"
train = df.date < SPLIT
# 阈值只用训练段分位数（因果：样本外不偷看）
vol_hi = float(vol20[train].median())
print(f"训练段(2005-2017) vol20 中位数阈值: {round(vol_hi,2)}%")

fwd1 = (df["close"].shift(-1) / df["close"] - 1).fillna(0)
test = df.date >= SPLIT
sub = df[test].reset_index(drop=True)
v = vol20[test].fillna(vol_hi).to_numpy()
f = fwd1[test].to_numpy()
w = df.wave_phase[test].to_numpy()

def run(gate, name):
    r = np.nan_to_num(f) * gate
    cum = np.prod(1 + r) - 1
    eq = np.cumprod(1 + r)
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    return {"累计%": round(cum * 100, 1), "MDD%": round(mdd * 100, 1),
            "敞口%": round(float(np.mean(gate)) * 100, 1),
            "翻转": int(np.sum(np.diff(gate) != 0))}

vol_gate = (v > vol_hi).astype(float)
dir_gate = w.astype(float)
mix_gate = ((v > vol_hi) & (w == 1)).astype(float)

print("\n=== 2018-2026 样本外（指数层） ===")
print("买入持有      ", run(np.ones_like(f), None))
print("方向门控(浪型)", run(dir_gate, dir_gate))
print("波动门控(M1)  ", run(vol_gate, vol_gate))
print("波动∩方向     ", run(mix_gate, mix_gate))

# 条件收益检验（样本外，因果：vol20 在 t 收盘已知，看 fwd1/5/20）
print("\n=== 波动状态的条件前瞻收益（样本外） ===")
for k in (1, 5, 20):
    fwdk = (df["close"].shift(-k) / df["close"] - 1).to_numpy()
    m = (df.date >= SPLIT) & ~np.isnan(fwdk)
    hi = fwdk[m][vol20[m].fillna(0) > vol_hi]
    lo = fwdk[m][vol20[m].fillna(0) <= vol_hi]
    print(f"fwd{k}: 高波动期均值 {round(np.nanmean(hi)*100,3)}% (n={len(hi)}) vs 低波动期 {round(np.nanmean(lo)*100,3)}% (n={len(lo)})")
