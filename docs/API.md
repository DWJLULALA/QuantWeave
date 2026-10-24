# API contract

All endpoints use the `/api` prefix and JSON requests/responses, except artifact downloads. `demo_server.py` implements a subset of the same contract for the shared frontend. Chinese UI values and workbook keys below are preserved as actual wire-format examples; translating this documentation does not change the API.

## Conventions

- Timestamps: ISO 8601 with timezone, such as `2026-10-22T18:12:11+08:00`.
- Dates: parameters accept `YYYY-MM-DD` or `YYYYMMDD`; responses can contain either.
- Unavailable read endpoints return `200 {available:false, reason}` or `503`; the frontend displays the reason.
- Write endpoints return `400` for invalid parameters, `409` for unavailable job kinds and `404` for missing resources.
- There is no built-in authentication. Bind to `127.0.0.1` by default; add an authenticated reverse proxy for network exposure.

## Status and configuration

### `GET /api/status`

Overall application status, requested first when the frontend starts.

```json
{
  "ok": true,
  "demo": false,
  "generated_at": "2026-10-22T22:31:00+08:00",
  "capabilities": {
    "data_update": {"available": true, "reason": "更新脚本与数据目录已就绪"},
    "factor_evaluate": {"available": true, "reason": "..."}
  },
  "data": {"latest_date": "20261022", "panel_count": 29, "...": "..."},
  "intraday": {"codes": 5525, "status": "partial", "latest_timestamp": "2026-10-23 10:00:00"}
}
```

`demo:true` displays the frozen-demo badge, `DEMO · 冻结快照`.

### `GET /api/config` · `PUT /api/config`

Read/write application configuration. `PUT` accepts a full or partial configuration and returns the updated object.

```json
{
  "factor": {"composite": "E1+E3+E5+E10", "gate_alpha": 0.65, "exit_rank": 1000, "top_k": 100, "fee_bps": 10},
  "data_update": {"auto": true},
  "chan_multi": {"combo": "5F-30F-D"},
  "predict": {"auto": true}
}
```

### `GET /api/review` · `PUT /api/review`

Read/write daily review notes. Demo mode returns frozen examples.

## Jobs

### `POST /api/jobs`

```json
{"kind": "factor_evaluate", "params": {"date": "2026-10-22", "gate_alpha": 0.65}}
```

Supported `kind` values:

| kind | Purpose | Typical duration |
|---|---|---|
| `data_update` | Daily / 30F increments and quality audit | Minutes |
| `factor_evaluate` | Causal replay | Seconds to minutes |
| `composite_factor` | Weighted cross-sectional blend of selected Kronos E1–E10 horizons | Seconds |
| `predict` | Daily inference | Minutes |
| `intraday_predict` | Intraday inference | Seconds |
| `chan_multi` | Multi-level structures and walk-forward evaluation | Minutes |
| `trend_refresh` | Industry / broad-market trend snapshots | Minutes |

Response `202`:

```json
{
  "available": true,
  "job": {
    "id": "f0d2c85a7e11",
    "kind": "chan_multi",
    "status": "running",
    "stage": "1/4",
    "progress": 0.12,
    "created_at": "2026-10-22T18:30:15+08:00"
  }
}
```

### `GET /api/runs` · `GET /api/runs/<id>`

Run list / individual details. Details include `params`, `metadata`, `tail` (recent log lines), `returncode`, `stage`, `progress` and `finished_at`.

### `GET /api/artifacts` · `GET /api/artifacts/<path>`

Artifact index and downloads. Example index item:

```json
{"path": "chan_multi_5F-30F-D.csv", "size": 5526, "modified": 1761000000, "kind": "csv"}
```

Download paths are normalized, reject traversal through `..`, and are restricted to the run and demo artifact directories.

## Result snapshots

These endpoints return `{available, path, result}`; the inner result schema is selected by `kind`:

| Endpoint | `result.kind` |
|---|---|
| `GET /api/results/latest` | `kronos_prediction` |
| `GET /api/factors/latest` | `factor_evaluation` |
| `GET /api/composite/latest` | `kronos_composite_factor` |
| `GET /api/intraday/latest` | `intraday_timing` |

Key factor-result fields:

```json
{
  "kind": "factor_evaluation",
  "engine_version": "causal_cash_book_v2",
  "signal_date": "20261022",
  "metrics": {"total_return": 0.0, "sharpe": null, "max_drawdown": 0.0, "turnover": 0.0},
  "daily_metrics": [{"date": "20260902", "equity": 1.0, "exposure": 0.0, "holdings": 0}],
  "effective_parameters": {"gate_alpha": 0.65, "fee_bps": 10, "top_k": 100}
}
```

Composite results use `top` for TopK details, `date_summary` for daily cross-sections and `future` for target dates and predicted means at selected horizons. `horizons` and associated weight fields describe the selected E1–E10 configuration; this is not limited to a next-day forecast.

## Trends

### `GET /api/trends`

Returns broad-market and industry rankings together:

```json
{
  "asof": "20261022",
  "available": true,
  "classifications": {
    "sw1": {"id": "sw1", "name": "申万一级", "count": 31, "industries": []},
    "sw2": {"id": "sw2", "name": "申万二级", "count": 134, "industries": []},
    "csi10": {"id": "csi10", "name": "中证全指行业", "count": 10, "industries": []}
  },
  "markets": [{"code": "000001", "name": "上证指数", "trend_score": 0, "phase_d": "上涨"}],
  "p_method": "seven_vote_smooth_0.9",
  "ranking_method": "fixed_weights_v1",
  "strict_rule_version": "1.1.0"
}
```

