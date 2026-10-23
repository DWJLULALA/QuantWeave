#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收盘价分型/笔/中枢的简化阶段引擎，并非完整 OHLC 缠论实现。

结构：3 根收盘价确认分型 → 间距 >=4 的交替笔 → 连续 3 笔重叠中枢。
同类分型可更新当前末笔，但逐日输出的 phase/div 一经产生便冻结，未来不得改写。
背驰只标记候选事件，不实现二/三类买卖点。信号在当日收盘后可知，交易必须滞后。
固定规则不等于样本外验证；ZigZag 为事后延迟评估基准，不能充当实时交易信号。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
INDEX = Path("/opt/kronos/factor_horizon/shadow_live/data/index_000300_SH.parquet")


def macd(close: pd.Series):
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def fractals(close: np.ndarray):
    """返回 (i, kind)，+1 顶/-1 底；i 处在第 i+1 根收盘后才可用。"""
    out = []
    for i in range(1, len(close) - 1):
        if close[i] > close[i - 1] and close[i] > close[i + 1]:
            out.append((i, 1))
        elif close[i] < close[i - 1] and close[i] < close[i + 1]:
            out.append((i, -1))
    return out


def _accept_fractal(pts, i, kind, close, min_gap=4):
    """Update the observed structure, reporting whether its last endpoint changed."""
    if not pts:
        pts.append([i, kind])
        return True
    last_i, last_kind = pts[-1]
    if kind == last_kind:
        if kind * close[i] > kind * close[last_i]:
            pts[-1] = [i, kind]
            return True
    elif i - last_i >= min_gap:
        pts.append([i, kind])
        return True
    return False


def strokes(fract, close: np.ndarray, min_gap=4):
    """Return the latest observed leg structure, not an immutable historical signal log."""
    pts = []
    for i, kind in fract:
        _accept_fractal(pts, i, kind, close, min_gap)
    legs = []
    for (i1, k1), (i2, _) in zip(pts[:-1], pts[1:]):
        legs.append({"i1": i1, "p1": close[i1], "i2": i2, "p2": close[i2],
                     "dir": 1 if k1 == -1 else -1, "confirm": i2 + 1})
    return pts, legs


def _latest_pivot(legs):
    """Original three-leg overlap rule; extending it does not change its bounds."""
    for k in range(len(legs) - 1, 1, -1):
        three = legs[k - 2:k + 1]
        zg = min(max(leg["p1"], leg["p2"]) for leg in three)
        zd = max(min(leg["p1"], leg["p2"]) for leg in three)
        if zg > zd:
            return zd, zg
    return None


def chan_phases(close: np.ndarray, dif: np.ndarray):
    """Return as-of-close phase/div signals and the latest observed leg structure.

    Consume one confirmed fractal at a time. A later, more extreme same-kind
    fractal replaces only the current last leg. Previous phase/div outputs remain
    frozen. Filtering final-history legs by confirm is NOT causal: future
    replacements can erase legs that were observable in the past.
    """
    close, dif = np.asarray(close, dtype=float), np.asarray(dif, dtype=float)
    if close.ndim != 1 or dif.shape != close.shape:
        raise ValueError("close and dif must be one-dimensional arrays of equal length")
    if not np.isfinite(close).all() or (close <= 0).any():
        raise ValueError("close must contain finite positive prices")
    phase = np.full(len(close), np.nan)
    diverg = np.full(len(close), np.nan)
    pts, legs, pivot = [], [], None
    for day in range(2, len(close)):
        i = day - 1  # i 的分型首次可知日为 day；绝不读取 day 之后的价格。
        kind = (1 if close[i] > close[i - 1] and close[i] > close[day]
                else -1 if close[i] < close[i - 1] and close[i] < close[day] else 0)
        if kind and _accept_fractal(pts, i, kind, close) and len(pts) >= 2:
            (i1, k1), (i2, _) = pts[-2:]
            direction = 1 if k1 == -1 else -1
            seg = dif[i1:i2 + 1]
            finite_seg = seg[np.isfinite(seg)]
            dif_ext = (float(finite_seg.max() if direction == 1 else finite_seg.min())
                       if len(finite_seg) else np.nan)
            leg = {"i1": i1, "p1": close[i1], "i2": i2, "p2": close[i2],
                   "dir": direction, "confirm": day, "dif_ext": dif_ext}
            if len(legs) < len(pts) - 1:
                legs.append(leg)
            else:
                legs[-1] = leg
            pivot = _latest_pivot(legs)
            if len(legs) >= 3:
                prev = legs[-3]  # 交替笔：上一条同向笔。
                if direction == -1 and leg["p2"] < prev["p2"] and dif_ext > prev["dif_ext"]:
                    diverg[day] = 1
                elif direction == 1 and leg["p2"] > prev["p2"] and dif_ext < prev["dif_ext"]:
                    diverg[day] = -1
        if pivot is not None:
            zd, zg = pivot
            phase[day] = 0 if zd <= close[day] <= zg else (1 if close[day] > zg else -1)
        elif legs:
            phase[day] = legs[-1]["dir"]
    return phase, diverg, legs, {"b1_div": int(np.sum(diverg == 1)),
                               "s1_div": int(np.sum(diverg == -1))}


