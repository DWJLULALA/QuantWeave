# Architecture

QuantWeave combines a single Flask process, a frontend without a build step and supervised subprocess jobs. Filesystem snapshots hold research state; the application requires no message queue, database or frontend framework.

## Layers

| Layer | Location | Responsibility |
|---|---|---|
| Presentation | `templates/index.html`, `static/*` | Plain HTML/CSS/JS, hash routing, inline SVG charts |
| API | `app.py` | Status, configuration, reviews, jobs, read-only snapshots and downloads |
| Jobs | `job_spec`, `run_command`, `monitor_job` in `app.py` | Validate parameters, build allowed argv, supervise subprocesses and record logs/exit codes |
| Computation | `factor_evaluate.py`, `chan_*.py`, `trend_analysis.py`, etc. | Python computation using defined input/output directories |
| Data | `data/`, `runs/`, `config.json` | Panels, structures, run records and artifacts |
| Demo | `demo_server.py`, `demo/` | Frozen snapshots and in-memory jobs for the same frontend |

`templates/index.html` loads `static/app.js` directly. Other page scripts attach as needed:

```text
app.js             Routing, application shell, job polling and shared charts
trend-analysis.js  Market comparisons, industry rankings and structure details
chan-cases.js      Classic-case replay
stock_views.js     Individual-stock OHLC dialogs
visuals.js         Small visualization helpers
```

## Request lifecycle

### Read-only snapshots

```text
GET /api/factors/latest
  → app.py reads runs/factor_*/result.json or the configured latest pointer
  → Return {available, path, result}
  → resultInto() dispatches by result.kind
  → factorResult / compositeResult / predictionResult renders the response
```

Read-only snapshot endpoints do not launch computation. Heavy work is requested through explicit jobs.

### Jobs

```text
POST /api/jobs {kind, params}
  → Require a currently available kind in CAPABILITIES
  → Validate allowed dates, codes, enums and numeric ranges
  → job_spec builds argv, cwd and metadata
  → Create runs/<12-character-id>/ and job.json; start a supervisor thread
  → Execute argv through subprocess with shell=False
  → Atomically write .exit.json after process exit
  → monitor_job reads logs and [stage n/m] markers, updating job.json
  → Frontend polls /api/runs/<id> every 2.5 seconds
  → Refresh the corresponding latest snapshot after completion
```

Arguments are passed as an argv array, without shell interpolation. Job kinds are allowlisted; callers cannot submit arbitrary commands. Exit-state JSON is written through a temporary file and `os.replace`. Active `data_update`, `chan_multi` and `trend_refresh` jobs are reused for another request of the same kind. Nonzero exit codes, timeouts and log tails remain visible to the frontend.

## Capability detection

`capability_state()` checks the configured environment and required files:

| Capability | Required assets |
|---|---|
| `data_update` | Update scripts and data directories |
| `review_write` | Writable review file |
| `factor_evaluate` | Factor script and market panels |
| `predict` | Daily-model script and model directory |
| `composite_factor` | Composite script and required snapshots |
| `intraday_predict` | Intraday script and 30F panel |
| `chan_multi` | Multi-level script and structure data |
| `trend_refresh` | Trend script and industry directory |

Unavailable capabilities do not become executable buttons. Direct requests return 409/503 with an explanation. This makes missing assets visible on a new installation.

## Result contract

Results share a stable outer structure:

```json
{
  "kind": "factor_evaluation",
  "engine_version": "causal_cash_book_v2",
  "generated_at": "2026-10-22T18:12:11+08:00",
  "signal_date": "2026-10-22",
  "summary": {},
  "metrics": {},
  "daily_metrics": [],
  "effective_parameters": {}
}
```

The frontend relies on `kind` and a small set of stable fields; additive fields are backward compatible. See [API.md](API.md).

## Computation modules

| Module | Responsibility |
|---|---|
| `update_latest.py` | Trading calendar, daily increments, index tails and quality audit |
| `update_intraday_rt.py`, `update_intraday_market_parallel.py` | Incremental and parallel market-wide 30-minute updates |
| `factor_evaluate.py` | Causal cash-ledger replay, IC, quantile groups and turnover |
| `composite_factor.py` | Cross-sectional blend of selected Kronos E1–E10 forecast horizons |
| `chan_core.py` | Containment, fractals, strokes, zones, divergence and per-bar states |
| `chan_multi_engine.py` | Three-timeframe features, labels and walk-forward evaluation |
| `strict_chan.py`, `strict_events.py` | Strict recursive first-buy evidence and event ledger |
| `build_market_5f.py`, `build_market_30f.py` | Market proxy baskets and breadth series |
| `trend_analysis.py` | Trend scores, P, relative strength, industry rankings and snapshots |
| `classic_cases.py` | Historical cases with per-bar replay slices |
| `engine_intraday.py`, `intraday_timing.py` | Intraday inference, entry/exit windows and reminders |
| `market_snapshot.py` | Stock OHLC, factor history and forecast horizons |
| `market_volume_history.py` | Turnover history and limit-up aggregates |
| `market_wave_factor.py`, `market_wave_rules.py`, `market_wave_eval.py` | Market wave features, rules and evaluation |
| `chan_vendor/` | Pinned MIT-licensed `chan.py` adaptation |

## Demo layer

`demo_server.py` serves the same frontend and contract. It returns files from `demo/snapshots/`, simulates job stages in memory and handles paper-account operations with an in-memory dictionary. The data backend changes; the interface implementation does not. `test_demo_server.py` uses Flask's test client to check key paths against that contract.

## Design choices and limits

A single process with supervisor threads keeps local research deployment small and exposes logs/exit codes directly. Filesystem state can be inspected, diffed and packaged. Plain JavaScript and SVG avoid a frontend build chain. Bundled snapshots let reviewers explore the application without model or data infrastructure.

Concurrency is limited; heavy workloads may need independent workers. Current job reuse and serialized workflows reduce duplicate work. Snapshot pointers use the last published version rather than database-style multi-version concurrency. Long tables rely on pagination; the demo workbook is clipped to Top 320. Live environments depend on upstream data layout, so another provider may require changes to `update_*` adapters and column mappings.
