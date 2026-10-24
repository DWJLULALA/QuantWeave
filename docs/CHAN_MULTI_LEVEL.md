# Multi-level Chan phase engine

Timeframe combinations: `5F–30F–D`, `30F–D–W`, `D–W–M`.

Introduced October 18, 2026, this engine extends the earlier single-level, close-only `chan_phase_engine.py` to **OHLC, three timeframes and per-bar causal structures**. Evaluation measures balanced accuracy for advancing / declining / consolidating labels. Available out-of-sample history differs by combination.

## 1. Modules and entry points

| File | Responsibility |
|---|---|
| `chan_core.py` | Containment → fractals → strokes → zones → divergence → per-bar states |
| `chan_multi_engine.py` | Combine timeframes, create labels, run walk-forward evaluation and write artifacts |
| `build_market_30f.py` | Build an equal-weight 30-minute market series and breadth features from 2,000+ A-shares |
| `build_market_5f.py` | Build a 5-minute basket from the 30 largest CSI 300 constituents using adjusted Baostock data; resample to 30 minutes |
| `app.py` | `chan_multi` jobs and latest/evaluation endpoints |
| `static/app.js` | Multi-level phase and evaluation panels |

```bash
# Rebuild the broad-market 30-minute series after data updates.
python3 build_market_30f.py

# Initial 5-minute basket build takes about 30 minutes; supports resuming.
python3 build_market_5f.py --top 30 --start-year 2020

# Evaluate all combinations, or select one with --combo.
python3 chan_multi_engine.py --combo all
```

Artifacts under `data/chan_multi/`:

```text
bars/                 market_30f.parquet, market_5f.parquet, 000300_SH_daily.parquet
levels/               D/W/M/5F/30F per-bar caches; fingerprint includes ENGINE_VERSION
combos/<combo>/        daily.csv, eval.json, latest.json, structures.json
summary.json
```

## 2. Data definitions

Coverage below describes the documented October 18, 2026 run, not a live availability promise.

| Level | Source | Coverage in the documented setup | Definition |
|---|---|---|---|
| 5F | Top 30 CSI 300 constituents at fixed June 30, 2020 weights; adjusted Baostock bars | From January 2020 | Weighted stock-return basket, not tick data for ETF 510300 |
| 30F in 5F–30F–D | Resampled from the same 5F basket | From January 2020 | Same underlying instrument proxy across both intraday levels |
| 30F in 30F–D–W | Equal-weight `astock_30min_2000_panel`, 2,000+ A-shares | June 2000–September 16, 2026 | Market breadth proxy with advancing counts, five-bar breadth, share above MA20 and cross-sectional dispersion |
| D/W/M | CSI 300 daily index from Tushare `index_daily` | From January 2005 | Calendar-week/month aggregation; timestamp is 15:00 on the final trading day |

The original setup could not obtain a ten-year 5-minute index series: its Tushare `stk_mins` entitlement was limited to roughly one request per hour, while the tested East Money/Sina/Tencent interfaces supplied only recent or approximately 1,488 bars. Baostock returned no index/ETF minute series but supplied stock 5-minute data from 2020. The fixed constituent basket therefore serves as a proxy, with its source and coverage labeled in the UI. These observations describe that setup rather than every provider account.

## 3. Causal structure rules

1. **Containment:** merge contained bars according to the current direction, using higher highs/higher lows when rising and lower lows/lower highs when falling. A merged bar closes only when a subsequent non-contained bar appears.
2. **Fractals:** identify a top/bottom from three closed merged bars. Confirmation occurs at the source bar that closes the right neighbor, never earlier.
3. **Strokes:** alternate tops and bottoms, with fractal centers separated by at least `min_gap=4` merged bars. A more extreme same-type fractal may replace the last endpoint. Previously emitted per-bar states remain unchanged; `test_chan_core` checks prefix invariance.
4. **Central zones:** three consecutive strokes define overlap bounds `ZD=max(lows)` and `ZG=min(highs)`. Extend while subsequent strokes overlap; terminate after two consecutive strokes leave. The initial three strokes fix ZD/ZG, although an active zone can update when its last stroke endpoint changes. Historical snapshots remain frozen.
5. **Phases:** `close>ZG` → advancing (1); `close<ZD` → declining (-1); otherwise consolidating (0). `trend_score = 0.45×zone_position + 0.25×zone_slope + 0.30×last_stroke_direction`.
6. **Divergence:** compare DIF extrema across adjacent strokes in the same direction; top divergence is -1 and bottom divergence +1.
7. **Third-type entries/exits:** simplified markers for a breakout beyond ZG/ZD followed by a retracement that does not reenter the zone.

## 4. Features across timeframes

Each level emits `phase`, `trend_score`, `pivot_pos`, `pivot_width_pct`, `pivot_slope`, `pivot_zd_slope`, `pivot_width_rank`, `stroke_accel`, `dist_zg_pct`, `dist_zd_pct`, `pivot_age`, `pivot_strokes`, `last_dir`, `last_stroke_ret`, `div`, `third_buy`, `third_sell`, `phase_run`, `bars_since_fractal`, `has_pivot` and `breakout_strength`.

