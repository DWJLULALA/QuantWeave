#!/usr/bin/env python3
"""大盘浪型阶段因子：艾略特"推动/调整"二相概率（日频，带延续性）。

语义对应艾略特最通用的阶段划分——推动浪(1-2-3-4-5)与调整浪(A-B-C)：以自适应
ZigZag 把指数历史切成上升腿(≈推动段)与下降腿(≈调整段)作为浪型标签，用宏观因子卡
验证过的市场状态量（M1 已实现波动 / M5 量比 / M8 动量态）+ 指数趋势结构（均线位置、
均线斜率、20 日动量、波动比）做逻辑回归，输出每日 P(推动浪)，再经 EMA 平滑保证
概率有延续性、不高频跳变。

这是描述/择时层因子，与 SET10/Kronos 横截面因子正交；只读行情，不提交订单。
输出: <ui>/data/market_wave.json  {summary, daily:[{date,close,p_raw,p_impulse,phase,...}]}
"""
from __future__ import annotations
import argparse, json, math, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(os.getenv("STOCKLAB_PROJECT_ROOT", "/opt/kronos"))
HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "market_wave.json"
INDEX = ROOT / "factor_horizon" / "shadow_live" / "data" / "index_000300_SH.parquet"
PANEL_DIR = ROOT / "factor_horizon" / "shadow_live" / "data" / "panels"
WARMUP = 130            # 特征/标签预热天数
ZIGZAG_BASE = 0.04      # 锯齿最小反转幅度 4%
VOL_MULT = 2.5          # 反转阈值 = max(4%, 2.5×σ20)（自适应波动）
ALPHA = float(os.getenv("MARKET_WAVE_ALPHA", "0.90"))  # 概率 EMA 平滑（越大越平滑）


def zigzag_labels(close: np.ndarray, thr: np.ndarray) -> np.ndarray:
    """上升腿=1(推动)，下降腿=0(调整)；反转幅度达 thr 即确认新腿。"""
    n = len(close)
    label = np.full(n, np.nan)
    if n < 3:
        return label
    direction = 1
    pivot = 0
    ext_px, ext_i = close[0], 0
    for i in range(1, n):
        px = close[i]
        if direction >= 0:
            if px > ext_px:
                ext_px, ext_i = px, i
            elif (ext_px - px) / ext_px >= thr[i]:
                label[pivot:ext_i + 1] = 1
                direction, pivot, ext_px, ext_i = -1, ext_i, px, i
        else:
            if px < ext_px:
                ext_px, ext_i = px, i
            elif (px - ext_px) / ext_px >= thr[i]:
                label[pivot:ext_i + 1] = 0
                direction, pivot, ext_px, ext_i = 1, ext_i, px, i
    label[pivot:] = 1 if direction >= 0 else 0
    return label


def load_amount_ratio() -> pd.Series | None:
    """M5 量比：全市场日成交额 / 其 20 日均值（最新累计面板聚合）。"""
    try:
        paths = sorted(PANEL_DIR.glob("astock_daily_ohlcva_*.parquet"))
        if not paths:
            return None
        frame = pd.read_parquet(paths[-1], columns=["timestamps", "amount"])
        frame["timestamps"] = pd.to_datetime(frame["timestamps"], errors="coerce")
        frame = frame.dropna()
        daily = frame.groupby(frame["timestamps"].dt.strftime("%Y%m%d"))["amount"].sum().sort_index()
        ratio = daily / daily.rolling(20).mean()
        return ratio.rename("f_amtratio")
    except Exception as exc:
        print(f"[market_wave] M5 量比跳过: {exc}", flush=True)
        return None


