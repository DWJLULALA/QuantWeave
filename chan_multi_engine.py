#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-timeframe Chan regime engine and walk-forward 3-class validation.

Combos
------
* ``5F-30F-D`` : 5-minute HS300 top-weight basket -> 30-minute resample -> HS300 daily
* ``30F-D-W``  : 2000-stock equal-weight market 30-minute -> HS300 daily -> weekly
* ``D-W-M``    : HS300 daily -> weekly -> monthly

For each daily close ``t`` the three levels are read at their latest completed
bar.  The target is the forward HS300 return over ``H`` trading days:
``up`` above +theta, ``down`` below -theta, otherwise ``consolidation``.

Validation is expanding-window walk-forward.  A model tested in year Y is
trained only on rows whose forward label is already complete before Y starts,
so no future label or future bar can leak into a prediction.  The headline
metric is balanced accuracy (macro recall over the three classes).
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import chan_core

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "chan_multi"
BAR_ROOT = DATA_ROOT / "bars"
OUT_ROOT = DATA_ROOT / "combos"
LEVEL_ROOT = DATA_ROOT / "levels"

COMBOS = {
    "5F-30F-D": ["5F", "30F", "D"],
    "30F-D-W": ["30F", "D", "W"],
    "D-W-M": ["D", "W", "M"],
}
WEIGHTS = [0.25, 0.35, 0.40]  # lower, middle, higher
LABELS = [-1, 0, 1]

STATE_COLS = [
    "phase", "trend_score", "pivot_zg", "pivot_zd", "pivot_pos",
    "pivot_width_pct", "pivot_slope", "pivot_zd_slope", "pivot_width_rank",
    "stroke_accel", "dist_zg_pct", "dist_zd_pct", "pivot_age",
    "pivot_strokes", "pivot_count", "last_dir", "last_stroke_ret",
    "stroke_count", "div", "third_buy", "third_sell", "phase_run",
    "bars_since_fractal",
]
MODEL_STATE_COLS = [
    "phase", "trend_score", "pivot_pos", "pivot_width_pct", "pivot_slope",
    "pivot_zd_slope", "pivot_width_rank", "stroke_accel", "dist_zg_pct",
    "dist_zd_pct", "pivot_age", "pivot_strokes", "last_dir",
    "last_stroke_ret", "div", "third_buy", "third_sell", "phase_run",
    "bars_since_fractal", "has_pivot", "breakout_strength",
]
BREADTH_COLS = ["breadth_up", "breadth_5", "breadth_ma20", "dispersion",
                "breadth_up_slope", "breadth_ma20_slope"]
PRICE_COLS = [
    "ret_1", "ret_5", "ret_20", "ret_60", "vol_20", "vol_60",
    "ma5_dist", "ma20_dist", "ma60_dist", "rsi14", "macd_hist_pct",
    "atr14_pct", "range_pos_20", "volume_ratio_20",
] + BREADTH_COLS
HORIZONS = [1, 3, 5, 10, 20]
THETAS = [0.005, 0.010, 0.015, 0.020, 0.030]
PRIMARY_H = 5
PRIMARY_THETA = 0.015


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


# --------------------------------------------------------------------- inputs
def _read_bars(name: str, source: str) -> pd.DataFrame:
    path = BAR_ROOT / ("%s.parquet" % name)
    if not path.exists():
        raise FileNotFoundError("missing bars: %s (run the market data builder first)" % path)
    frame = pd.read_parquet(path)
    frame["dt"] = pd.to_datetime(frame["dt"])
    frame = frame.sort_values("dt").drop_duplicates("dt", keep="last").reset_index(drop=True)
    if "volume" not in frame.columns and "vol" in frame.columns:
        frame["volume"] = frame["vol"]
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    log("loaded %s (%s) rows=%d range=%s..%s" % (
        name, source, len(frame), frame["dt"].iloc[0], frame["dt"].iloc[-1]))
    return frame


def _bar_end(bars: pd.DataFrame, level: str) -> pd.DataFrame:
    """Normalise every level to a bar-end timestamp at 15:00 for daily data."""
    out = bars.copy()
    out["dt"] = pd.to_datetime(out["dt"])
    if level in {"D", "W", "M"}:
        out["dt"] = out["dt"].dt.normalize() + pd.Timedelta(hours=15)
    return out.sort_values("dt").drop_duplicates("dt", keep="last").reset_index(drop=True)


ENGINE_VERSION = "chan_core_v3"


def _fingerprint(bars: pd.DataFrame) -> str:
    return "%s_%d_%s_%s_%.4f" % (
        ENGINE_VERSION, len(bars), bars["dt"].iloc[0].strftime("%Y%m%d%H%M"),
        bars["dt"].iloc[-1].strftime("%Y%m%d%H%M"), float(bars["close"].iloc[-1]))