Combined features include:

- `align_score = 0.25×lower_trend_score + 0.35×middle_trend_score + 0.40×higher_trend_score`.
- `align_phase`, `consensus`, `all_up/all_down`, `phase_gap`, `score_gap`, `div_sum`, `breakout_higher`.
- Auxiliary market breadth, not Chan levels: `breadth_up`, `breadth_5`, `breadth_ma20`, `dispersion` and their five-day changes.
- Price controls: 1/5/20/60-day returns, 20/60-day volatility, distances from MA5/20/60, RSI14, MACD histogram, ATR14, 20-day range position and volume ratio.

## 5. Labels and validation

The primary five-day path label is:

```text
fwd_ret = close[t+5]/close[t] - 1
fwd_max = max(close[t+1..t+5])/close[t] - 1
fwd_min = min(close[t+1..t+5])/close[t] - 1
advancing      if fwd_ret >= +1.5% and fwd_min >= -2.5%
declining      if fwd_ret <= -1.5% and fwd_max <= +2.5%
consolidating  otherwise
```

Path labels classify an advance followed by a reversal as consolidation. Endpoint-only labels remain a comparison in `endpoint_comparison`.

Walk-forward models for test year Y train only on samples with `label_end < Y-01-01`, and retrain annually. `test_chan_multi_engine` checks the exclusion of future labels and bars.

| Model | Definition |
|---|---|
| `hgb_logit` | HistGradientBoosting with balanced sample weights; primary model |
| `hier_logit` | Trend/consolidation followed by direction; trend threshold calibrated on the final 20% of training data |
| `combined_logit` | Multinomial logistic regression using Chan, breadth and price features |
| `chan_logit` | Chan features only |
| `price_logit` | Breadth and price features only |
| `rule` | Three-level phase agreement without training |
| `momentum` | 20-day momentum with a 1% dead band |
| `single_chan` | Daily phase only |

Balanced accuracy (BA) is the macro-average of recall across the three classes. Reports include confusion matrices, annual BA, monthly block-bootstrap 95% confidence intervals and intervals for ΔBA against the rule baseline.

## 6. Recorded results: October 18, 2026

| Combination | OOS period | n | HGB BA | Hierarchical BA | Combined logistic BA | Rule BA | Single-level BA |
|---|---|---:|---:|---:|---:|---:|---:|
| 5F–30F–D | Jan 2022–Oct 2026 | 1152 | 0.364 | 0.380 | 0.382 | 0.338 | 0.350 |
| 30F–D–W | Jan 2016–Sep 2026 | 2597 | 0.384 | 0.349 | 0.338 | 0.333 | 0.323 |
| D–W–M | Jan 2016–Oct 2026 | 2613 | 0.361 | 0.323 | 0.318 | 0.337 | 0.326 |

- The strongest ten-year result is 30F–D–W: HGB 0.384, ΔBA +0.052 versus the rule, 95% CI **[0.016, 0.088]**.
- 5F–30F–D has only about 4.8 OOS years and a 2020–2021 initial training window. Hierarchical/combined logistic BA is 0.380–0.382 versus HGB 0.364; the HGB improvement interval crosses zero.
- D–W–M: HGB 0.361, ΔBA +0.024, 95% CI **[-0.009, 0.064]**.
- For 30F–D–W, path-label BA is 0.384 versus 0.351 for endpoint labels, a 3.3 percentage-point difference.
- Structural phases are persistent. For the daily phase three days ahead, persistence achieves BA around 0.81 versus HGB 0.72. HGB did **not** beat this persistence baseline.

These results support a modest improvement in three-class classification and an interpretable phase overlay. Direction prediction remains a weak signal; BA is not trading return.

## 7. Limits of this evaluation

1. The 5F series starts in 2020. Ten-year 5F evaluation requires a source with earlier minute history.
2. The documented broad-market 30F panel ends September 16, 2026. That run's 30F–D–W state uses D at September 16 and W at September 11; the UI labels the lag.
3. The 30F broad-market series and 5F constituent basket are proxies, not the CSI 300 index itself.
4. Daily samples have overlapping five-day labels; monthly block bootstrap only partially addresses dependence.
5. Classification evaluation excludes fees, slippage and price-limit execution constraints.
6. H=5, threshold 1.5%, path labels and HGB were declared as the primary protocol; parameter-grid results are diagnostic.

## 8. UI and API

The trend section exposes combination selection, validation jobs, KPIs, three phase states, confusion matrices, annual BA, H×threshold grids, sources and lag information. The panel originated on the factor page and was subsequently moved to trend analysis.

- `GET /api/chan_multi?combo=5F-30F-D|30F-D-W|D-W-M`
- `GET /api/chan_multi/eval?combo=...`
- `POST /api/jobs {"kind":"chan_multi","params":{"combo":"..."}}`

When changing `static/app.js` or `templates/index.html`, bump the asset `?v=` cache version. The initial multi-level release used `20261018-chan-multi3`.
