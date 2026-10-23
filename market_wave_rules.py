#!/usr/bin/env python3
"""大盘浪型阶段因子 v2：纯规则引擎（替换 v1 逻辑回归）。

七条确定性规则等权投票，每条输出 +1(推动)/-1(调整)/0(中性)：
  R1 道氏结构   最近两个摆动高点 HH 且两个低点 HL → 推动；LH+LL → 调整
  R2 Supertrend ATR(10,3.0) 翻转带方向
  R3 ADX(14)    >25 趋势期 +1；<20 震荡期 -1
  R4 Choppiness <38.2 趋势 +1；>61.8 震荡 -1
  R5 均线族     MA20>MA60>MA120 多头排列 +1；空头排列 -1
  R6 动量(M8)   20 日收益 >+2% +1；<-2% -1
  R7 浪型计数   最近 5 条 ZigZag 腿满足艾略特三铁律(W2 不破 W1 起点/W3 非最短/
                W4 不重叠 W1 区域)+斐波那契指引 → 有效推动计数 +1

得分 score = 票均值 ∈ [-1,1]；P(推动浪)=(score+1)/2。
相位翻转带滞回：|score| 越过 0.2 才允许翻转，且每段最短持有 5 天（防高频跳变）。
无任何拟合参数（ADX/CHOP/反转带均为业界标准常数），全历史可直接当样本外读。
输出与 v1 同 schema: data/market_wave.json
"""
from __future__ import annotations
import argparse, json, math, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(os.getenv("STOCKLAB_PROJECT_ROOT", "/opt/kronos"))
HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "market_wave.json"
INDEX = ROOT / "factor_horizon" / "shadow_live" / "data" / "index_000300_SH.parquet"
HYSTERESIS = 0.2
MIN_HOLD = 5