def run_level(name: str, bars: pd.DataFrame, use_cache: bool = True) -> Tuple[pd.DataFrame, Dict]:
    """Run the causal engine for one level, with a source-fingerprint cache."""
    LEVEL_ROOT.mkdir(parents=True, exist_ok=True)
    fp = _fingerprint(bars)
    state_path = LEVEL_ROOT / ("%s.parquet" % name)
    meta_path = LEVEL_ROOT / ("%s.meta.json" % name)
    struct_path = LEVEL_ROOT / ("%s.structures.json" % name)
    if use_cache and state_path.exists() and meta_path.exists() and struct_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("fingerprint") == fp:
                states = pd.read_parquet(state_path)
                states["dt"] = pd.to_datetime(states["dt"])
                log("cache %s rows=%d" % (name, len(states)))
                return states, json.loads(struct_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    started = time.time()
    engine = chan_core.ChanEngine(bars)
    states = engine.run()
    structures = engine.structures()
    states.to_parquet(state_path, index=False)
    meta = {"level": name, "fingerprint": fp, "rows": int(len(states)),
            "start": str(states["dt"].iloc[0]), "end": str(states["dt"].iloc[-1]),
            "strokes": len(engine.strokes), "pivots": len(engine.pivots) + int(engine.active_pivot is not None),
            "elapsed_seconds": round(time.time() - started, 2)}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    struct_path.write_text(json.dumps(structures, ensure_ascii=False), encoding="utf-8")
    log("engine %s rows=%d strokes=%d pivots=%d %.1fs" % (
        name, len(states), meta["strokes"], meta["pivots"], meta["elapsed_seconds"]))
    return states, structures


def build_levels(combo: str) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Dict], Dict[str, str]]:
    levels = COMBOS[combo]
    sources: Dict[str, str] = {}
    frames: Dict[str, pd.DataFrame] = {}
    structures: Dict[str, Dict] = {}
    five = None
    if "5F" in levels:
        five = _read_bars("market_5f", "hs300_top_weight_basket_5min")
        frames["5F"] = _bar_end(five, "5F")
        sources["5F"] = "HS300 前 30 大权重股篮子 · 5 分钟合成（Baostock 前复权）"
    if "30F" in levels:
        if combo == "5F-30F-D":
            if five is None:
                five = _read_bars("market_5f", "hs300_top_weight_basket_5min")
            frames["30F"] = _bar_end(chan_core.resample_intraday(five, "30min"), "30F")
            sources["30F"] = "同一 5 分钟篮子重采样 30 分钟"
        else:
            frames["30F"] = _bar_end(_read_bars("market_30f", "equal_weight_market_30min"), "30F")
            sources["30F"] = "2000+ 只 A 股等权合成 30 分钟市场序列"
    if "D" in levels:
        daily = _read_bars("000300_SH_daily", "hs300_index_daily")
        frames["D"] = _bar_end(daily, "D")
        sources["D"] = "沪深300 指数日线"
    if "W" in levels:
        if "D" not in frames:
            frames["D"] = _bar_end(_read_bars("000300_SH_daily", "hs300_index_daily"), "D")
            sources["D"] = "沪深300 指数日线"
        frames["W"] = _bar_end(chan_core.resample_period(
            frames["D"].assign(dt=frames["D"]["dt"].dt.normalize()), "W"), "W")
        sources["W"] = "沪深300 日线按自然周合成"
    if "M" in levels:
        if "D" not in frames:
            frames["D"] = _bar_end(_read_bars("000300_SH_daily", "hs300_index_daily"), "D")
            sources["D"] = "沪深300 指数日线"
        frames["M"] = _bar_end(chan_core.resample_period(
            frames["D"].assign(dt=frames["D"]["dt"].dt.normalize()), "M"), "M")
        sources["M"] = "沪深300 日线按自然月合成"

    states = {}
    for level in levels:
        cache_name = level
        if level == "30F":
            cache_name = "30F_basket" if combo == "5F-30F-D" else "30F_market"
        states[level], structures[level] = run_level(cache_name, frames[level])
    return states, structures, sources


# ------------------------------------------------------------------- features
def _asof(left: pd.DataFrame, right: pd.DataFrame, prefix: str,
          tolerance: pd.Timedelta, same_day: bool) -> pd.DataFrame:
    r = right[["dt"] + STATE_COLS].copy()
    r = r.rename(columns={c: prefix + c for c in STATE_COLS})
    r = r.rename(columns={"dt": prefix + "bar_dt"})
    r = r.sort_values(prefix + "bar_dt")
    merged = pd.merge_asof(
        left.sort_values("dt_decision"),
        r, left_on="dt_decision", right_on=prefix + "bar_dt",
        direction="backward", allow_exact_matches=True, tolerance=tolerance)
    if same_day:
        stale = merged[prefix + "bar_dt"].dt.normalize() != merged["dt_decision"].dt.normalize()
        cols = [c for c in merged.columns if c.startswith(prefix) and c != prefix + "bar_dt"]
        merged.loc[stale, cols] = np.nan
    p = prefix
    merged[p + "has_pivot"] = (merged[p + "pivot_count"].fillna(0) > 0).astype(int)
    merged[p + "pivot_pos"] = merged[p + "pivot_pos"].fillna(0.0)
    merged[p + "pivot_width_pct"] = merged[p + "pivot_width_pct"].fillna(0.0)
    merged[p + "dist_zg_pct"] = merged[p + "dist_zg_pct"].fillna(0.0)
    merged[p + "dist_zd_pct"] = merged[p + "dist_zd_pct"].fillna(0.0)
    merged[p + "breakout_strength"] = np.where(
        merged[p + "phase"] > 0, merged[p + "dist_zg_pct"],
        np.where(merged[p + "phase"] < 0, -merged[p + "dist_zd_pct"], 0.0))
    return merged