def build() -> None:
    idx = pd.read_parquet(INDEX)
    idx["trade_date"] = idx["trade_date"].astype(str).str.replace("-", "", regex=False)
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    close = idx["close"].astype(float)
    dates = idx["trade_date"].tolist()
    ret = close.pct_change()
    sigma20 = ret.rolling(20).std()
    thr = np.maximum(ZIGZAG_BASE, (VOL_MULT * sigma20).fillna(ZIGZAG_BASE).to_numpy())

    feats = pd.DataFrame(index=idx.index)
    ma20, ma60, ma120 = close.rolling(20).mean(), close.rolling(60).mean(), close.rolling(120).mean()
    feats["f_trendpos"] = close / ma120 - 1.0          # 价格相对半年线
    feats["f_maslope"] = ma20 / ma20.shift(10) - 1.0   # 短均线斜率
    feats["f_mom20"] = close.pct_change(20)            # 20 日动量（M8 的中期版）
    feats["f_vol20"] = (sigma20 * math.sqrt(244))      # M1 已实现波动（年化）
    feats["f_volratio"] = sigma20.rolling(20).mean() / sigma20.rolling(60).mean()  # 波动聚集方向
    amt = load_amount_ratio()
    if amt is not None:
        feats["f_amtratio"] = amt.reindex(dates).to_numpy()  # M5 量比（天量预警）

    label = zigzag_labels(close.to_numpy(), thr)
    valid = feats.notna().all(axis=1).to_numpy() & ~np.isnan(label)
    valid[:WARMUP] = False

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    X = feats.to_numpy(float)
    mu, sd = X[valid].mean(axis=0), X[valid].std(axis=0) + 1e-12
    Z = (X - mu) / sd
    model = LogisticRegression(C=1.0, max_iter=2000).fit(Z[valid], label[valid].astype(int))
    p_raw = np.full(len(dates), np.nan)
    p_raw[valid] = model.predict_proba(Z[valid])[:, 1]
    auc = float(roc_auc_score(label[valid], p_raw[valid]))

    p_imp = np.full(len(dates), np.nan)
    prev = None
    for i in range(len(dates)):
        if not np.isfinite(p_raw[i]):
            continue
        prev = p_raw[i] if prev is None else ALPHA * prev + (1 - ALPHA) * p_raw[i]
        p_imp[i] = prev
    phase = np.where(p_imp >= 0.5, 1, 0)

    # 连续性体检：近 250 日翻转次数 / 平均阶段长度
    recent = phase[~np.isnan(p_imp)][-250:]
    flips = int(np.sum(np.diff(recent) != 0)) if len(recent) > 1 else 0
    seg_lens, cur = [], 1
    for a, b in zip(phase[:-1], phase[1:]):
        if a == b:
            cur += 1
        else:
            seg_lens.append(cur); cur = 1
    seg_lens.append(cur)
    days_in_phase = seg_lens[-1]
    cur_phase = int(phase[-1])

    coeff = {name: round(float(w), 4) for name, w in zip(feats.columns, model.coef_[0])}
    daily = []
    for i in range(len(dates)):
        if not np.isfinite(p_imp[i]):
            continue
        daily.append({"date": dates[i], "close": round(float(close.iloc[i]), 2),
                      "p_raw": round(float(p_raw[i]), 4) if np.isfinite(p_raw[i]) else None,
                      "p_impulse": round(float(p_imp[i]), 4),
                      "phase": int(phase[i]) if np.isfinite(p_imp[i]) else None,
                      "label": int(label[i]) if np.isfinite(label[i]) else None})
    payload = {
        "available": bool(daily), "kind": "market_wave",
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "date": daily[-1]["date"] if daily else None,
        "p_impulse": daily[-1]["p_impulse"] if daily else None,
        "phase": "推动浪(1-3-5)" if cur_phase == 1 else "调整浪(A-C)",
        "phase_code": cur_phase, "days_in_phase": days_in_phase,
        "auc_insample": round(auc, 4),
        "flips_250d": flips, "avg_phase_days": round(float(np.mean(seg_lens)), 1),
        "alpha": ALPHA, "zigzag_base": ZIGZAG_BASE,
        "features": coeff,
        "feat_today": {c: (round(float(X[-1][j]), 4) if np.isfinite(X[-1][j]) else None) for j, c in enumerate(feats.columns)},
        "method": "ZigZag(自适应阈值)浪段标签 + 宏观因子卡状态量(M1波动/M5量比/动量/趋势结构) 逻辑回归 + EMA 平滑",
        "read_only": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"summary": payload, "daily": daily}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    build()
