# Models and evaluation semantics

This document describes rules implemented by the code. Historical replay metrics do not establish future returns. Model scores are predictions; their relative ordering can be wrong.

## 1. Causal factor replay (`factor_evaluate.py`)

### Execution assumptions

```text
Generate scores at the close of signal day T (15:00)
  → Execute target weights at the T+1 open
  → Exit or rebalance at the T+1+H open after H trading days
  → Mark holdings to each daily close
  → Liquidate at the final replay close
```

- Execution uses the next open, not the signal-day close.
- Apply one-way `fee_bps` (default 10 bp) separately to buys and sells.
- Maintain daily `cash / holdings / market_value / total_asset` records.
- Hold at most `top_k` positions. Exit when a position falls below `exit_rank` or reaches its holding limit.
- Price limits, suspensions and missing opening prices prevent execution that day; defer to the next tradable session.

### Scores

Outside pure E mode, normalize Kronos and SET10 separately across the stock universe, apply causal EMA to each and then combine:

```text
K_t = decay_k × K_(t-1) + (1-decay_k) × normal_score(kronos_t)
S_t = decay_s × S_(t-1) + (1-decay_s) × normal_score(set10_t)
score_t = w_kronos × K_t + w_set10 × S_t
```

SET10 applies cross-sectional z-scores, clips at ±3, ranks each factor, then combines equal weights and applies `normal_score`. `decay` is the weight on the previous value. Recent training windows, LoRA and research comparisons are documented in [RESEARCH_EVIDENCE.md](RESEARCH_EVIDENCE.md).

`kronos_source=composite` dispatches to `composite_replay.py`: map cross-sectional percentiles for E1/E3/E5/E10 to [-1,1], require all four horizons and average them equally. This mode **does not include SET10 or daily EMA**. It must not be interpreted as the two-factor strategy implied by unrelated UI weights.

### Gates

- Chan: `up_only` permits holdings only in an advancing phase; `non_down` excludes declining phases; `off` disables this gate. A missing phase closes an enabled gate.
- Wave gating outside pure E mode: `gate_alpha` smooths the market wave score over time. Valid values are 0 (disabled) or [0.5,1). A nonnegative smoothed market score opens the gate.
- `gate_alpha=0` does **not** disable the Chan gate; use `gate_mode=off` explicitly.
- Gates do not alter stock ranking scores. They determine entry eligibility and trigger exits when closed; orders still follow subsequent-open and tradability constraints.
- Pure E replay supports Chan comparisons with `off/non_down/up_only`; it does not blend wave alpha into rankings.

### Outputs

- `total_return`, `annualized`, `sharpe`, `max_drawdown`, `volatility`.
- `turnover`, `hit_rate`, `avg_holding_days`.
- Daily `equity / exposure / holdings / cash`.
- Next-day / holding-period returns by score quantile, and RankIC.

RankIC compares predicted rankings with realized return rankings. It is not portfolio return or a percentage of correct predictions. Results are written to `runs/<job_id>/result.json` and `factor_daily.csv`; the frontend reads snapshots.

## 2. Composite factor display (`composite_factor.py`)

Select Kronos E1–E10 forecast horizons and calculate cross-sectional percentiles independently for each horizon:

```text
h_score = 2 × rank_pct(k_eh) - 1
composite_score = Σ weight_h × h_score
```

Weights are normalized and may be equal or custom. Outputs include TopK, selected horizons, effective weights, daily summaries, future target dates and means, and historical `stable/watch/break` indicators. This module does not train models or execute trades. It combines forecast horizons, not daily and intraday models.

The display accepts selectable horizons; pure E replay above uses the fixed E1/E3/E5/E10 set. These capabilities are distinct.

## 3. Chan structures

Modules: `chan_core.py`, `chan_multi_engine.py`, `strict_chan.py`.

### Single-level causal pipeline

```text
Raw OHLC bars
  → Directional containment handling
  → Top/bottom fractals, confirmed after the right merged bar closes
  → Alternating strokes with a minimum bar gap
  → Central zones from overlapping strokes
  → Divergence evidence
  → Per-bar advancing / declining / consolidating state
```

Historical per-bar snapshots are frozen. Developing endpoints and zones remain explicitly provisional; confirmation timing must not use future bars. The basic multi-level engine and strict recursive engine have different divergence and confirmation rules; see their dedicated documents.

### Multiple timeframes

Each timeframe runs its own causal pipeline. Higher timeframes provide direction; lower ones provide entry context.

| Combination | Purpose |
|---|---|
| `5F-30F-D` | Intraday entries, 30-minute structure, daily direction |
| `30F-D-W` | Swing research, 30-minute entries, daily/weekly direction |
| `D-W-M` | Longer horizons, daily entries, weekly/monthly direction |

