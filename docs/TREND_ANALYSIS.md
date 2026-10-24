# Trend analysis

Route `#trends` contains broad-market and industry views. Market wave tendencies, Chan phases and multi-level evaluation moved here from the factor page. Historical smoothed P remains available alongside separately computed sensitive rules and index position. In-sample fits and retrospective wave segments are labeled explicitly.

Broad-market views cover the SSE Composite, CSI 300 and CSI 500, with two-index comparison. Industry views default to the Shenwan 2021 classification: 31 first-level and 134 second-level entries. Filter by parent, name/code or daily bottom state. Ten CSI All Share industries, codes 000986–000995, remain available. The catalog uses the official 2021 mapping and is cross-checked against Tushare `index_classify`; versioned `sw_catalog.json` preserves parent relationships and official names.

CSI daily series come from SSE, with corresponding Tencent indices for intraday data. Shenwan daily OHLC comes from SWS Research. Weekly/monthly bars are aggregated from daily data. Reliable historical Shenwan 30/60-minute OHLC was unavailable in this setup, so those choices are disabled. Individual-stock highs/lows are not combined into invented industry OHLC.

## Data and rankings

CLI: `python trend_analysis.py --data-dir data/trends`.

Refresh writes an independent generation directory and atomically replaces `snapshot.json` after all files are ready. Snapshot reads do not access the network. A failure retains the previous generation. The UI and morning/evening pipelines dispatch a separate `trend_refresh` job, requiring no GPU or model predictions; another request reuses an active job of that kind.

- `GET /api/trends`: same-close industry rankings, coverage, unavailable reasons, methodology and generation time.
- `GET /api/trends/<code>?level=D|W|M|60F|30F`: structures, OHLC, MACD and the index's daily tendency series, where that timeframe is supported.
- `POST /api/jobs {"kind":"trend_refresh","params":{}}`: background refresh, with progress/logs in the run center.

Rankings use the latest completed CSI 300 close and compute percentiles independently within each classification. The complete-data ranking excludes industries without that close, a continuous last 61 benchmark sessions or sufficient structure history.

A separate reference ranking allows internal gaps. Its 20/60-session relative strength still uses the exact benchmark-calendar endpoints; removing missing rows must not shorten the horizon. Structure and P use observed data and can be affected by gaps. The two lists rank separately. Filtering by parent preserves the full second-level rank.

```text
score = 100 × [0.40×structure + 0.30×smoothed_P
             + 0.20×relative_strength_20_percentile
             + 0.10×relative_strength_60_percentile]
structure = (0.7×daily_phase + 0.3×weekly_phase + 1) / 2
relative_strength_N = (industry_now/industry_N_ago) / (CSI300_now/CSI300_N_ago) - 1
```

Phases are −1/0/+1. Each structural phase uses the close relative to the latest completed R2 zone, falling back to the latest frozen R1 segment direction, then zero if neither exists. Relative-strength percentiles use average ranks among comparable industries that day. Weights are fixed display rules, not optimized backtest parameters; the score is not a calibrated probability of an advance.

P combines Dow structure, Supertrend, ADX, Choppiness, moving-average order, 20-day momentum and impulse count, with EMA decay 0.9. The sensitive line uses four short-term rules and decay 0.6. A ZigZag turn enters the knowable set only after the reversal is observed. Gray retrospective wave segments are drawn separately and excluded from P and rankings. The 120-day close-range position measures price location, not probability.

## Structures and confirmation

Fractals follow OHLC containment handling and wait for the right merged bar to close. Use extrema retained by merging, without reviving discarded prices. Historical strokes are solid only once an opposing stroke has been accepted; the last stroke and raw-price extensions remain dashed candidates.

Gray shading marks only the latest three-stroke overlap observation area. Purple lines are frozen recursive segments; green boxes are recursive zones; dashed boxes are developing zones. Select R2/R3/R4 zones and their child-level segments. Feature sequences handle containment and gaps. Stroke freezing uses the opposing-stroke acceptance time, not merely the endpoint fractal time.

Weekly/monthly charts use completed periods. Intraday charts retain closed bars and accumulate archives. Rankings use daily data; more recent intraday bars do not alter the close-based ranking.

Validation: `python -m unittest test_strict_chan test_trend_analysis test_trend_api -q`. Additional audits compare daily append stability and truncated reconstruction on actual industry histories.

## Bottom diagnostics and data quality

Selecting an industry scrolls to its structure chart. The observation card shows the recent 20-bar low, right-side confirmation date, fractal upper boundary, whether the latest two closes exceed it, distance from the low, stroke-level MACD-area/DIF comparisons and remaining conditions.

A local bottom fractal waits for the right merged bar to close; a new low removes the current low confirmation. Adjacent descending strokes with a lower start/new low but weaker negative MACD area and DIF are only divergence candidates. Adding a bottom fractal creates a first-buy observation candidate, not confirmed strict trend divergence.

Strict R2+ events require fully separated descending zone envelopes, b/c divergence, a new low in c without a later lower low before confirmation, and completed lower-level terminal zones and recursive divergence. R1 candidates are not promoted. The UI separates the historical point, structure confirmation, first actual observation, fixed invalidation threshold and later invalidation/revision withdrawal. Gaps suspend evaluation; insufficient structure has an explicit reason. Daily-bar recursion does not establish real 30-minute nesting. See [STRICT_CHAN.md](STRICT_CHAN.md).

Quality checks precede diagnostics. Complete rankings require continuous data over the last 61 CSI 300 sessions and the structure input. Bottom diagnostics check daily inputs through the end of each requested period; a later gap cannot invalidate an earlier completed month's evidence. Preserve genuine gaps rather than interpolating, substituting previous closes for OHLC or using a similar industry. A reference score cannot establish a confirmed first-buy event. Keep catalog entries with unavailable sources, stale closes, insufficient history or gaps, and show their reasons.

Before the first repair on October 19, 2026, the snapshot ended October 16. All 31 first-level and 124 published second-level Shenwan entries belonged to the reference list because actual sessions, including October 15, were missing; the complete list was empty. Ten additional second-level entries had no published current index. A same-index supplementary source was then added and reconciled with the exchange calendar; September 25 was a closure, not a missing session.

Preserve official rows, add only retrieved OHLC, verify identity with overlapping opens/closes and record high/low conflicts. Conflicts intersecting strict evidence block confirmation. Current coverage is defined by `repair_summary` and each industry's `data_quality`, not those historical counts. Historical reconstruction records actual first retrieval times instead of impersonating historical live alerts. The internal October 19 source audit documents catalog verification.

Contract tests cover 31/134 parent relationships, fractal confirmation lag, new-low invalidation, unchanged 20/60-day anchors across gaps and prevention of bottom confirmation in published incomplete snapshots. Valid cached data can survive a network failure. A certificate-chain compatibility fallback is restricted to the fixed public SWS endpoint, and its source metadata records `tls_verified`.