def build_features(combo: str) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame], Dict[str, Dict], Dict[str, str]]:
    states, structures, sources = build_levels(combo)
    daily = _read_bars("000300_SH_daily", "hs300_index_daily")
    daily = daily.sort_values("dt").reset_index(drop=True)
    frame = daily[["dt", "open", "high", "low", "close", "volume"]].copy()
    frame["date"] = frame["dt"].dt.normalize()
    frame["dt_decision"] = frame["date"] + pd.Timedelta(hours=15)

    # Causal price controls (independent of the Chan structure).
    close = frame["close"].astype(float)
    frame["ret_1"] = close.pct_change(1)
    frame["ret_5"] = close.pct_change(5)
    frame["ret_20"] = close.pct_change(20)
    frame["ret_60"] = close.pct_change(60)
    frame["vol_20"] = close.pct_change().rolling(20).std()
    frame["vol_60"] = close.pct_change().rolling(60).std()
    for window in (5, 20, 60):
        frame["ma%d_dist" % window] = close / close.rolling(window).mean() - 1.0
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    frame["rsi14"] = (100.0 - 100.0 / (1.0 + gain / loss.replace(0, np.nan))).fillna(50.0)
    dif, dea, hist = chan_core.macd(close)
    frame["macd_hist_pct"] = hist / close
    prev_close = close.shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    frame["atr14_pct"] = true_range.ewm(alpha=1 / 14, adjust=False).mean() / close
    low20 = frame["low"].rolling(20).min()
    high20 = frame["high"].rolling(20).max()
    frame["range_pos_20"] = ((close - low20) / (high20 - low20).replace(0, np.nan)).fillna(0.5)
    frame["volume_ratio_20"] = (frame["volume"] / frame["volume"].rolling(20).mean() - 1.0).fillna(0.0)

    # Market-wide breadth from the long 30-minute panel.  This is an
    # auxiliary regime feature, not a Chan level; it is available for every
    # combo and is aligned to the daily close without future data.
    breadth_path = BAR_ROOT / "market_30f.parquet"
    if breadth_path.exists():
        breadth = pd.read_parquet(breadth_path)
        if "breadth_up" in breadth.columns:
            breadth["dt"] = pd.to_datetime(breadth["dt"])
            breadth = breadth.sort_values("dt").drop_duplicates("dt", keep="last")
            keep_b = [c for c in ["dt", "breadth_up", "breadth_5", "breadth_ma20", "dispersion"] if c in breadth.columns]
            b = breadth[keep_b].rename(columns={"dt": "breadth_dt"})
            merged = pd.merge_asof(
                frame.sort_values("dt_decision"), b,
                left_on="dt_decision", right_on="breadth_dt",
                direction="backward", allow_exact_matches=True,
                tolerance=pd.Timedelta(days=1))
            stale = merged["breadth_dt"].dt.normalize() != merged["dt_decision"].dt.normalize()
            for col in ["breadth_up", "breadth_5", "breadth_ma20", "dispersion"]:
                if col not in merged.columns:
                    merged[col] = np.nan
                merged.loc[stale, col] = np.nan
            frame = merged.drop(columns=["breadth_dt"])
    for col, default in (("breadth_up", 0.5), ("breadth_5", 0.5),
                         ("breadth_ma20", 0.5), ("dispersion", 0.0)):
        if col not in frame.columns:
            frame[col] = default
        frame[col] = frame[col].fillna(default)
    frame["breadth_up_slope"] = frame["breadth_up"] - frame["breadth_up"].shift(5)
    frame["breadth_ma20_slope"] = frame["breadth_ma20"] - frame["breadth_ma20"].shift(5)
    for col in ("breadth_up_slope", "breadth_ma20_slope"):
        frame[col] = frame[col].fillna(0.0)

    levels = COMBOS[combo]
    prefixes = {"5F": "l5f_", "30F": "l30f_", "D": "ld_", "W": "lw_", "M": "lm_"}
    tolerances = {"5F": pd.Timedelta(days=1), "30F": pd.Timedelta(days=1),
                  "D": pd.Timedelta(days=1), "W": pd.Timedelta(days=14),
                  "M": pd.Timedelta(days=45)}
    same_day = {"5F": True, "30F": True, "D": False, "W": False, "M": False}
    for level in levels:
        frame = _asof(frame, states[level], prefixes[level],
                      tolerances[level], same_day[level])

    low_p, mid_p, high_p = [prefixes[l] for l in levels]
    # Keep only dates where every level already has a completed bar.  Without
    # this, the 5F combo would train on 2005-2019 rows whose intraday levels
    # are entirely missing, diluting the signal with imputed constants.
    available = frame[[prefixes[l] + "phase" for l in levels]].notna().all(axis=1)
    frame = frame.loc[available].reset_index(drop=True)
    frame["align_score"] = sum(w * frame[p + "trend_score"] for w, p in zip(WEIGHTS, [low_p, mid_p, high_p]))
    frame["align_phase"] = sum(w * frame[p + "phase"] for w, p in zip(WEIGHTS, [low_p, mid_p, high_p]))
    frame["same_up"] = sum((frame[p + "phase"] == 1).astype(int) for p in [low_p, mid_p, high_p])
    frame["same_down"] = sum((frame[p + "phase"] == -1).astype(int) for p in [low_p, mid_p, high_p])
    frame["same_flat"] = sum((frame[p + "phase"] == 0).astype(int) for p in [low_p, mid_p, high_p])
    frame["consensus"] = frame[["same_up", "same_down", "same_flat"]].max(axis=1)
    frame["all_up"] = (frame["same_up"] == 3).astype(int)
    frame["all_down"] = (frame["same_down"] == 3).astype(int)
    frame["higher_up"] = (frame[high_p + "phase"] == 1).astype(int)
    frame["higher_down"] = (frame[high_p + "phase"] == -1).astype(int)
    frame["middle_up"] = (frame[mid_p + "phase"] == 1).astype(int)
    frame["middle_down"] = (frame[mid_p + "phase"] == -1).astype(int)
    frame["lower_up"] = (frame[low_p + "phase"] == 1).astype(int)
    frame["lower_down"] = (frame[low_p + "phase"] == -1).astype(int)
    frame["div_sum"] = sum(frame[p + "div"] for p in [low_p, mid_p, high_p])
    frame["div_higher"] = frame[high_p + "div"]
    frame["phase_gap"] = frame[high_p + "phase"] - frame[low_p + "phase"]
    frame["score_gap"] = frame[high_p + "trend_score"] - frame[low_p + "trend_score"]
    frame["breakout_higher"] = frame[high_p + "breakout_strength"]
    frame["rule_pred"] = _rule_predict(frame, low_p, mid_p, high_p, strict=True)
    frame["rule2_pred"] = _rule_predict(frame, low_p, mid_p, high_p, strict=False)
    frame["combo"] = combo
    return frame, states, structures, sources


def _rule_predict(frame: pd.DataFrame, low_p: str, mid_p: str, high_p: str,
                  strict: bool) -> pd.Series:
    hp = frame[high_p + "phase"]
    mp = frame[mid_p + "phase"]
    lp = frame[low_p + "phase"]
    hs = frame[high_p + "trend_score"]
    ls = frame[low_p + "trend_score"]
    align = frame["align_score"]
    if strict:
        out = np.zeros(len(frame), dtype=int)
        out[(hp == 1) & (mp == 1) & (lp >= 0)] = 1
        out[(hp == -1) & (mp == -1) & (lp <= 0)] = -1
        out[(out == 0) & (hp == 1) & (mp >= 0) & (ls > -0.2) & (align > 0.2)] = 1
        out[(out == 0) & (hp == -1) & (mp <= 0) & (ls < 0.2) & (align < -0.2)] = -1
        out[(out == 0) & (frame["align_phase"] >= 0.75)] = 1
        out[(out == 0) & (frame["align_phase"] <= -0.75)] = -1
        return pd.Series(out, index=frame.index)
    out = np.zeros(len(frame), dtype=int)
    out[(align > 0.35) & (hp >= 0)] = 1
    out[(align < -0.35) & (hp <= 0)] = -1
    return pd.Series(out, index=frame.index)