Outputs include the three current phases and their agreement, recent zones/strokes/divergences, daily labels, walk-forward balanced accuracy, confusion matrices and window results. Training only uses labels available before each evaluation boundary; validation windows do not participate in fitting. See [CHAN_MULTI_LEVEL.md](CHAN_MULTI_LEVEL.md).

### Strict recursive first-buy structures

Rule version `1.1.0` requires completed recursive evidence at R2 or higher: descending adjacent zones with separated full envelopes, verified entering b and departing c legs, divergence, a new low in c, and completed lower-level recursive evidence. Confirmation is blocked by a lower low before the final evidence, relevant missing data or source conflicts. R1 stroke-level signals remain candidates.

The complete rule, including tail-break vetoes, level matching and confirmation timestamps, is specified in [STRICT_CHAN.md](STRICT_CHAN.md). `strict_events.py` maintains an append-only event ledger with event identifiers, code, level, confirmation time, invalidation and evidence. A daily-bar recursive level must not be relabeled as a verified intraday timeframe.

### Data quality

- Use only closed, observed bars; unfinished intraday bars do not enter structures.
- Reconcile missing Shenwan dates against the official trading calendar; record supplementary sources.
- Preserve conflicts in `data_quality` and failures instead of silently overwriting source data.
- Label strict recursive samples with the fixed `2022-01-01` start separately from earlier histories.

## 4. Trend score (`trend_analysis.py`)

```text
structure = (0.7 × phase_d + 0.3 × phase_w + 1) / 2      # phase ∈ {-1, 0, 1}
score = 100 × [0.40 × structure
             + 0.30 × p_smooth
             + 0.20 × rs20_percentile
             + 0.10 × rs60_percentile]
```

- `phase_d / phase_w`: daily/weekly Chan states, -1/0/1, mapped to [0,1]. The strict implementation uses the latest completed R2 zone, with a frozen R1 direction fallback; see [TREND_ANALYSIS.md](TREND_ANALYSIS.md).
- `p_smooth`: exponentially smoothed seven-rule P, decay 0.9, in [0,1].
- `rs20 / rs60`: relative 20/60-day performance against CSI 300, ranked across comparable industries.
- `position120`: current close within the 120-day closing-price range, **not a probability**.

| Vote | Rule |
|---|---|
| Dow structure | Higher highs and higher lows |
| Supertrend | Price above the trend band |
| ADX | +1 above 25; −1 below 20; otherwise 0 |
| Choppiness | +1 below 38.2; −1 above 61.8 |
| Moving averages | +1 for MA20 > MA60 > MA120; −1 for the reverse order |
| 20-day momentum | +1 above +2%; −1 below −2% |
| Impulse count | Check structural constraints using turns confirmed by that point |

Votes are −1/0/+1, except impulse count at 0/1. Compute `p_raw=(mean(votes)+1)/2`, then `p_smooth=0.9×p_smooth_prev+0.1×p_raw`.

**Retrospective-label isolation:** the trend page uses only turns confirmed by the current bar for its structure votes. Full-history retrospective ZigZag labels appear only in a gray review layer and do not enter P or rankings. This describes the trend-page implementation, not an audit of every older wave script. `ranking_uses_retrospective_labels` must be `false`; otherwise the frontend rejects the ranking.

## 5. Daily model (`predict`)

- Inputs: the latest fully closed daily panel and Kronos weights.
- Outputs: next-session target portfolio, planned exposure, `NO_TRADE` status and candidate rankings.
- Risk gates may produce zero targets and 0% exposure for `risk_off`, insufficient liquidity or excessive data gaps.
- Results are written to `runs/<job_id>/result.json` and shown as the next-session plan.

**`NO_TRADE` is a valid result, not a failure.** It means the strategy chose not to participate.

## 6. Intraday model (`engine_intraday.py` / `intraday_timing.py`)

- Input: the latest **closed** 30-minute bars; exclude the unfinished final bar.
- Outputs: a 16-bar forecast path per stock, EMA history, percentiles, states and position reminders.
- Entry window: 14:45–14:55; sale eligibility begins at T+1.
- Exit on the first applicable condition: an EMA signal turning negative at T+1 or later, a 5% stop loss, or an eight-bar holding limit.
- Industry rankings and market curves filter relative strength without changing individual forecast paths.

Outputs support research and reminders. They do not guarantee optimal entry/exit points or connect to a broker.

## 7. Evaluation limits

Historical in-sample and out-of-sample replay can contain selection, survivorship and data-revision biases. Costs model explicit fees, not market impact, slippage or capacity. Chan rules depend on minimum bar counts and overlap parameters. Trend scores are display rules, not calibrated probabilities. Model behavior must be evaluated under the intended data and execution conditions.