def zigzag_pivots(close, threshold):
    """Retrospective pivots, with explicit direction AFTER the pivot and confirmation day."""
    close = np.asarray(close, dtype=float)
    threshold = np.broadcast_to(np.asarray(threshold, dtype=float), close.shape)
    if not len(close):
        return []
    direction, ext_px, ext_i = 1, close[0], 0
    pivots = []
    for i in range(1, len(close)):
        px = close[i]
        if direction == 1:
            if px >= ext_px:
                ext_px, ext_i = px, i
            elif (ext_px - px) / ext_px >= threshold[i]:
                pivots.append({"index": ext_i, "direction_after": -1, "confirm": i})
                direction, ext_px, ext_i = -1, px, i
        else:
            if px <= ext_px:
                ext_px, ext_i = px, i
            elif (px - ext_px) / ext_px >= threshold[i]:
                pivots.append({"index": ext_i, "direction_after": 1, "confirm": i})
                direction, ext_px, ext_i = 1, px, i
    return pivots


def pivot_delay_metrics(phase, pivots, min_history=200, max_lag=40, binary_phase=False):
    """First recognition of the new direction, within [0, max_lag).

    The denominator is exactly the eligible pivot set with a complete observation
    window. This is a hindsight diagnostic, not a claim of real-time pivot detection.
    For legacy binary wave phases, zero means non-up (not specifically down).
    """
    phase = np.asarray(phase, dtype=float)
    eligible = [p for p in pivots
                if p["index"] >= min_history and p["index"] + max_lag <= len(phase)]
    lags = []
    for pivot in eligible:
        want = pivot["direction_after"]
        if binary_phase:
            want = 1 if want == 1 else 0
        t = pivot["index"]
        hits = np.flatnonzero(phase[t:t + max_lag] == want)
        if len(hits):
            lags.append(int(hits[0]))
    return {"中位": float(np.median(lags)) if lags else None,
            "均值": round(float(np.mean(lags)), 1) if lags else None,
            "捕获率%": round(len(lags) / len(eligible) * 100, 1) if eligible else None,
            "可评估拐点": len(eligible), "已捕获": len(lags), "窗口天数": max_lag}


def _read_index(path):
    path = Path(path)
    idx = pd.read_csv(path, dtype={"trade_date": str, "date": str}) if path.suffix.lower() == ".csv" else pd.read_parquet(path)
    if "trade_date" not in idx.columns and "date" in idx.columns:
        idx = idx.rename(columns={"date": "trade_date"})
    idx["trade_date"] = pd.to_datetime(idx["trade_date"].astype(str)).dt.strftime("%Y%m%d")
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    if idx.empty or idx["trade_date"].duplicated().any():
        raise ValueError("index must contain nonempty, unique trading dates")
    idx["close"] = pd.to_numeric(idx["close"], errors="raise")
    return idx