def model_columns(combo: str, kind: str) -> List[str]:
    levels = COMBOS[combo]
    prefixes = {"5F": "l5f_", "30F": "l30f_", "D": "ld_", "W": "lw_", "M": "lm_"}
    chan = []
    for level in levels:
        p = prefixes[level]
        chan.extend([p + c for c in MODEL_STATE_COLS])
    chan.extend(["align_score", "align_phase", "same_up", "same_down", "same_flat",
                 "consensus", "all_up", "all_down", "higher_up", "higher_down",
                 "middle_up", "middle_down", "lower_up", "lower_down", "div_sum",
                 "div_higher", "phase_gap", "score_gap", "breakout_higher"])
    if kind == "chan":
        return chan
    if kind == "price":
        return list(PRICE_COLS)
    return chan + list(PRICE_COLS)


# --------------------------------------------------------------------- labels
def add_labels(frame: pd.DataFrame, horizon: int, theta: float,
               mode: str = "path", pullback: Optional[float] = None) -> pd.DataFrame:
    """Forward stage label.

    ``path`` (primary): a sustained up/down move must both clear ``theta`` on
    the endpoint return and keep its adverse excursion within ``pullback``.
    A choppy move that ends above theta is therefore labelled consolidation,
    which matches the Chan distinction between a trend and a central pivot.
    ``endpoint`` is the legacy label kept for comparison.
    """
    out = frame.copy()
    close = out["close"].astype(float)
    out["fwd_ret"] = close.shift(-horizon) / close - 1.0
    out["label_end"] = out["dt"].shift(-horizon)
    future = pd.concat([close.shift(-k) for k in range(1, horizon + 1)], axis=1)
    if mode == "path":
        out["fwd_max"] = future.max(axis=1) / close - 1.0
        out["fwd_min"] = future.min(axis=1) / close - 1.0
        pull = pullback if pullback is not None else max(1.67 * theta, 0.02)
        up = (out["fwd_ret"] >= theta) & (out["fwd_min"] >= -pull)
        down = (out["fwd_ret"] <= -theta) & (out["fwd_max"] <= pull)
        out["label"] = np.where(up, 1, np.where(down, -1, 0))
    elif mode == "vol_scaled":
        fwd_std = future.std(axis=1) / close
        strength = out["fwd_ret"] / (fwd_std * math.sqrt(horizon)).replace(0, np.nan)
        up = (out["fwd_ret"] >= theta) & (strength >= 1.0)
        down = (out["fwd_ret"] <= -theta) & (strength <= -1.0)
        out["label"] = np.where(up, 1, np.where(down, -1, 0))
    else:
        out["label"] = np.where(out["fwd_ret"] > theta, 1,
                                np.where(out["fwd_ret"] < -theta, -1, 0))
    out.loc[out["fwd_ret"].isna(), "label"] = np.nan
    return out


# ------------------------------------------------------------------ validation
def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, cols: List[str]):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    x_train = train[cols].to_numpy(float)
    y_train = train["label"].to_numpy(int)
    if len(np.unique(y_train)) < 3:
        return None, None
    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(C=0.3, class_weight="balanced", max_iter=2000, solver="lbfgs"),
    )
    model.fit(x_train, y_train)
    x_test = test[cols].to_numpy(float)
    pred = model.predict(x_test)
    proba = model.predict_proba(x_test)
    classes = list(model.named_steps["logisticregression"].classes_)
    return pred, {"classes": classes, "proba": proba}


def walk_forward(frame: pd.DataFrame, cols: List[str], start_year: int,
                 min_train: int = 400) -> Tuple[pd.Series, pd.DataFrame, List[int]]:
    pred = pd.Series(np.nan, index=frame.index, dtype=float)
    proba = pd.DataFrame(np.nan, index=frame.index, columns=["down", "flat", "up"])
    years = sorted(frame["dt"].dt.year.dropna().unique())
    tested = []
    for year in years:
        if year < start_year:
            continue
        year_start = pd.Timestamp(year=int(year), month=1, day=1)
        train = frame[(frame["label_end"] < year_start) & frame["label"].notna()]
        test = frame[(frame["dt"] >= year_start) &
                     (frame["dt"] < year_start + pd.DateOffset(years=1)) &
                     frame["label"].notna()]
        if len(train) < min_train or len(test) == 0:
            continue
        pred_test, info = _fit_predict(train, test, cols)
        if pred_test is None:
            continue
        pred.loc[test.index] = pred_test
        classes = info["classes"]
        for i, cls in enumerate(classes):
            key = {-1: "down", 0: "flat", 1: "up"}.get(int(cls))
            if key:
                proba.loc[test.index, key] = info["proba"][:, i]
        tested.append(int(year))
    return pred, proba, tested


def _hier_models():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def make_model():
        return make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(C=0.3, class_weight="balanced", max_iter=2000, solver="lbfgs"))
    return make_model


def _hier_predict(stage1, stage2, x_test, cut):
    p_trend = stage1.predict_proba(x_test)[:, 1]
    if stage2 is None:
        p_up_given = np.full(len(x_test), 0.5)
    else:
        p_up_given = stage2.predict_proba(x_test)[:, 1]
    p_up = p_trend * p_up_given
    p_down = p_trend * (1.0 - p_up_given)
    p_flat = 1.0 - p_trend
    pred = np.where(p_trend < cut, 0, np.where(p_up_given >= 0.5, 1, -1))
    return pred, np.column_stack([p_down, p_flat, p_up])


