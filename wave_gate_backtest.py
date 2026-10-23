#!/usr/bin/env python3
"""浪型门控参数网格回测：EMA α ∈ [0.60, 0.90]，周期 = 2018至今(样本外) 与 2026年7月以来。

指数层（沪深300 日收益）+ 策略层（2026 组合书盯市收益）。成本假设：每次跨越（进或出）
0.1%（佣金+印花+冲击的保守近似），翻转 n 次扣 2n 次成本。所有敞口用前一日(前一行)信息。
"""
from __future__ import annotations
import glob, json, math, os, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ALPHAS = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
COST = 0.001  # 每次跨越（单向）成本


def ema(x, alpha):
    out = np.full(len(x), np.nan)
    prev = None
    for i, v in enumerate(x):
        v = 0.0 if v is None or math.isnan(v) else v
        prev = v if prev is None else alpha * prev + (1 - alpha) * v
        out[i] = prev
    return out


def stats(ret, gate=None):
    gross = float(np.prod(1 + ret) - 1)
    eq = np.cumprod(1 + ret)
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    out = {"累计%": round(gross * 100, 2), "净累计%(扣成本)": round(gross * 100, 2), "最大回撤%": round(mdd * 100, 1)}
    if gate is not None:
        flips = int(np.sum(np.diff(gate) != 0))
        net = (1 + gross) * (1 - COST) ** (2 * flips) - 1
        out["净累计%(扣成本)"] = round(net * 100, 2)
        out["敞口%"] = round(float(np.mean(gate)) * 100, 1)
        out["翻转次数"] = flips
    return out


def variants(score_prev: np.ndarray, ret: np.ndarray) -> dict:
    res = {"V0_不门控": stats(ret)}
    g1 = (score_prev >= 0).astype(float)
    res["V1_无平滑二值"] = stats(ret * g1, g1)
    for a in ALPHAS:
        es = ema(list(score_prev), a)
        gb = (es >= 0).astype(float)
        res[f"EMA{a}_二值"] = stats(ret * gb, gb)
        gc = np.clip(es * 1.5, 0, 1)
        res[f"EMA{a}_置信度"] = stats(ret * gc, (gc > 0).astype(float))
    return res


def main() -> None:
    wave = json.loads((HERE / "data" / "market_wave.json").read_text(encoding="utf-8"))
    wmap = {str(r["date"]): float(r["score"]) for r in wave["daily"]}

    import market_wave_factor as mwf
    idx = pd.read_parquet(mwf.INDEX)
    idx["trade_date"] = idx["trade_date"].astype(str).str.replace("-", "", regex=False)
    idx = idx.sort_values("trade_date").reset_index(drop=True)

    out = {}
    for name, start in (("指数层_2018至今", "20180101"), ("指数层_2026年7月以来", "20260701")):
        seg = idx[idx["trade_date"] >= start].reset_index(drop=True)
        fwd1 = (seg["close"].shift(-1) / seg["close"] - 1).fillna(0).to_numpy()
        sc = np.array([wmap.get(d, np.nan) for d in seg["trade_date"]])
        sc_prev = pd.Series(sc).shift(1).fillna(0.0).to_numpy()
        out[name] = {"窗口": f"{seg['trade_date'].iloc[0]}→{seg['trade_date'].iloc[-1]}（{len(seg)}日）", **variants(sc_prev, fwd1)}

    snaps = sorted(glob.glob(str(HERE / "runs" / "factor_*/factor_daily.csv")), key=os.path.getmtime)
    src = None
    for c in reversed(snaps):
        snap = Path(c).parent / "factor_snapshot.json"
        if snap.exists() and json.loads(snap.read_text()).get("kronos_source") == "daily":
            src = c
            break
    d = pd.read_csv(src)
    order = d["signal_date"].astype(str).tolist()
    sc = np.array([wmap.get(dt, np.nan) for dt in order])
    sc_prev = pd.Series(sc).shift(1).fillna(0.0).to_numpy()
    port = d["port_ret"].fillna(0).to_numpy()
    bench = d["bench_ret"].fillna(0).to_numpy()
    out["策略层_组合书"] = {"窗口": f"{order[0]}→{order[-1]}（{len(order)} 信号日）",
                        "基准_不门控": stats(port), "基准_沪深300": stats(bench), **variants(sc_prev, port)}

    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
