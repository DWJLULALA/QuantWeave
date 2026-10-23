#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Causal single-timeframe Chan structure engine (OHLC bars).

The engine intentionally stops at the elements needed for regime recognition:

    inclusion-merged bars -> confirmed fractals -> strokes -> central pivots

It does **not** implement segments (线段), buy/sell points beyond a diagnostic
third-type breakout flag, or multi-level nesting.  Those are deliberately out
of scope: every reported value must be knowable at the close of the bar it is
attached to.

Causality rules
---------------
* A merged bar is only final once a later source bar no longer includes it.
* A fractal centred on merged bar ``j`` is confirmed at the close of the first
  source bar of merged bar ``j+2`` (the bar that proves ``j+1`` is closed).
* Only the final stroke endpoint may be replaced by a more extreme same-kind
  fractal.  Values already written for earlier bars are never rewritten.
* Pivots are rebuilt from the tail that follows the last *ended* pivot.  The
  active pivot is therefore provisional, while ended pivots are frozen.

The output is a per-source-bar state table.  All timestamps are naive
Asia/Shanghai exchange timestamps, matching the upstream Tushare data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

EPS = 1e-12


@dataclass
class MergedBar:
    start_idx: int
    end_idx: int
    dt_start: pd.Timestamp
    dt_end: pd.Timestamp
    high: float
    low: float
    direction: int
    closed: bool = False


@dataclass
class Fractal:
    mi: int                 # merged-bar index of the centre
    kind: int               # +1 top, -1 bottom
    price: float
    ext_idx: int            # source bar carrying the extreme
    avail_idx: int          # source bar at whose close the fractal is known
    dt: pd.Timestamp        # confirmation timestamp


@dataclass
class Stroke:
    start_mi: int
    end_mi: int
    start_price: float
    end_price: float
    direction: int
    start_idx: int
    end_idx: int
    confirm_idx: int
    start_dt: pd.Timestamp
    end_dt: pd.Timestamp
    confirm_dt: pd.Timestamp
    provisional: bool = True

    @property
    def low(self) -> float:
        return min(self.start_price, self.end_price)

    @property
    def high(self) -> float:
        return max(self.start_price, self.end_price)

    @property
    def ret(self) -> float:
        return self.end_price / max(self.start_price, EPS) - 1.0


@dataclass
class Pivot:
    start_stroke: int
    end_stroke: int
    zg: float               # upper bound (中枢上沿)
    zd: float               # lower bound (中枢下沿)
    start_idx: int
    end_idx: int
    start_dt: pd.Timestamp
    end_dt: pd.Timestamp
    stroke_count: int
    status: str = "active"  # active | ended
    leaving_stroke: Optional[int] = None

    @property
    def width(self) -> float:
        return max(self.zg - self.zd, EPS)