def _fit_predict_hier(train: pd.DataFrame, test: pd.DataFrame, cols: List[str]):
    """Two-stage Chan model: trend vs consolidation, then direction.

    The trend/flat cut is selected on the last 20% of the training window only.
    That tail is earlier than the test year, so the calibration cannot see the
    test labels.  Without it the stage-1 probability tends to over-predict
    consolidation and the three-class recall collapses.
    """
    make_model = _hier_models()
    x_train = train[cols].to_numpy(float)
    x_test = test[cols].to_numpy(float)
    trend = (train["label"].to_numpy(int) != 0).astype(int)
    if len(np.unique(trend)) < 2:
        return None, None
    cut = 0.45
    split = int(len(train) * 0.8)
    if split >= 100 and len(train) - split >= 80:
        fit = train.iloc[:split]
        calib = train.iloc[split:]
        if len(np.unique(calib["label"].to_numpy(int))) >= 2:
            stage1_c = make_model().fit(fit[cols].to_numpy(float),
                                        (fit["label"].to_numpy(int) != 0).astype(int))
            trend_fit = fit.loc[fit["label"] != 0]
            stage2_c = None
            if len(trend_fit) >= 50 and trend_fit["label"].nunique() >= 2:
                stage2_c = make_model().fit(
                    trend_fit[cols].to_numpy(float),
                    (trend_fit["label"].to_numpy(int) == 1).astype(int))
            x_cal = calib[cols].to_numpy(float)
            best = None
            for candidate in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55):
                pred_c, _ = _hier_predict(stage1_c, stage2_c, x_cal, candidate)
                score = balanced_accuracy(calib["label"].to_numpy(int), pred_c)
                if best is None or score > best[0]:
                    best = (score, candidate)
            if best is not None:
                cut = best[1]

    stage1 = make_model().fit(x_train, trend)
    trend_train = train.loc[train["label"] != 0]
    up = (trend_train["label"].to_numpy(int) == 1).astype(int)
    stage2 = None
    if len(np.unique(up)) >= 2 and len(trend_train) >= 50:
        stage2 = make_model().fit(trend_train[cols].to_numpy(float), up)
    pred, proba = _hier_predict(stage1, stage2, x_test, cut)
    return pred, {"classes": [-1, 0, 1], "proba": proba, "trend_cut": cut}


def walk_forward_hier(frame: pd.DataFrame, cols: List[str], start_year: int,
                      min_train: int = 400) -> Tuple[pd.Series, pd.DataFrame, List[int]]:
    pred = pd.Series(np.nan, index=frame.index, dtype=float)
    proba = pd.DataFrame(np.nan, index=frame.index, columns=["down", "flat", "up"])
    tested = []
    for year in sorted(frame["dt"].dt.year.dropna().unique()):
        if year < start_year:
            continue
        year_start = pd.Timestamp(year=int(year), month=1, day=1)
        train = frame[(frame["label_end"] < year_start) & frame["label"].notna()]
        test = frame[(frame["dt"] >= year_start) &
                     (frame["dt"] < year_start + pd.DateOffset(years=1)) &
                     frame["label"].notna()]
        if len(train) < min_train or len(test) == 0:
            continue
        pred_test, info = _fit_predict_hier(train, test, cols)
        if pred_test is None:
            continue
        pred.loc[test.index] = pred_test
        for i, key in enumerate(["down", "flat", "up"]):
            proba.loc[test.index, key] = info["proba"][:, i]
        tested.append(int(year))
    return pred, proba, tested


def _fit_predict_hgb(train: pd.DataFrame, test: pd.DataFrame, cols: List[str]):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.utils.class_weight import compute_sample_weight

    y_train = train["label"].to_numpy(int)
    if len(np.unique(y_train)) < 3:
        return None, None
    imputer = SimpleImputer(strategy="median").fit(train[cols])
    x_train = imputer.transform(train[cols])
    x_test = imputer.transform(test[cols])
    weights = compute_sample_weight("balanced", y_train)
    model = HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.05, max_leaf_nodes=15,
        l2_regularization=1.0, min_samples_leaf=20, random_state=42)
    model.fit(x_train, y_train, sample_weight=weights)
    pred = model.predict(x_test)
    proba = model.predict_proba(x_test)
    return pred, {"classes": list(model.classes_), "proba": proba}


def walk_forward_hgb(frame: pd.DataFrame, cols: List[str], start_year: int,
                     min_train: int = 400) -> Tuple[pd.Series, pd.DataFrame, List[int]]:
    pred = pd.Series(np.nan, index=frame.index, dtype=float)
    proba = pd.DataFrame(np.nan, index=frame.index, columns=["down", "flat", "up"])
    tested = []
    for year in sorted(frame["dt"].dt.year.dropna().unique()):
        if year < start_year:
            continue
        year_start = pd.Timestamp(year=int(year), month=1, day=1)
        train = frame[(frame["label_end"] < year_start) & frame["label"].notna()]
        test = frame[(frame["dt"] >= year_start) &
                     (frame["dt"] < year_start + pd.DateOffset(years=1)) &
                     frame["label"].notna()]
        if len(train) < min_train or len(test) == 0:
            continue
        pred_test, info = _fit_predict_hgb(train, test, cols)
        if pred_test is None:
            continue
        pred.loc[test.index] = pred_test
        for i, cls in enumerate(info["classes"]):
            key = {-1: "down", 0: "flat", 1: "up"}.get(int(cls))
            if key:
                proba.loc[test.index, key] = info["proba"][:, i]
        tested.append(int(year))
    return pred, proba, tested


def balanced_accuracy(y: np.ndarray, p: np.ndarray) -> float:
    recalls = []
    for cls in LABELS:
        mask = y == cls
        if mask.any():
            recalls.append(float((p[mask] == cls).mean()))
    return float(np.mean(recalls)) if recalls else float("nan")


def metrics(y: np.ndarray, p: np.ndarray) -> Dict:
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=int)
    valid = np.isfinite(y) & np.isfinite(p)
    y, p = y[valid], p[valid]
    out = {"n": int(len(y)), "class_counts": {str(c): int((y == c).sum()) for c in LABELS},
           "pred_counts": {str(c): int((p == c).sum()) for c in LABELS}}
    recalls = {}
    for cls in LABELS:
        mask = y == cls
        recalls[str(cls)] = float((p[mask] == cls).mean()) if mask.any() else None
    out["recall"] = recalls
    out["balanced_accuracy"] = float(np.mean([r for r in recalls.values() if r is not None]))
    out["accuracy"] = float((p == y).mean()) if len(y) else None
    out["confusion"] = [[int(((y == t) & (p == q)).sum()) for q in LABELS] for t in LABELS]
    f1s = []
    for cls in LABELS:
        tp = int(((y == cls) & (p == cls)).sum())
        fp = int(((y != cls) & (p == cls)).sum())
        fn = int(((y == cls) & (p != cls)).sum())
        if tp + fp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    out["macro_f1"] = float(np.mean(f1s)) if f1s else None
    return out