def wilder_adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    up, dn = high.diff(), -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=close.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=close.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def choppiness(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_sum = tr.rolling(n).sum()
    rng = high.rolling(n).max() - low.rolling(n).min()
    return 100 * np.log10(atr_sum / rng.replace(0, np.nan)) / np.log10(n)


def supertrend(close: pd.Series, n: int = 10, mult: float = 3.0) -> pd.Series:
    """收盘价近似带的标准 Supertrend（final band 递推 + 方向翻转）。"""
    atr = close.diff().abs().rolling(n).mean().bfill()
    upper, lower = close + mult * atr, close - mult * atr
    fu = upper.copy(); fl = lower.copy()
    for i in range(1, len(close)):
        fu.iloc[i] = min(upper.iloc[i], fu.iloc[i - 1]) if close.iloc[i - 1] <= fu.iloc[i - 1] else upper.iloc[i]
        fl.iloc[i] = max(lower.iloc[i], fl.iloc[i - 1]) if close.iloc[i - 1] >= fl.iloc[i - 1] else lower.iloc[i]
    st = pd.Series(index=close.index, dtype=float)
    dirn = 1
    for i in range(len(close)):
        if close.iloc[i] > fu.iloc[i - 1] if i else False:
            dirn = 1
        elif i and close.iloc[i] < fl.iloc[i - 1]:
            dirn = -1
        st.iloc[i] = fl.iloc[i] if dirn == 1 else fu.iloc[i]
    return st


SSE_CACHE = HERE / "data" / "index_000001_SH.parquet"


def load_sse_index(max_age_days: int = 7):
    """上证指数日收盘（腾讯接口分页抓取，parquet 缓存；失败返回 None 不阻塞主流程）。"""
    try:
        f = pd.read_parquet(SSE_CACHE) if SSE_CACHE.exists() else None
        if f is not None and len(f):
            last = pd.Timestamp(str(f["trade_date"].iloc[-1]))
            if (pd.Timestamp.now() - last).days <= max_age_days:
                return f
        import requests
    except Exception:
        return f if SSE_CACHE.exists() and f is not None and len(f) else None
    rows, end = [], ""
    try:
        for _ in range(30):
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,{end},320,qfq"
            data = (requests.get(url, timeout=20).json().get("data") or {}).get("sh000001") or {}
            klines = data.get("qfqday") or data.get("day") or []
            if not klines:
                break
            rows = klines + rows
            first = str(klines[0][0]).replace("-", "")
            if first <= "20050101":
                break
            end = (pd.Timestamp(first) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        pass
    if not rows:
        return f if f is not None and len(f) else None
    f = pd.DataFrame({"trade_date": [str(r[0]).replace("-", "") for r in rows],
                      "close": [float(r[2]) for r in rows]})
    f = f.drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        f.to_parquet(SSE_CACHE)
    except Exception:
        pass
    return f


def pos120(close: pd.Series) -> pd.Series:
    """价格在近 120 日高低区间的位置（0-100，Stochastic 式同尺度化）。"""
    hh, ll = close.rolling(120).max(), close.rolling(120).min()
    rng = (hh - ll).replace(0, np.nan)
    return ((close - ll) / rng * 100).clip(0, 100)


def zigzag_pivots(close: np.ndarray, thr: np.ndarray):
    """返回拐点索引列表 [(idx, px)]，交替峰谷。"""
    pivots = [(0, close[0])]
    direction = 1
    ext_px, ext_i = close[0], 0
    for i in range(1, len(close)):
        px = close[i]
        if direction >= 0:
            if px >= ext_px:
                ext_px, ext_i = px, i
            elif (ext_px - px) / ext_px >= thr[i]:
                pivots.append((ext_i, ext_px))
                direction, ext_px, ext_i = -1, px, i
        else:
            if px <= ext_px:
                ext_px, ext_i = px, i
            elif (px - ext_px) / ext_px >= thr[i]:
                pivots.append((ext_i, ext_px))
                direction, ext_px, ext_i = 1, px, i
    if pivots[-1][0] != ext_i:
        pivots.append((ext_i, ext_px))
    return pivots


def zigzag_labels(pivots, n):
    """事后复盘真值：拐点之间的腿方向（上升腿=1 推动段，下降腿=0 调整段）。"""
    labels = np.full(n, np.nan)
    for (i1, p1), (i2, p2) in zip(pivots[:-1], pivots[1:]):
        labels[i1:i2 + 1] = 1 if p2 >= p1 else 0
    # 尾部（最新一段进行中的腿）沿用最后一腿方向
    if len(pivots) >= 2:
        last_dir = 1 if pivots[-1][1] >= pivots[-2][1] else 0
        for i in range(pivots[-1][0], n):
            if np.isnan(labels[i]):
                labels[i] = last_dir
    return labels


def dow_vote(pivots, lookback=4):
    """最近若干拐点：两个高点 HH 且两个低点 HL → +1；LH+LL → -1；否则 0。"""
    pts = pivots[-lookback:]
    if len(pts) < 5:
        return 0
    highs, lows = [], []
    for i in range(1, len(pts) - 1):
        if pts[i][1] > pts[i - 1][1] and pts[i][1] > pts[i + 1][1]:
            highs.append(pts[i])
        if pts[i][1] < pts[i - 1][1] and pts[i][1] < pts[i + 1][1]:
            lows.append(pts[i])
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            return 1
        if lh and ll:
            return -1
    return 0


def elliott_vote(pivots):
    """最近 5 条腿尝试 1-2-3-4-5 推动计数（看多方向）：满足三铁律 + W3 斐波那契 → +1。"""
    if len(pivots) < 6:
        return 0
    pts = pivots[-6:]
    legs = [(pts[i + 1][1] - pts[i][1]) for i in range(5)]
    up_legs = [legs[0], legs[2], legs[4]]
    if any(v <= 0 for v in up_legs):
        return 0
    w1, w3, w5 = up_legs
    if legs[1] <= 0 or legs[3] <= 0:
        return 0
    w2, w4 = legs[1], legs[3]
    start1 = pts[0][1]
    ok = True
    if pts[2][1] <= start1:      # 铁律1：W2 不破 W1 起点
        ok = False
    if min(w1, w5) > w3:         # 铁律2：W3 不是最短
        ok = False
    if pts[4][1] <= pts[1][1]:   # 铁律3：W4 不重叠 W1 区域
        ok = False
    fib = 1.0 <= (w3 / w1) <= 2.618 if w1 > 0 else False
    return 1 if (ok and fib) else 0


def build() -> None:
    idx = pd.read_parquet(INDEX)
    idx["trade_date"] = idx["trade_date"].astype(str).str.replace("-", "", regex=False)
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    close = idx["close"].astype(float)
    dates = idx["trade_date"].tolist()
    ret = close.pct_change()
    sigma20 = ret.rolling(20).std()
    thr = np.maximum(0.04, (2.5 * sigma20).fillna(0.04).to_numpy())

    high = low = close  # 指数仅收盘价
    adx = wilder_adx(high, low, close)
    chop = choppiness(high, low, close)
    st = supertrend(close)
    ma20, ma60, ma120 = close.rolling(20).mean(), close.rolling(60).mean(), close.rolling(120).mean()
    mom20 = close.pct_change(20)
    ema10 = close.ewm(span=10, adjust=False).mean()
    mom5 = close.pct_change(5)

    sse = load_sse_index()
    sse_close = None
    if sse is not None and len(sse):
        smap = dict(zip(sse["trade_date"].astype(str), pd.to_numeric(sse["close"], errors="coerce")))
        sse_series = pd.Series([smap.get(d) for d in dates], dtype=float)
        if sse_series.notna().sum() > 200:
            sse_close = sse_series
    csi_pos, sse_pos = pos120(close), (pos120(sse_close) if sse_close is not None else None)
    pivots = zigzag_pivots(close.to_numpy(), thr)
    label_arr = zigzag_labels(pivots, len(dates))
    state_fast = None

    rows = []
    phase, days_in, score_prev = None, 0, 0.0
    votes_hist = []
    for i in range(len(dates)):
        if i < 130 or any(math.isnan(v) for v in (adx.iloc[i], chop.iloc[i], ma120.iloc[i], mom20.iloc[i])):
            continue
        piv_i = [p for p in pivots if p[0] <= i]
        r1 = dow_vote(piv_i)
        r2 = 1 if close.iloc[i] > st.iloc[i] else -1
        r3 = 1 if adx.iloc[i] > 25 else (-1 if adx.iloc[i] < 20 else 0)
        r4 = 1 if chop.iloc[i] < 38.2 else (-1 if chop.iloc[i] > 61.8 else 0)
        r5 = 1 if (ma20.iloc[i] > ma60.iloc[i] > ma120.iloc[i]) else (-1 if (ma20.iloc[i] < ma60.iloc[i] < ma120.iloc[i]) else 0)
        r6 = 1 if mom20.iloc[i] > 0.02 else (-1 if mom20.iloc[i] < -0.02 else 0)
        r7 = elliott_vote(piv_i)
        votes = [r1, r2, r3, r4, r5, r6, r7]
        score = float(np.mean(votes))
        fast_votes = [r2, 1 if chop.iloc[i] < 45 else -1,
                      1 if (mom5.iloc[i] if math.isfinite(mom5.iloc[i]) else 0) > 0 else -1,
                      1 if close.iloc[i] > ema10.iloc[i] else -1]
        fast_score = float(np.mean(fast_votes))
        if phase is None:
            if abs(score) >= HYSTERESIS:
                phase = 1 if score > 0 else 0
                days_in = 1
        else:
            days_in += 1
            if days_in >= MIN_HOLD:
                if phase == 1 and score <= -HYSTERESIS:
                    phase, days_in = 0, 1
                elif phase == 0 and score >= HYSTERESIS:
                    phase, days_in = 1, 1
        fast_prev = state_fast
        state_fast = fast_score if state_fast is None else 0.6 * state_fast + 0.4 * fast_score
        rows.append({"date": dates[i], "close": round(float(close.iloc[i]), 2),
                     "csi300_pos": (round(float(csi_pos.iloc[i]), 2) if np.isfinite(csi_pos.iloc[i]) else None),
                     "sse_pos": (round(float(sse_pos.iloc[i]), 2) if sse_pos is not None and np.isfinite(sse_pos.iloc[i]) else None),
                     "score": round(score, 4),
                     "p_impulse": round((score + 1) / 2, 4),
                     "p_fast": round((state_fast + 1) / 2, 4),
                     "label": (None if np.isnan(label_arr[i]) else int(label_arr[i])),
                     "phase": phase,
                     "votes": {"R1_道氏": r1, "R2_SuperTrend": r2, "R3_ADX": r3, "R4_Chop": r4,
                               "R5_均线族": r5, "R6_动量": r6, "R7_浪型计数": r7}})

    seg_lens, cur = [], 1
    for a, b in zip((r["phase"] for r in rows[:-1]), (r["phase"] for r in rows[1:])):
        if a == b:
            cur += 1
        else:
            seg_lens.append(cur); cur = 1
    seg_lens.append(cur)
    recent = [r["phase"] for r in rows][-250:]
    flips = int(np.sum(np.diff(recent) != 0)) if len(recent) > 1 else 0
    last = rows[-1]
    payload = {
        "available": bool(rows), "kind": "market_wave",
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "date": last["date"], "p_impulse": last["p_impulse"],
        "phase": "推动浪(1-3-5)" if last["phase"] == 1 else "调整浪(A-C)",
        "phase_code": last["phase"], "days_in_phase": seg_lens[-1],
        "auc_insample": None,
        "flips_250d": flips, "avg_phase_days": round(float(np.mean(seg_lens)), 1),
        "alpha": None, "zigzag_base": 0.04,
        "features": {"R1_道氏": "HH+HL=+1/LH+LL=-1", "R2_SuperTrend": "ATR(10,3)", "R3_ADX": ">25/<20",
                     "R4_Chop": "<38.2/>61.8", "R5_均线族": "20>60>120", "R6_动量": "±2%", "R7_浪型计数": "三铁律+斐波那契"},
        "feat_today": last["votes"],
        "p_fast": last.get("p_fast"),
        "corr_truth": (round(float(np.corrcoef([r["p_impulse"] for r in rows if r.get("label") is not None],
                                               [r["label"] for r in rows if r.get("label") is not None])[0, 1]), 4) if any(r.get("label") is not None for r in rows) else None),
        "truth_days": {"up": int(sum(1 for r in rows if r.get("label") == 1)), "down": int(sum(1 for r in rows if r.get("label") == 0))},        "method": "纯规则引擎v2：道氏结构/SuperTrend/ADX/Choppiness/均线族/动量/艾略特三铁律计数 七规则等权投票；滞回±0.2+最短持有5天防跳变；无拟合参数",
        "read_only": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"summary": payload, "daily": rows}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    build()