def _as_bars(frame: pd.DataFrame, dt_col: str = "dt") -> pd.DataFrame:
    required = {dt_col, "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("bars missing columns: %s" % sorted(missing))
    bars = frame.copy()
    bars[dt_col] = pd.to_datetime(bars[dt_col], errors="raise")
    bars = bars.sort_values(dt_col, kind="mergesort").drop_duplicates(dt_col, keep="last")
    bars = bars.reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in bars.columns:
            bars[col] = pd.to_numeric(bars[col], errors="coerce")
    if "open" not in bars.columns:
        bars["open"] = bars["close"]
    if "volume" not in bars.columns:
        bars["volume"] = 0.0
    values = bars[["open", "high", "low", "close"]].to_numpy(float)
    if not np.isfinite(values).all():
        raise ValueError("bars contain non-finite OHLC values")
    if (bars["close"] <= 0).any():
        raise ValueError("close must be positive")
    if (bars["high"] < bars[["open", "close", "low"]].max(axis=1) - EPS).any():
        raise ValueError("high is below open/close/low")
    if (bars["low"] > bars[["open", "close", "high"]].min(axis=1) + EPS).any():
        raise ValueError("low is above open/close/high")
    return bars


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Causal MACD.  EWM at t only uses prices up to t."""
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2.0
    return dif, dea, hist


class ChanEngine:
    """Online OHLC Chan structure engine for one timeframe."""

    def __init__(self, bars: pd.DataFrame, min_gap: int = 4,
                 macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9,
                 max_tail_strokes: int = 4000):
        self.bars = _as_bars(bars)
        self.min_gap = int(min_gap)
        self.max_tail_strokes = int(max_tail_strokes)
        self.dt = self.bars["dt"].to_numpy()
        self.open = self.bars["open"].to_numpy(float)
        self.high = self.bars["high"].to_numpy(float)
        self.low = self.bars["low"].to_numpy(float)
        self.close = self.bars["close"].to_numpy(float)
        self.volume = self.bars["volume"].to_numpy(float)
        self.dif, self.dea, self.hist = [x.to_numpy(float) for x in macd(
            self.bars["close"], macd_fast, macd_slow, macd_signal)]

        self.merged: List[MergedBar] = []
        self._evaluated_centres: set = set()
        self.fractals: List[Fractal] = []
        self.points: List[Dict] = []
        self.strokes: List[Stroke] = []
        self.pivots: List[Pivot] = []          # ended pivots, frozen
        self.active_pivot: Optional[Pivot] = None
        self.div_current: int = 0
        self.div_events: List[Dict] = []
        self._phase_run: int = 0
        self._last_phase: Optional[int] = None
        self._last_fractal_idx: int = -1
        self.states: List[Dict] = []

    # ------------------------------------------------------------------ bars
    def _append_or_merge(self, t: int) -> None:
        high, low, dt = float(self.high[t]), float(self.low[t]), self.dt[t]
        if not self.merged:
            self.merged.append(MergedBar(t, t, dt, dt, high, low, 1))
            return
        last = self.merged[-1]
        included = ((high <= last.high and low >= last.low) or
                    (high >= last.high and low <= last.low))
        if included:
            if last.direction >= 0:
                last.high = max(last.high, high)
                last.low = max(last.low, low)
            else:
                last.high = min(last.high, high)
                last.low = min(last.low, low)
            last.end_idx = t
            last.dt_end = dt
            return

        last.closed = True
        direction = 1 if high > last.high else -1
        self.merged.append(MergedBar(t, t, dt, dt, high, low, direction))
        # The previous merged bar just became final.  Its centre is two bars
        # back, and both of its neighbours are now closed.
        self._maybe_emit_fractal(t)

    def _source_extreme(self, mi: int, kind: int) -> Tuple[float, int]:
        bar = self.merged[mi]
        if kind > 0:
            values = self.high[bar.start_idx:bar.end_idx + 1]
            pos = int(values.argmax())
            return float(values[pos]), bar.start_idx + pos
        values = self.low[bar.start_idx:bar.end_idx + 1]
        pos = int(values.argmin())
        return float(values[pos]), bar.start_idx + pos

    def _maybe_emit_fractal(self, t: int) -> None:
        centre = len(self.merged) - 3
        if centre < 1 or centre in self._evaluated_centres:
            return
        if not (self.merged[centre - 1].closed and self.merged[centre].closed
                and self.merged[centre + 1].closed):
            return
        self._evaluated_centres.add(centre)
        c, left, right = self.merged[centre], self.merged[centre - 1], self.merged[centre + 1]
        top = (c.high > left.high + EPS and c.high > right.high + EPS
               and c.low > left.low + EPS and c.low > right.low + EPS)
        bottom = (c.low < left.low - EPS and c.low < right.low - EPS
                  and c.high < left.high - EPS and c.high < right.high - EPS)
        if not (top or bottom):
            return
        kind = 1 if top else -1
        price, ext_idx = self._source_extreme(centre, kind)
        fractal = Fractal(centre, kind, price, ext_idx, t, self.dt[t])
        self.fractals.append(fractal)
        self._last_fractal_idx = t
        self._on_fractal(fractal)

    # --------------------------------------------------------------- strokes
    def _on_fractal(self, f: Fractal) -> None:
        if not self.points:
            self.points.append(self._point(f))
            return
        last = self.points[-1]
        if f.kind == last["kind"]:
            more_extreme = (f.price > last["price"] + EPS if f.kind > 0
                            else f.price < last["price"] - EPS)
            if not more_extreme:
                return
            self.points[-1] = self._point(f)
            if self.strokes:
                self._replace_last_endpoint(f)
            self._rebuild_pivots()
            self._refresh_divergence()
            return

        if f.mi - last["mi"] < self.min_gap:
            return
        if (f.kind > 0 and f.price <= last["price"] + EPS) or (
                f.kind < 0 and f.price >= last["price"] - EPS):
            return
        self.points.append(self._point(f))
        if self.strokes:
            self.strokes[-1].provisional = False
        stroke = Stroke(
            start_mi=last["mi"], end_mi=f.mi,
            start_price=last["price"], end_price=f.price,
            direction=1 if f.kind > 0 else -1,
            start_idx=last["ext_idx"], end_idx=f.ext_idx,
            confirm_idx=f.avail_idx,
            start_dt=last["dt"], end_dt=self.dt[f.ext_idx],
            confirm_dt=f.dt,
        )
        self.strokes.append(stroke)
        self._rebuild_pivots()
        self._refresh_divergence()

    @staticmethod
    def _point(f: Fractal) -> Dict:
        return {"mi": f.mi, "kind": f.kind, "price": f.price,
                "ext_idx": f.ext_idx, "avail_idx": f.avail_idx, "dt": f.dt}

    def _replace_last_endpoint(self, f: Fractal) -> None:
        stroke = self.strokes[-1]
        stroke.end_mi = f.mi
        stroke.end_price = f.price
        stroke.end_idx = f.ext_idx
        stroke.end_dt = self.dt[f.ext_idx]
        stroke.confirm_idx = f.avail_idx
        stroke.confirm_dt = f.dt
        stroke.provisional = True

    # ---------------------------------------------------------------- pivots
    @staticmethod
    def _make_pivot(strokes: Sequence[Stroke], k: int) -> Optional[Pivot]:
        if k < 2:
            return None
        trio = strokes[k - 2:k + 1]
        zd = max(s.low for s in trio)
        zg = min(s.high for s in trio)
        if zg <= zd + EPS:
            return None
        return Pivot(
            start_stroke=k - 2, end_stroke=k, zg=float(zg), zd=float(zd),
            start_idx=trio[0].start_idx, end_idx=trio[-1].end_idx,
            start_dt=trio[0].start_dt, end_dt=trio[-1].end_dt,
            stroke_count=3, status="active",
        )

    @staticmethod
    def _overlaps(pivot: Pivot, stroke: Stroke) -> bool:
        return stroke.high > pivot.zd + EPS and stroke.low < pivot.zg - EPS

    def _rebuild_pivots(self) -> None:
        """Rebuild only the un-ended tail; ended pivots stay frozen."""
        if not self.strokes:
            return
        start = self.pivots[-1].end_stroke + 1 if self.pivots else 0
        strokes = self.strokes
        ended: List[Pivot] = []
        active: Optional[Pivot] = None
        pending_leave: Optional[int] = None

        for k in range(start, len(strokes)):
            stroke = strokes[k]
            if active is None:
                candidate = self._make_pivot(strokes, k)
                if candidate is not None:
                    active = candidate
                continue
            if self._overlaps(active, stroke):
                active.end_stroke = k
                active.end_idx = stroke.end_idx
                active.end_dt = stroke.end_dt
                active.stroke_count += 1
                active.leaving_stroke = None
                pending_leave = None
            elif pending_leave is None:
                pending_leave = k
                active.leaving_stroke = k
            else:
                active.status = "ended"
                if active.leaving_stroke is not None:
                    last_inside = active.leaving_stroke - 1
                    if last_inside >= active.start_stroke:
                        active.end_stroke = last_inside
                        active.end_idx = strokes[last_inside].end_idx
                        active.end_dt = strokes[last_inside].end_dt
                ended.append(active)
                active = None
                pending_leave = None
                candidate = self._make_pivot(strokes, k)
                if candidate is not None:
                    active = candidate

        if ended:
            self.pivots.extend(ended)
        self.active_pivot = active

    # ----------------------------------------------------------- divergence
    def _refresh_divergence(self) -> None:
        self.div_current = 0
        if len(self.strokes) < 3:
            return
        cur = self.strokes[-1]
        prev = self.strokes[-3]  # alternating strokes: same direction, one apart
        if cur.direction != prev.direction:
            return
        lo, hi = cur.start_idx, cur.end_idx
        plo, phi = prev.start_idx, prev.end_idx
        cur_dif = self.dif[lo:hi + 1]
        prev_dif = self.dif[plo:phi + 1]
        if not len(cur_dif) or not len(prev_dif):
            return
        if cur.direction > 0:
            if (cur.end_price > prev.end_price + EPS
                    and np.nanmax(cur_dif) < np.nanmax(prev_dif) - EPS):
                self.div_current = -1
        else:
            if (cur.end_price < prev.end_price - EPS
                    and np.nanmin(cur_dif) > np.nanmin(prev_dif) + EPS):
                self.div_current = 1
        if self.div_current:
            self.div_events.append({
                "idx": cur.confirm_idx, "dt": cur.confirm_dt,
                "kind": self.div_current,
                "stroke_start": cur.start_dt, "stroke_end": cur.end_dt,
            })

    # ---------------------------------------------------------------- state
    def _state(self, t: int) -> Dict:
        close = float(self.close[t])
        ap = self.active_pivot
        prev = self.pivots[-1] if self.pivots else None
        last = self.strokes[-1] if self.strokes else None
        if ap is not None:
            width = ap.width
            pos = (close - ap.zd) / width
            phase = 1 if close > ap.zg + EPS else (-1 if close < ap.zd - EPS else 0)
            pos_score = float(np.clip((pos - 0.5) * 2.0, -1.0, 1.0))
            slope = 0.0
            if prev is not None:
                slope = float(np.clip((ap.zg - prev.zg) / width, -1.0, 1.0))
            last_dir = last.direction if last else 0
            score = float(np.clip(0.45 * pos_score + 0.25 * slope + 0.30 * last_dir,
                                  -1.0, 1.0))
            zg, zd = ap.zg, ap.zd
            pivot_slope = ((ap.zg - prev.zg) / close) if prev is not None else 0.0
            pivot_zd_slope = ((ap.zd - prev.zd) / close) if prev is not None else 0.0
            widths = [p.width for p in self.pivots[-60:]] + [width]
            pivot_width_rank = float(np.mean(np.asarray(widths) <= width))
            pivot_age = t - ap.start_idx
            pivot_strokes = ap.stroke_count
            pivot_count = len(self.pivots) + 1
            dist_zg = (close - zg) / close
            dist_zd = (close - zd) / close
            width_pct = width / close
            third_buy = int(close > zg and last is not None and last.direction > 0
                            and last.low > zg)
            third_sell = int(close < zd and last is not None and last.direction < 0
                             and last.high < zd)
        else:
            zg = zd = pos = dist_zg = dist_zd = width_pct = np.nan
            pivot_slope = pivot_zd_slope = 0.0
            pivot_width_rank = 0.5
            last_dir = last.direction if last else 0
            phase = last_dir
            ret = last.ret if last else 0.0
            score = float(last_dir * min(1.0, abs(ret) / 0.02)) if last else 0.0
            pivot_age = -1
            pivot_strokes = 0
            pivot_count = len(self.pivots)
            third_buy = third_sell = 0

        stroke_accel = 0.0
        if len(self.strokes) >= 3:
            stroke_accel = float(self.strokes[-1].ret - self.strokes[-3].ret)

        if self._last_phase is None or phase != self._last_phase:
            self._phase_run = 1
            self._last_phase = phase
        else:
            self._phase_run += 1

        return {
            "dt": self.dt[t],
            "close": close,
            "phase": int(phase),
            "trend_score": score,
            "pivot_zg": float(zg) if np.isfinite(zg) else np.nan,
            "pivot_zd": float(zd) if np.isfinite(zd) else np.nan,
            "pivot_pos": float(np.clip(pos, -5.0, 5.0)) if np.isfinite(pos) else np.nan,
            "pivot_width_pct": float(width_pct) if np.isfinite(width_pct) else np.nan,
            "pivot_slope": float(pivot_slope),
            "pivot_zd_slope": float(pivot_zd_slope),
            "pivot_width_rank": float(pivot_width_rank),
            "stroke_accel": float(stroke_accel),
            "dist_zg_pct": float(dist_zg) if np.isfinite(dist_zg) else np.nan,
            "dist_zd_pct": float(dist_zd) if np.isfinite(dist_zd) else np.nan,
            "pivot_age": int(max(pivot_age, -1)),
            "pivot_strokes": int(pivot_strokes),
            "pivot_count": int(pivot_count),
            "last_dir": int(last_dir),
            "last_stroke_ret": float(last.ret) if last else 0.0,
            "stroke_count": len(self.strokes),
            "div": int(self.div_current),
            "third_buy": int(third_buy),
            "third_sell": int(third_sell),
            "phase_run": int(self._phase_run),
            "bars_since_fractal": int(t - self._last_fractal_idx) if self._last_fractal_idx >= 0 else -1,
            "merged_bars": len(self.merged),
            "fractal_count": len(self.fractals),
        }

    def run(self) -> pd.DataFrame:
        for t in range(len(self.bars)):
            self._append_or_merge(t)
            self.states.append(self._state(t))
        out = pd.DataFrame(self.states)
        out["dt"] = pd.to_datetime(out["dt"])
        return out

    # ------------------------------------------------------------ structures
    def structures(self, max_strokes: int = 240, max_pivots: int = 120,
                   max_fractals: int = 240) -> Dict[str, List[Dict]]:
        def iso(value) -> str:
            return pd.Timestamp(value).isoformat()

        strokes = []
        for s in self.strokes[-max_strokes:]:
            strokes.append({
                "start": iso(s.start_dt), "end": iso(s.end_dt),
                "start_price": round(s.start_price, 4), "end_price": round(s.end_price, 4),
                "direction": s.direction, "provisional": bool(s.provisional),
            })
        pivots = []
        for p in (self.pivots[-max_pivots:] + ([self.active_pivot] if self.active_pivot else [])):
            pivots.append({
                "start": iso(p.start_dt), "end": iso(p.end_dt),
                "zg": round(p.zg, 4), "zd": round(p.zd, 4),
                "stroke_count": p.stroke_count, "status": p.status,
            })
        fractals = [{"dt": iso(f.dt), "kind": f.kind, "price": round(f.price, 4)}
                    for f in self.fractals[-max_fractals:]]
        return {"strokes": strokes, "pivots": pivots, "fractals": fractals}


def resample_intraday(bars: pd.DataFrame, rule: str = "30min",
                      dt_col: str = "dt") -> pd.DataFrame:
    """Resample 5-minute A-share bars to 30-minute bars without lunch leakage."""
    frame = _as_bars(bars, dt_col=dt_col).copy()
    frame[dt_col] = pd.to_datetime(frame[dt_col])
    # Tushare minute bars are stamped at bar end.  Group by session so that
    # 11:30 and 13:00 never share one synthetic bar.
    minutes = frame[dt_col].dt.hour * 60 + frame[dt_col].dt.minute
    session = np.where(minutes <= 11 * 60 + 30, 0, 1)
    day = frame[dt_col].dt.strftime("%Y%m%d").to_numpy()
    step = 30 if str(rule).startswith("30") else 60
    # 09:35..11:30 and 13:05..15:00 are 24 five-minute bars each.
    first_minute = np.where(session == 0, 9 * 60 + 35, 13 * 60 + 5)
    seq = (minutes - first_minute) // 5
    bucket = (seq // (step // 5)).astype(int)
    frame["_bucket"] = [f"{d}-{s}-{b}" for d, s, b in zip(day, session, bucket)]
    out = frame.groupby("_bucket", sort=False).agg(
        dt=(dt_col, "last"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        bars=(dt_col, "size"),
    ).reset_index(drop=True)
    out["dt"] = pd.to_datetime(out["dt"])
    return out.sort_values("dt").reset_index(drop=True)


def resample_period(daily: pd.DataFrame, period: str = "W") -> pd.DataFrame:
    """Build completed weekly ('W') or monthly ('M') bars from daily bars.

    The timestamp of each synthetic bar is the last trading day in the period,
    which is exactly when the bar becomes knowable.
    """
    frame = _as_bars(daily, dt_col="dt").copy()
    frame["dt"] = pd.to_datetime(frame["dt"])
    if period.upper() not in {"W", "M"}:
        raise ValueError("period must be W or M")
    if period.upper() == "W":
        key = frame["dt"].dt.strftime("%G-W%V")
    else:
        key = frame["dt"].dt.strftime("%Y-%m")
    out = frame.groupby(key, sort=False).agg(
        dt=("dt", "last"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        bars=("dt", "size"),
    ).reset_index(drop=True)
    out["dt"] = pd.to_datetime(out["dt"])
    return out.sort_values("dt").reset_index(drop=True)