def bootstrap_ba(frame: pd.DataFrame, pred_a: pd.Series, pred_b: Optional[pd.Series] = None,
                 reps: int = 500, seed: int = 42) -> Dict:
    y = frame["label"].to_numpy(int)
    a = pred_a.to_numpy(int)
    b = pred_b.to_numpy(int) if pred_b is not None else None
    blocks = frame["dt"].dt.strftime("%Y-%m").to_numpy()
    unique = np.unique(blocks)
    index_by_block = {b: np.flatnonzero(blocks == b) for b in unique}
    rng = np.random.default_rng(seed)
    vals = []
    deltas = []
    for _ in range(reps):
        pick = rng.choice(unique, size=len(unique), replace=True)
        idx = np.concatenate([index_by_block[b] for b in pick])
        if len(np.unique(y[idx])) < 2:
            continue
        ba = balanced_accuracy(y[idx], a[idx])
        vals.append(ba)
        if b is not None:
            deltas.append(ba - balanced_accuracy(y[idx], b[idx]))
    def interval(values):
        if not values:
            return {"low": None, "high": None, "mean": None}
        arr = np.asarray(values, dtype=float)
        return {"low": float(np.quantile(arr, 0.025)),
                "high": float(np.quantile(arr, 0.975)),
                "mean": float(arr.mean())}
    return {"ba": interval(vals), "delta": interval(deltas) if b is not None else None,
            "reps": len(vals)}


def yearly_metrics(frame: pd.DataFrame, pred: pd.Series) -> List[Dict]:
    rows = []
    for year in sorted(frame["dt"].dt.year.unique()):
        mask = (frame["dt"].dt.year == year) & pred.notna() & frame["label"].notna()
        if not mask.any():
            continue
        m = metrics(frame.loc[mask, "label"].to_numpy(int), pred.loc[mask].to_numpy(int))
        m["year"] = int(year)
        rows.append(m)
    return rows


def evaluate(frame: pd.DataFrame, start_year: int, include_grid: bool = True) -> Dict:
    results = {"grid": [], "models": {}, "primary": {}}
    cells = [(h, t) for h in HORIZONS for t in THETAS] if include_grid else [(PRIMARY_H, PRIMARY_THETA)]
    feature_sets = {
        "chan_logit": model_columns(frame.attrs["combo"], "chan"),
        "price_logit": model_columns(frame.attrs["combo"], "price"),
        "combined_logit": model_columns(frame.attrs["combo"], "combined"),
    }
    for horizon, theta in cells:
        labeled = add_labels(frame, horizon, theta, mode="path")
        learned = {}
        for name, cols in feature_sets.items():
            pred, proba, years = walk_forward(labeled, cols, start_year)
            learned[name] = (pred, proba, years)
        hier_pred, hier_proba, hier_years = walk_forward_hier(
            labeled, feature_sets["combined_logit"], start_year)
        learned["hier_logit"] = (hier_pred, hier_proba, hier_years)
        is_primary = horizon == PRIMARY_H and abs(theta - PRIMARY_THETA) < 1e-12
        hgb_pred = hgb_proba = None
        hgb_years = []
        if is_primary:
            hgb_pred, hgb_proba, hgb_years = walk_forward_hgb(
                labeled, feature_sets["combined_logit"], start_year)
            learned["hgb_logit"] = (hgb_pred, hgb_proba, hgb_years)
        combined_pred, combined_proba, tested_years = learned["combined_logit"]
        if tested_years:
            # One shared evaluation set: every learned model and baseline must
            # be scored on exactly the same rows.
            test_mask = (combined_pred.notna() & labeled["label"].notna()
                         & learned["chan_logit"][0].notna()
                         & learned["price_logit"][0].notna()
                         & hier_pred.notna())
            if hgb_pred is not None:
                test_mask = test_mask & hgb_pred.notna()
            test_frame = labeled.loc[test_mask].copy()
            test_frame["pred"] = combined_pred.loc[test_mask].to_numpy(int)
            test_frame["rule"] = labeled.loc[test_mask, "rule_pred"].to_numpy(int)
            test_frame["rule2"] = labeled.loc[test_mask, "rule2_pred"].to_numpy(int)
            test_frame["momentum"] = _momentum(labeled.loc[test_mask]).to_numpy(int)
            test_frame["single_chan"] = labeled.loc[test_mask, "ld_phase"].to_numpy(int)
            test_frame["pred_hier"] = hier_pred.loc[test_mask].to_numpy(int)
            if hgb_pred is not None:
                test_frame["pred_hgb"] = hgb_pred.loc[test_mask].to_numpy(int)
            cell = {
                "horizon": horizon, "theta": theta,
                "test_start": str(test_frame["dt"].min().date()),
                "test_end": str(test_frame["dt"].max().date()),
                "years": tested_years,
                "models": {
                    "combined_logit": metrics(test_frame["label"].to_numpy(int), test_frame["pred"].to_numpy(int)),
                    "hier_logit": metrics(test_frame["label"].to_numpy(int), hier_pred.loc[test_mask].to_numpy(int)),
                    "chan_logit": metrics(test_frame["label"].to_numpy(int), learned["chan_logit"][0].loc[test_mask].to_numpy(int)),
                    "price_logit": metrics(test_frame["label"].to_numpy(int), learned["price_logit"][0].loc[test_mask].to_numpy(int)),
                    "rule": metrics(test_frame["label"].to_numpy(int), test_frame["rule"].to_numpy(int)),
                    "rule2": metrics(test_frame["label"].to_numpy(int), test_frame["rule2"].to_numpy(int)),
                    "momentum": metrics(test_frame["label"].to_numpy(int), test_frame["momentum"].to_numpy(int)),
                    "single_chan": metrics(test_frame["label"].to_numpy(int), test_frame["single_chan"].to_numpy(int)),
                },
            }
            if hgb_pred is not None:
                cell["models"]["hgb_logit"] = metrics(
                    test_frame["label"].to_numpy(int), test_frame["pred_hgb"].to_numpy(int))
                cell["hgb_years"] = hgb_years
            results["grid"].append(cell)
            if is_primary:
                cell["label_mode"] = "path"
                main_pred = test_frame["pred_hgb"] if hgb_pred is not None else test_frame["pred_hier"]
                cell["bootstrap"] = bootstrap_ba(
                    test_frame, main_pred,
                    test_frame["rule"] if test_frame["rule"].notna().all() else None)
                cell["yearly"] = yearly_metrics(test_frame, main_pred)
                cell["yearly_rule"] = yearly_metrics(test_frame, test_frame["rule"])
                cell["test_dates"] = test_frame["dt"].dt.strftime("%Y-%m-%d").tolist()
                # Endpoint-label comparison on the exact same dates.
                endpoint = add_labels(frame, horizon, theta, mode="endpoint")
                ep_pred, ep_proba, _ = walk_forward(
                    endpoint, feature_sets["combined_logit"], start_year)
                ep_mask = ep_pred.notna() & endpoint["label"].notna() & test_mask
                if ep_mask.any():
                    cell["endpoint_comparison"] = {
                        "metrics": metrics(endpoint.loc[ep_mask, "label"].to_numpy(int),
                                           ep_pred.loc[ep_mask].to_numpy(int)),
                        "n": int(ep_mask.sum()),
                    }
                results["primary"] = cell
                results["primary_frame"] = test_frame
        log("cell H=%d theta=%.3f tested_years=%s" % (horizon, theta, tested_years))
    if not results["primary"] and results["grid"]:
        results["primary"] = results["grid"][0]
    return results