def main(index_path=INDEX, output_path=HERE / "data" / "chan_phase.csv",
         wave_path=HERE / "data" / "market_wave.json", metrics_output=None):
    idx = _read_index(index_path)
    close = idx["close"].to_numpy(float)
    dates = idx["trade_date"].tolist()
    dif, _, _ = macd(idx["close"])
    phase, diverg, legs, cnt = chan_phases(close, dif.to_numpy())
    df = pd.DataFrame({"date": dates, "close": close, "phase": phase, "div": diverg})
    out = {"method": "close_fractal_online_v2", "legs": len(legs), "signals": cnt,
           "signal_timing": "phase/div are known after date close; use on a later execution date"}
    for k in (1, 5, 20):
        fwd = idx["close"].shift(-k) / idx["close"] - 1
        row = {}
        for name, mask in (("上涨", df.phase == 1), ("震荡", df.phase == 0), ("下跌", df.phase == -1)):
            v = fwd[mask & fwd.notna()]
            row[name] = {"日均%": round(float(v.mean() * 100), 3) if len(v) else None,
                         "n": int(len(v)), "胜率%": round(float((v > 0).mean() * 100), 1) if len(v) else None}
        div_v = fwd[df["div"] == 1].dropna()
        row["底背驰后"] = {"日均%": round(float(div_v.mean() * 100), 3) if len(div_v) else None,
                         "n": int(len(div_v))}
        out[f"fwd{k}"] = row

    # Close-only index diagnostic: decide after t close, execute at t+1 close,
    # then earn t+1 close -> t+2 close. Portfolio backtests use their own fills.
    split = "20180101"
    fwd1 = (idx["close"].shift(-1) / idx["close"] - 1).fillna(0).to_numpy()
    test = np.array([d >= split for d in dates])

    def bt(signal, name):
        gate = np.r_[0.0, signal[:-1]]
        gate[-1] = 0.0  # Final close liquidation; no unobserved future return.
        r = fwd1[test] * gate[test]
        eq = np.r_[1.0, np.cumprod(1 + r)]
        out.setdefault("门控_2018+", {})[name] = {
            "累计%": round(float(eq[-1] - 1) * 100, 1),
            "MDD%": round(float((eq / np.maximum.accumulate(eq) - 1).min()) * 100, 1),
            "敞口%": round(float(gate[test].mean()) * 100, 1) if test.any() else None,
            "翻转": int(np.sum(np.diff(gate[test]) != 0))}

    bt(np.ones(len(dates)), "买入持有")
    bt((phase == 1).astype(float), "仅上涨段")
    bt((phase >= 0).astype(float), "上涨+震荡")
    bt((phase != -1).astype(float), "非下跌段")
    vol20 = (idx["close"].pct_change().rolling(20).std() * np.sqrt(244) * 100).to_numpy()
    tr_mask = np.array([d < split for d in dates])
    train_vol = vol20[tr_mask & np.isfinite(vol20)]
    vol_hi = float(np.median(train_vol)) if len(train_vol) else np.nan
    bt(((phase == 1) & (vol20 > vol_hi)).astype(float), "波动∩缠论上涨")
    out["vol阈值"] = round(vol_hi, 2) if np.isfinite(vol_hi) else None
    out["指数诊断执行口径"] = "收盘后信号，下一交易日收盘执行；无成本，仅供诊断"

    sigma = idx["close"].pct_change().rolling(20).std()
    threshold = np.maximum(0.04, (2.5 * sigma).fillna(0.04).to_numpy())
    pivots = zigzag_pivots(close, threshold)
    out["拐点延迟_天"] = {"缠论": pivot_delay_metrics(phase, pivots)}
    if wave_path is not None and Path(wave_path).is_file():
        wave = json.loads(Path(wave_path).read_text())
        wmap = {str(r["date"]): (1 if r["p_impulse"] >= 0.5 else 0) for r in wave["daily"]}
        wave_phase = np.array([wmap.get(d, np.nan) for d in dates])
        out["拐点延迟_天"]["浪型规则"] = pivot_delay_metrics(wave_phase, pivots, binary_phase=True)
    out["延迟说明"] = "事后拐点后的新方向首次识别；分母为拥有完整40日窗口且历史>=200日的拐点；浪型0仅代表非上涨"
    out["最新状态"] = {"date": dates[-1], "phase": {1: "上涨", 0: "震荡", -1: "下跌"}.get(int(phase[-1]))
                     if not math.isnan(phase[-1]) else None}
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    payload = json.dumps(out, ensure_ascii=False, indent=1, allow_nan=False)
    if metrics_output is not None:
        metrics_output = Path(metrics_output)
        metrics_output.parent.mkdir(parents=True, exist_ok=True)
        metrics_output.write_text(payload + "\n")
    print(payload)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=INDEX, help="CSV or parquet index prices")
    parser.add_argument("--output", type=Path, default=HERE / "data" / "chan_phase.csv")
    parser.add_argument("--wave", type=Path, default=HERE / "data" / "market_wave.json")
    parser.add_argument("--no-wave", action="store_true")
    parser.add_argument("--metrics-output", type=Path)
    args = parser.parse_args()
    main(args.index, args.output, None if args.no_wave else args.wave, args.metrics_output)