Industry items include `bottom` (bottom/strict first-buy evidence), `phase_d`, `phase_w`, `trend_score`, `p_impulse`, `p_fast`, `rs20`, `rs60`, `position120`, `rank` and `data_quality`.

### `GET /api/trends/<code>?level=D|W|M`

Returns `{available, generation, summary, chart, wave, p_method}` for one code/timeframe. `chart` includes OHLC bars, strokes, central zones and recursive segments; `summary` includes phase, bottom confirmation and position.

### `GET /api/market_wave` · `GET /api/chan_phase`

Market wave and Chan-phase series for trend-page overlays.

### `GET /api/chan_multi?combo=5F-30F-D`

Multi-level structure snapshot. `combo` accepts `5F-30F-D`, `30F-D-W` or `D-W-M`.

### `GET /api/chan_multi/eval?combo=5F-30F-D`

Walk-forward evaluation: `balanced_accuracy`, confusion matrices and per-window results.

### `GET /api/chan/classic-cases`

Classic-case index and per-bar slices:

```json
{
  "available": true,
  "cases": [{
    "id": "2014_bull",
    "name": "2014 牛市启动与延续",
    "events": [{"event_id": "e1", "name": "2014-07-31 · 平滑转强", "date": "2014-07-31"}],
    "bars": [{"date": "2014-07-31", "open": 2179.54, "high": 2202.13, "low": 2173.90, "close": 2201.56}]
  }]
}
```

## Daily model

### `GET /api/champion_workbook`

```json
{
  "available": true,
  "overview": {"signal_date": "20260903", "rows": 5488},
  "sheets": {
    "个股最新": {"columns": ["股票代码", "股票名称", "申万二级", "正式收盘", "全市场排名", "10日EMA分数", "..."], "rows": []},
    "行业最新": {"columns": [], "rows": []},
    "E市场曲线": {"columns": [], "rows": []}
  }
}
```

Demo mode clips the latest-stock sheet, `个股最新`, to Top 320 while preserving the live schema.

## Intraday

| Endpoint | Purpose |
|---|---|
| `GET /api/intraday/analytics` | Intraday summary, forecast paths and industry strength |
| `GET /api/intraday/industry-ranking` | Relative industry strength |
| `GET /api/intraday/market-curve` | Current-session market curve |
| `GET /api/intraday/ic-trend?date=YYYY-MM-DD` | Intraday IC history |
| `POST /api/intraday/timing` | Entry/exit windows and position reminders |
| `GET /api/market/volume-history?days=250` | Turnover history and limit-up aggregates |

## Paper account

### `GET /api/intraday/account`

```json
{
  "available": true,
  "account": {
    "capital": 1000000,
    "cash": 300000,
    "market_value": 734500,
    "total_asset": 1034500,
    "positions": [{"id": "a1b2c3", "ts_code": "600519.SH", "name": "贵州茅台", "shares": 100, "entry_px": 1480.0, "last_price": 1522.5, "return": 0.0287, "float_pnl": 4250, "market_value": 152250, "action": "HOLD"}],
    "trades": [],
    "stats": {"total_pnl": 30330, "n_trades": 11, "win_rate": 0.6364, "profit_factor": 1.92}
  }
}
```

### `PUT /api/intraday/account`

```json
{"capital": 1000000, "reset": false}
```

`reset:true` clears positions and trades. `capital` must be positive and cannot be below the current position cost.

### `POST /api/intraday/positions`

```json
{"ts_code": "600519.SH", "shares": 100, "entry_price": 1480.0, "entry_date": "2026-10-23"}
```

Record a paper position and deduct cash. Resubmitting the same code replaces its prior position and refunds the prior cost.

### `DELETE /api/intraday/positions/<id>`

Undo an entry and refund cash without creating a trade.

### `POST /api/intraday/positions/<id>/sell`

```json
{"price": 1522.5, "reason": "手动平仓"}
```

Record a close, calculate P&L and update aggregate statistics. If `price` is omitted, use the latest reminder price.

> Account endpoints only modify local JSON in live research mode or in-memory dictionaries in demo mode. **There is no brokerage order interface.**

## Market data

### `GET /api/market_snapshot`

| Parameter | Definition |
|---|---|
| `mode` | `stock` for one stock, or `snapshot` for the market |
| `symbol` | `600519.SH` |
| `limit` | 1–200; default 100 |
| `date` | Optional, `YYYYMMDD` |
| `industry` | Optional industry code |

Response for `mode=stock`:

```json
{
  "available": true,
  "symbol": "600519.SH",
  "ohlc": [{"timestamps": [1761000000000], "open": [1480], "high": [1530], "low": [1470], "close": [1522.5]}],
  "horizon": [{"raw_mean": 0.012, "score": 1.2}],
  "factor_history": []
}
```

### `GET /api/visuals/market` · `GET /api/visuals/factors`

Visualization compatibility endpoints. Return `503` when computation is unavailable; the frontend supports a fallback.

## Error example

```json
{"available": false, "reason": "尚未生成（运行 chan_multi_engine.py --combo D-W-M）"}
```

The frontend displays `reason` directly in the corresponding card. Preserve this convention for new endpoints.