def _momentum(frame: pd.DataFrame, deadband: float = 0.01) -> pd.Series:
    return pd.Series(np.where(frame["ret_20"] > deadband, 1,
                              np.where(frame["ret_20"] < -deadband, -1, 0)),
                     index=frame.index)


# ---------------------------------------------------------------------- output
def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, np.datetime64) and np.isnat(value):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def latest_state(frame: pd.DataFrame, combo: str, label: pd.Series,
                 label_end: pd.Series) -> Dict:
    if frame.empty:
        return {"available": False}
    row = frame.iloc[-1]
    levels = COMBOS[combo]
    prefixes = {"5F": "l5f_", "30F": "l30f_", "D": "ld_", "W": "lw_", "M": "lm_"}
    out_levels = {}
    for level in levels:
        p = prefixes[level]
        out_levels[level] = {
            "bar_dt": None if pd.isna(row[p + "bar_dt"]) else str(row[p + "bar_dt"]),
            "phase": int(row[p + "phase"]) if pd.notna(row[p + "phase"]) else None,
            "trend_score": None if pd.isna(row[p + "trend_score"]) else round(float(row[p + "trend_score"]), 4),
            "pivot_zg": None if pd.isna(row[p + "pivot_zg"]) else round(float(row[p + "pivot_zg"]), 2),
            "pivot_zd": None if pd.isna(row[p + "pivot_zd"]) else round(float(row[p + "pivot_zd"]), 2),
            "pivot_pos": None if pd.isna(row[p + "pivot_pos"]) else round(float(row[p + "pivot_pos"]), 3),
            "pivot_strokes": int(row[p + "pivot_strokes"]) if pd.notna(row[p + "pivot_strokes"]) else None,
            "last_dir": int(row[p + "last_dir"]) if pd.notna(row[p + "last_dir"]) else None,
            "div": int(row[p + "div"]) if pd.notna(row[p + "div"]) else None,
            "has_pivot": bool(row[p + "has_pivot"]),
        }
    return {
        "available": True,
        "date": str(row["date"].date()),
        "close": round(float(row["close"]), 2),
        "label": None if "label" not in row or pd.isna(row.get("label")) else int(row["label"]),
        "levels": out_levels,
        "alignment": {
            "score": round(float(row["align_score"]), 4),
            "phase": round(float(row["align_phase"]), 4),
            "consensus": int(row["consensus"]),
            "all_up": bool(row["all_up"]),
            "all_down": bool(row["all_down"]),
        },
        "rule_pred": int(row["rule_pred"]),
        "rule2_pred": int(row["rule2_pred"]),
        "price": {
            "ret_20": None if pd.isna(row["ret_20"]) else round(float(row["ret_20"]), 4),
            "vol_20": None if pd.isna(row["vol_20"]) else round(float(row["vol_20"]), 4),
            "rsi14": None if pd.isna(row["rsi14"]) else round(float(row["rsi14"]), 2),
        },
    }


def live_prediction(frame: pd.DataFrame, combo: str,
                    horizon: int = PRIMARY_H, theta: float = PRIMARY_THETA) -> Dict:
    labeled = add_labels(frame, horizon, theta, mode="path")
    latest = labeled.iloc[-1]
    train = labeled[(labeled["label_end"] <= latest["dt"]) & labeled["label"].notna()]
    cols = model_columns(combo, "combined")
    out = {"horizon": horizon, "theta": theta, "label_mode": "path",
           "fit_rows": int(len(train))}
    pred, info = _fit_predict(train, labeled.iloc[[-1]], cols)
    if pred is not None:
        classes = info["classes"]
        out["multinomial"] = {
            "pred": int(pred[0]),
            "proba": {str(int(c)): round(float(info["proba"][0, i]), 4)
                      for i, c in enumerate(classes)},
        }
    hpred, hinfo = _fit_predict_hier(train, labeled.iloc[[-1]], cols)
    if hpred is not None:
        out["hier"] = {
            "pred": int(hpred[0]),
            "proba": {"-1": round(float(hinfo["proba"][0, 0]), 4),
                      "0": round(float(hinfo["proba"][0, 1]), 4),
                      "1": round(float(hinfo["proba"][0, 2]), 4)},
            "trend_cut": hinfo.get("trend_cut"),
        }
    gpred, ginfo = _fit_predict_hgb(train, labeled.iloc[[-1]], cols)
    if gpred is not None:
        classes = list(ginfo["classes"])
        out["hgb"] = {
            "pred": int(gpred[0]),
            "proba": {str(int(c)): round(float(ginfo["proba"][0, i]), 4)
                      for i, c in enumerate(classes)},
        }
        out["pred"] = int(gpred[0])
        out["proba"] = out["hgb"]["proba"]
        out["model"] = "hgb"
        out["available"] = True
    elif hpred is not None:
        out["pred"] = int(hpred[0])
        out["proba"] = out["hier"]["proba"]
        out["model"] = "hier"
        out["available"] = True
    else:
        out["available"] = pred is not None
        out["model"] = "multinomial" if pred is not None else None
        if pred is not None:
            out["pred"] = int(pred[0])
            out["proba"] = out["multinomial"]["proba"]
    return out


