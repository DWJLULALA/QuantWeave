# Research design and evidence

QuantWeave follows market phase → predicted stock rankings → replay and records. In this workflow, forecasts are converted into predictions of relative strength; the ranking itself can be wrong. RankIC is the rank correlation between prediction scores and realized returns. It measures ranking agreement, not percentage accuracy or portfolio returns. Market direction, transaction costs and execution need separate evaluation.

## Wave and Chan analysis

- Impulse-wave analysis checks whether highs, lows, trend bands, moving averages and momentum support continued movement. The trend page combines confirmed structures and rule-based tendencies. P maps rule scores onto a scale; it is not a calibrated probability of a price increase.
- Chan analysis applies containment handling and fractal confirmation, then constructs strokes, segments and overlapping central zones to describe advancing, declining and consolidating structures. Multiple timeframes expose agreement or disagreement across scales.
- A structure enters a decision only when confirmed. Retrospective wave layers are excluded from trend rankings. Phase identification can lag or fail.

## Kronos, recent-window LoRA and EMA

[Upstream Kronos](https://github.com/shiyu-coder/Kronos) is an autoregressive price-volume sequence forecasting model; see its [paper](https://arxiv.org/abs/2508.02739). This project studies LoRA adaptation, conversion of forecasts into cross-sectional scores, pooling across forecast vintages and day-to-day EMA.

A roughly two-month pre-trade window is one candidate training design, not an established optimal duration. Recovered notes describe monthly increments and recent data mixed with historical replay. The rp10 full-speed variant uses a July increment plus 10% historical replay; it must not be described as training exclusively on the two preceding months. Future labels attached to training examples must all end before the evaluation boundary.

EMA smooths rankings but introduces lag; on reversal days it can preserve an outdated style. UI parameters, vintage EMA and daily EMA in separate experiments are distinct configurations.

## Five target days under a shared protocol

The following values are transcribed from section 2 of the recovered research note *Bull-tail window fine-tuning experiment, 20260829*. Its evaluation protocol specifies target dates **August 24–28, 2026**, approximately **5,467 stocks**, temperature 0.6, top-p 0.9, one sample, seed 20260824, pooling across ten vintages, vintage EMA 0.65 and daily EMA 0.90.

| Variant | Five-day mean RankIC |
|---|---:|
| Kronos base | 0.0021 |
| rp10 full-speed fine-tuned variant, fingerprint `bdecdf09` | 0.0809 |
| Difference | +0.0788 |

This compares model variants under the same evaluation protocol. It is **not an independent ablation of LoRA and EMA**, and these values are not returns. Multiple model choices were explored; five days do not establish long-term stability or statistical significance. The comparison does not support a “39× return improvement” or a claim that newer training data always performs better.

## SET10: a complementary ranking style

SET10 combines ten price-volume factors with equal rank weights: REV_OH, OVERNIGHT, ILLIQ_20, BETA_60, VOL_RATIO, D_MA20, POS_20, RET_5, RET_10 and PV_CORR_20. Directions are aligned before aggregation. Drawdown, range position and moving-average deviation components give the blend a reversal orientation.

The research motivation is to complement weaknesses of the recently adapted Kronos variant on some reversal days. “Antagonistic” describes observed style complementarity in a sample, not permanent negative correlation or cancellation of risk.

| Target date | Kronos rp10 full-speed RankIC | SET10 RankIC |
|---|---:|---:|
| 2026-08-24 | 0.263 | 0.022 |
| 2026-08-27 | -0.362 | 0.195 |
| 2026-08-31 | -0.100 | 0.210 |

These selected dates illustrate changes across conditions, not the full period. The complete eight-day summary from the notes is in [evidence/research_summary.csv](evidence/research_summary.csv). Combination weights were selected within those eight days, and SET10 selection also has within-window bias. The retrospective combination results therefore do not establish executable returns or an out-of-sample improvement.

A separate note, *Kronos + SET10 trading strategy*, reports approximately **−0.80 correlation between daily IC time series** across 14 target dates from August 24 to September 10, 2026. This is different from cross-sectional rank correlation between stock scores and does not establish a stable hedge. The same note reports only two active days among 23 executable out-of-sample days under one rule set, with a net result of −0.77%. Complementary factors and an executable strategy are different evaluation questions.

## Recovery and verification status

Research Markdown, some workbooks and other replay artifacts were recovered. The original per-stock predictions and referenced `daily_rankic.csv` and `combo_scan.csv` for the five/eight-day comparisons were not found in the restored directory. This page and its CSV are **transcribed summaries, not recomputations from raw predictions**. Daily values retain three decimal places from the notes; means retain four, so recomputing means from rounded daily values can produce small differences.

Source notes, with titles translated for this documentation:

- *Bull-tail window fine-tuning experiment, 20260829*, sections 1–2 and 9; last appended September 3, 2026.
- *Supplementary experiment conclusions, 20260827*, sections 2–5: rp10 incremental training and replay design.
- *SET10 equal-weight factor card, 20260829*, sections 2 and 6–7: definition, selection bias and eight-day comparison.
- *Kronos + SET10 trading strategy*, September 11, 2026: 14-day IC correlation and subsequent trading evaluation.

Only aggregate research information is included here, without private accounts, server paths, training credentials or raw market data. Full revalidation requires the original predictions, labels, model fingerprints and execution protocol, followed by longer walk-forward evaluation.
