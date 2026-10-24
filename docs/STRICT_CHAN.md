# Shenwan data repair and strict recursive first-buy rules

Specification date: October 19, 2026. This implementation serves the trend page independently of portfolio gates and return replay. Its fixed historical start is January 1, 2022, after the 2021 Shenwan classification was introduced. It appends closed, observed bars rather than resetting the structure with a rolling 1,000-bar window. Comparisons use the same closing date within a snapshot.

## Market data and provenance

Official SWS Research daily OHLC is the primary source; existing source rows are preserved. Reconcile dates against the SSE trading calendar. September 25, 2026 was a Mid-Autumn Festival closure and must not be filled with a fabricated bar.

Missing dates may be filled from AShareAPI daily bars for the same Shenwan code, `pt01+801xxx`. Verify the code using Tencent's quoted name, then compare at least 20 overlapping sessions. At least 99% of closes must match within `max(0.011 index points, 0.1 basis points)`. Record every O/H/L/C disagreement, including possible opening-price revisions. Retain official high/low values where sources disagree, and block strict confirmation when a conflict intersects its evidence window. A supplementary observed bar can fill a gap without resolving an existing source conflict; the UI reports gaps and conflicts separately.

The documented batch interface accepts at most five indices per request. The adapter follows the provider's published anonymous proof-of-work challenge and 15-request-per-minute limit, with a 4.3-second minimum interval in one process. It does not purchase services or reuse another person's credentials. Request only windows around gaps and cache raw bytes, SHA256, request URL and actual retrieval time. API failure, throttling or discontinued publication must not trigger forward filling, interpolation, constituent aggregation or guessed weekday bars. Ten unpublished level-two catalog entries remain marked unpublished.

References:

- [SWS Research daily index interface](https://www.swsresearch.com/institute-sw/api/index_publish/trend/?swindexcode=801010&period=DAY)
- [AShareAPI K-line endpoint](https://ashareapi.com/docs/endpoints/kline/)
- [AShareAPI challenge endpoint](https://ashareapi.com/docs/endpoints/challenge/)
- [Tushare trading calendar](https://tushare.pro/document/2?doc_id=26)
- [SSE September 2026 holiday announcement](https://www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml)

## Recursive structures

R0 is the stroke basis after containment handling and fractal confirmation at the input bar frequency. An endpoint fractal does not freeze a stroke: the preceding stroke becomes fixed only when a new opposing stroke is accepted, using the source-bar timestamp actually available then.

R1 and higher segments use feature-sequence fractals, containment, actual breaks and gap confirmation, not fixed groups of three strokes. The adapted feature-sequence implementation comes from `chan.py`, pinned at `429d6ed3043e27c93a003ba2b10e70a05575e1f5`, under MIT. Attribution and adaptation records are in `chan_vendor/`. Its nested-gap confirmation implements a stricter subset of the reference conditions and can delay or omit structures.

Each level exposes only frozen child structures to its parent; traversal through `.next` must not reveal unavailable children. Three completed child movements define a central overlap core ZD/ZG. Retracement into that core extends the existing zone and its developing DD/GG envelope. Freeze it after the third-type departure conditions complete. Overlap between adjacent full envelopes indicates expansion awaiting an upgrade, not a trend at the same level. A parent must genuinely complete recursively; separated cores alone cannot establish a higher-level structure or trend.

Rule `1.1.0` also checks an observed tail that has not yet been released as a complete child structure. If it has already broken a candidate top/bottom by the actual confirmation time, that old endpoint cannot freeze. Tail prices can veto a broken endpoint but cannot act as completed feature-sequence elements. The check is limited to the period after the last released child endpoint and before the current evidence cutoff. It neither reads future prices nor reintroduces all raw historical extrema discarded by containment handling.

An R2 strict recursive first-buy event requires:

1. Two completed adjacent zones at the same structural level, both with downward departures. The latter zone's GG must be below the former's DD. Initial trial segments are excluded.
2. The geometric ends of zones A/B precede c's start, although confirmation of those ends may wait for a later retracement. Zones formed during that wait cannot replace the original A/B. The entering b and departing c legs must be identifiable; a lower-level retracement must not reenter the zone, and c must make a new low.
3. The magnitude of c's negative MACD area is smaller than b's, while DIF at the low is higher. Both conditions are required by this implementation; they are not claimed as the only definition of Chan divergence.
4. The feature sequence freezes c. Inside c, at least two descending lower-level zones and confirmed terminal divergence at the same low must complete recursively down to the declared stroke basis.
5. No lower low occurs before the final necessary evidence arrives. Missing bars or source conflicts within the evidence window block confirmation.

Only complete R2-or-higher recursive evidence creates a strict signal. R1 stroke divergence remains a candidate. R1/R2 computed from daily bars must not be renamed as actual 30-minute/daily nested-timeframe evidence. Reliable historical Shenwan 30/60-minute OHLC was unavailable in this setup; intraday nesting stays unverified. R2+ records carry the bar-confirmed-stroke-basis qualification.

Level matching is explicit: an R2 zone is made of R1 segments, and its b/c legs are R1 segments too. An R2 parent segment cannot be used as an R2 zone exit. Likewise, R1 zone exits are frozen R0 strokes. When evidence completes a zone, recheck its earlier exit; do not permanently miss an event because the zone was unfinished at c's first arrival.

Nested evidence inside c is decomposed locally from c's children, excluding the first movement entering the initial zone. It need not coincide with global zone partitions or published lower-level labels. Preserve zone, momentum and common-endpoint evidence at every level.

The internal October 19, 2026 rule audit records the reference-text and source-code comparison. Trend rankings retain 40/30/20/10 weights. Their structural phase uses the close relative to the latest completed R2 zone; if unavailable, use the latest frozen R1 segment direction, otherwise zero. Retrospective waves remain excluded.

## Confirmation time, revisions and invalidation

Store three separate times: historical low `point_at`, arrival of the final required evidence `confirmed_at`, and first actual retrieval/calculation `observed_at`. Initial historical reconstruction is labeled as backfill, not an alert sent at the historical timestamp.

Prefix replay compares only frozen structures and signals knowable by that cutoff. Developing zones and terminal provisional strokes remain labeled mutable. Historical price revisions and rule upgrades differ from appending future bars: preserve observations and input fingerprints, then append withdrawal/restoration events rather than silently overwriting history.

The invalidation price stays fixed at the signal low. After confirmation, append an invalidation event only when a subsequent closed daily bar has a low strictly below it. Do not move the historical point, confirmation time or invalidation price, and do not delete invalidated signals. A later data gap puts current monitoring into a pending-verification state; it does not retroactively remove previously complete evidence.

Exclude unfinished daily, weekly and monthly bars. Accumulated intraday archives preserve the initial basis. `signal_ledger/{code}_{level}.jsonl` is append-only and records initial observations, invalidation, revision withdrawals/restorations and input checkpoints. `repairs/audit` stores per-industry repair/conflict audits. Publish each snapshot generation atomically; retain the previous complete generation on failure.

## Validation

Tests cover actual stroke-freeze times, feature sequences with and without gaps, containment and zone extension, complete descending envelopes, missing terminal recursion, new lows before confirmation, invalidation afterward, supplementary-source identity/price checks, backfill timestamps, conflict blocking, prefix replay and future-crash perturbations. Separate audits inspect snapshot dates, gaps, signals and structure replay for 155 observed Shenwan industries; counts belong to their dated audit outputs.

Version `1.1.0` adds two recognition regressions. A 113-bar SSE fixture reproduces a candidate endpoint already broken by an observed tail at confirmation. A synthetic OHLC fixture passes through the complete fractal → frozen stroke → segment → zone → local recursion pipeline to produce an R2 first-buy event, without injecting zones/signals or mocking momentum decisions. Its low occurs at bar 937 but is confirmed only after bar 1059 closes; it is unavailable one bar earlier. A later crash appends invalidation without changing the point, confirmation time or threshold. This synthetic positive case validates implementation behavior, not trading performance.

The classic-case page uses `classic_cases.py` to reconstruct five historical SSE cases and separately identifies the synthetic proof case. Volume/amount ratios use the preceding 20-session average, excluding the current day. Price-volume observations are educational overlays: they do not rewrite structure signals, E factors or portfolio gates. Historical E data does not cover those classic years, so the page reports no combined-strategy win rate or return.