def write_combo(combo: str, include_grid: bool = True, start_year: Optional[int] = None) -> Dict:
    started = time.time()
    frame, states, structures, sources = build_features(combo)
    frame.attrs["combo"] = combo
    first_year = int(frame["dt"].dt.year.min())
    if start_year is None:
        start_year = 2022 if combo == "5F-30F-D" else 2016
    result = evaluate(frame, start_year=start_year, include_grid=include_grid)
    primary = result.get("primary", {})
    test_frame = result.pop("primary_frame", None)

    out_dir = OUT_ROOT / combo
    out_dir.mkdir(parents=True, exist_ok=True)
    daily = frame.copy()
    daily["date"] = daily["dt"].dt.strftime("%Y-%m-%d")
    primary_labeled = add_labels(frame, PRIMARY_H, PRIMARY_THETA, mode="path")
    daily["label"] = primary_labeled["label"]
    daily["fwd_ret"] = primary_labeled["fwd_ret"]
    if test_frame is not None:
        daily["test_flag"] = daily.index.isin(test_frame.index).astype(int)
        for col in ("pred", "pred_hier", "rule", "rule2", "momentum", "single_chan"):
            daily[col] = np.nan
            daily.loc[test_frame.index, col] = test_frame[col].to_numpy()
        for col in ("down", "flat", "up"):
            daily["proba_" + col] = np.nan
            daily["proba_hier_" + col] = np.nan
            daily["proba_hgb_" + col] = np.nan
        pred_full, proba_full, _ = walk_forward(
            primary_labeled, model_columns(combo, "combined"), start_year)
        hpred_full, hproba_full, _ = walk_forward_hier(
            primary_labeled, model_columns(combo, "combined"), start_year)
        gpred_full, gproba_full, _ = walk_forward_hgb(
            primary_labeled, model_columns(combo, "combined"), start_year)
        for col in ("down", "flat", "up"):
            daily["proba_" + col] = proba_full[col]
            daily["proba_hier_" + col] = hproba_full[col]
            daily["proba_hgb_" + col] = gproba_full[col]
        daily["pred_model"] = pred_full
        daily["pred_hier_full"] = hpred_full
        daily["pred_hgb_full"] = gpred_full
    keep = (["date", "close", "label", "fwd_ret", "align_score", "align_phase",
             "consensus", "rule_pred", "rule2_pred", "test_flag", "pred", "pred_hier",
             "pred_hgb", "pred_model", "pred_hier_full", "pred_hgb_full",
             "proba_down", "proba_flat", "proba_up",
             "proba_hier_down", "proba_hier_flat", "proba_hier_up",
             "proba_hgb_down", "proba_hgb_flat", "proba_hgb_up",
             "rule", "momentum", "single_chan"])
    keep = [c for c in keep if c in daily.columns]
    daily[keep].to_csv(out_dir / "daily.csv", index=False, encoding="utf-8")

    latest = latest_state(frame, combo, frame.get("label"), frame.get("label_end"))
    latest["live"] = live_prediction(frame, combo)
    latest["data_sources"] = sources
    latest["coverage"] = {
        "start": str(frame["dt"].min().date()),
        "end": str(frame["dt"].max().date()),
        "rows": int(len(frame)),
        "first_year": first_year,
        "test_start_year": start_year,
    }
    if primary:
        main_name = "hgb_logit" if "hgb_logit" in primary["models"] else "hier_logit"
        main = primary["models"][main_name]
        latest["headline"] = {
            "horizon": primary["horizon"], "theta": primary["theta"],
            "label_mode": primary.get("label_mode", "path"),
            "model": "hgb" if main_name == "hgb_logit" else "hier",
            "test_start": primary["test_start"], "test_end": primary["test_end"],
            "balanced_accuracy": main["balanced_accuracy"],
            "hgb_balanced_accuracy": primary["models"].get("hgb_logit", {}).get("balanced_accuracy"),
            "hier_balanced_accuracy": primary["models"]["hier_logit"]["balanced_accuracy"],
            "combined_balanced_accuracy": primary["models"]["combined_logit"]["balanced_accuracy"],
            "rule_balanced_accuracy": primary["models"]["rule"]["balanced_accuracy"],
            "single_chan_balanced_accuracy": primary["models"]["single_chan"]["balanced_accuracy"],
            "momentum_balanced_accuracy": primary["models"]["momentum"]["balanced_accuracy"],
            "n": main["n"],
            "class_counts": main["class_counts"],
            "confusion": main["confusion"],
            "recall": main["recall"],
            "bootstrap": primary.get("bootstrap"),
            "endpoint_comparison": primary.get("endpoint_comparison"),
            "yearly": primary.get("yearly", []),
        }
    (out_dir / "latest.json").write_text(
        json.dumps(_clean(latest), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "eval.json").write_text(
        json.dumps(_clean(result), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "structures.json").write_text(
        json.dumps(_clean(structures), ensure_ascii=False), encoding="utf-8")
    summary = {
        "combo": combo, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - started, 1),
        "coverage": latest["coverage"], "headline": latest.get("headline"),
        "latest": latest.get("date"),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--combo", default="all",
                        choices=["all"] + list(COMBOS))
    parser.add_argument("--start-year", type=int, default=0)
    parser.add_argument("--quick", action="store_true", help="only the primary horizon/threshold")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    import chan_core as _core  # noqa: F401  (keeps import graph explicit)
    combos = list(COMBOS) if args.combo == "all" else [args.combo]
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    summaries = []
    for combo in combos:
        log("=== %s ===" % combo)
        try:
            summary = write_combo(combo, include_grid=not args.quick,
                                  start_year=args.start_year or None)
            summaries.append(summary)
        except FileNotFoundError as exc:
            log("skip %s: %s" % (combo, exc))
            summaries.append({"combo": combo, "error": str(exc)})
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "combos": summaries}
    (DATA_ROOT / "summary.json").write_text(
        json.dumps(_clean(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(_clean(payload), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
