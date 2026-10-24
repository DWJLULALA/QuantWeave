# Demo mode

The local demo makes the product available **without a market-data account, GPU or private research assets**. Follow [README_AGENT.md](../README_AGENT.md) for isolated installation and health checks.

```bash
pip install -r requirements-demo.txt
python demo_server.py --host 127.0.0.1 --port 7072
# Open http://127.0.0.1:7072
```

The badge `DEMO · 冻结快照` means the application is using frozen demo data. The current application interface is in Chinese.

## How it works

`demo_server.py` reuses the live application's `templates/` and `static/`; only its data backend changes.

| Live research | Demo |
|---|---|
| Snapshots under `data/` and `runs/` | Files in `demo/snapshots/` |
| Subprocesses execute research scripts | In-memory threads simulate stages over about six seconds |
| Paper account persists to JSON | Paper account lives in memory and resets on restart |
| Stock bars come from market panels | Deterministic synthetic OHLC derived from each code's hash |
| Trend details use per-code research assets | Frozen `demo/snapshots/trends_detail/` files |

Jobs still use `POST /api/jobs` → polling `GET /api/runs/<id>` → refreshing the latest snapshot. Progress bars, log tails, completion states and downloads therefore use the actual frontend workflow.

## Snapshot inventory

Sizes are approximate and refer to the bundled snapshot set.

| File | Contents | Size |
|---|---|---:|
| `status.json` | Capabilities, daily dates and intraday coverage | 6 KB |
| `config.json` | Default configuration | 0.6 KB |
| `factors_latest.json` | Factor replay results | 29 KB |
| `composite_latest.json` | Composite TopK and daily summaries | 64 KB |
| `intraday_latest.json` | Intraday forecasts and paths | 344 KB |
| `intraday_analytics.json` | Intraday aggregates | 153 KB |
| `intraday_industry_ranking.json` | Relative industry strength | 76 KB |
| `intraday_market_curve.json` | Market curve | 6 KB |
| `intraday_ic_trend.json` | Trimmed IC history | 100 KB |
| `market_volume_history.json` | Turnover and limit-up aggregates | 103 KB |
| `market_wave.json` | Last 900 days of market wave data | 117 KB |
| `chan_phase.json` | Chan phases | 52 KB |
| `chan_multi_*.json` | Last 900 days for three combinations | 700 KB × 3 |
| `chan_multi_eval_*.json` | Walk-forward results | 112–159 KB |
| `chan_classic_cases.json` | Per-bar classic-case slices | 282 KB |
| `trends.json` | Broad market and 31 first-level Shenwan industries | 813 KB |
| `trends_detail/` | 34 codes × D/W/M, 102 files | 11 MB |
| `champion_workbook.json` | Workbook with stocks clipped to Top 320 | 397 KB |
| `runs.json` | Seven synthetic run records | 4.5 KB |
| `artifacts.json` | Eight demo artifact entries | 1.3 KB |

Sample CSV, JSON and log downloads are stored in `demo/artifacts/`.

## Trimming and sanitization

1. Keep the last 420/260/120 trend bars by timeframe and the last 900 days of wave/multi-level structures.
2. Keep Top 320 workbook stocks, 260 stocks in IC history and event windows for classic cases.
3. Recursively remove or replace private absolute paths, usernames, hostnames, private addresses and execution-environment details. Use portable `/opt/quantweave` and `/opt/kronos` placeholders.

See [PRIVACY.md](PRIVACY.md) for the sanitization scope.

## What the demo represents

Dates are frozen at the October 22/23, 2026 snapshots; “latest” means latest within that set. Individual-stock OHLC used for interactions is synthetic. Paper positions and P&L are demonstration data, not a person's account. Jobs simulate progress and return frozen results rather than recomputing models; some date-specific operations therefore return a fixed example. The demo supports product exploration and code review, not trading decisions.

## Regenerating snapshots

The repository has no universal extraction script because upstream research interfaces vary.

1. Run `app.py` in the live environment and inspect `/api/status` capabilities.
2. Request the relevant read-only `GET /api/*` endpoints and save their JSON.
3. Trim long series and large tables using the rules above.
4. Recursively remove private identities and paths.
5. Validate JSON with `python -m json.tool` and run `test_demo_server.py` before committing.

Tests check required files, readable fields, Top 320 clipping, artifact downloads, job lifecycle and 404 behavior.
