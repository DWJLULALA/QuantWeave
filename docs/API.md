# API 契约

所有接口以 `/api` 为前缀，请求与响应均为 JSON（产物下载除外）。演示服务器 `demo_server.py` 实现同一套契约的子集，前端代码不区分两种模式。

## 约定

- 时间：ISO 8601 带时区，例如 `2026-10-22T18:12:11+08:00`；
- 日期：接口参数接受 `YYYY-MM-DD` 或 `YYYYMMDD`，响应里两种都可能出现；
- 只读接口失败时返回 `200 {available:false, reason}` 或 `503`，前端显示原因而不是白屏；
- 写接口参数错误返回 `400`，任务类型不可用返回 `409`，资源不存在返回 `404`；
- 没有内置鉴权，默认只监听 `127.0.0.1`；如需暴露到局域网，请在前面加反向代理与认证。

## 状态与配置

### `GET /api/status`

面板总状态。前端启动时首先调用。

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

`demo:true` 时前端显示 `DEMO · 冻结快照` 徽标。

### `GET /api/config` · `PUT /api/config`

读写面板配置。`PUT` 接受完整或部分配置，返回更新后的对象。

```json
{
  "factor": {"composite": "E1+E3+E5+E10", "gate_alpha": 0.65, "exit_rank": 1000, "top_k": 100, "fee_bps": 10},
  "data_update": {"auto": true},
  "chan_multi": {"combo": "5F-30F-D"},
  "predict": {"auto": true}
}
```

### `GET /api/review` · `PUT /api/review`

读写单日复盘记录。演示模式返回冻结样例。

## 任务

### `POST /api/jobs`

```json
{"kind": "factor_evaluate", "params": {"date": "2026-10-22", "gate_alpha": 0.65}}
```

`kind` 取值：

| kind | 作用 | 典型耗时 |
|---|---|---|
| `data_update` | 日线 / 30F 增量与质量审计 | 分钟级 |
| `factor_evaluate` | 因果回测 | 秒到分钟 |
| `composite_factor` | 日模型 × 分时合成 | 秒级 |
| `predict` | 日模型推演 | 分钟级 |
| `intraday_predict` | 分时推演 | 秒级 |
| `chan_multi` | 多级别结构 + walk-forward | 分钟级 |
| `trend_refresh` | 行业 / 宽基趋势快照 | 分钟级 |

响应 `202`：

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

运行列表 / 单条详情。详情包含 `params`、`metadata`、`tail`（最后若干行日志）、`returncode`、`stage`、`progress`、`finished_at`。

### `GET /api/artifacts` · `GET /api/artifacts/<path>`

产物索引与下载。索引项：

```json
{"path": "chan_multi_5F-30F-D.csv", "size": 5526, "modified": 1761000000, "kind": "csv"}
```

下载路径经过规范化，禁止 `..` 越界；只允许 `runs/` 与演示产物目录内的文件。

## 结果快照

以下接口统一返回 `{available, path, result}`（`result` 内层按 `kind` 区分）：

| 接口 | `result.kind` |
|---|---|
| `GET /api/results/latest` | `kronos_prediction` |
| `GET /api/factors/latest` | `factor_evaluation` |
| `GET /api/composite/latest` | `kronos_composite_factor` |
| `GET /api/intraday/latest` | `intraday_timing` |

因子结果的关键字段：

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

组合结果的 `top` 为 TopK 明细，`date_summary` 为逐日横截面，`future` 为次日预测均值，`horizons` 为 E1–E10 权重。

## 趋势

### `GET /api/trends`

一次返回大盘 + 行业榜：

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

行业项包含 `bottom`（底部/严格 1 买）、`phase_d`、`phase_w`、`trend_score`、`p_impulse`、`p_fast`、`rs20`、`rs60`、`position120`、`rank`、`data_quality`。

### `GET /api/trends/<code>?level=D|W|M`

单代码单级别详情，返回 `{available, generation, summary, chart, wave, p_method}`。`chart` 包含 K 线、笔、中枢、递归线段；`summary` 包含阶段、底部确认、位置。

### `GET /api/market_wave` · `GET /api/chan_phase`

市场浪型与缠论阶段序列，供趋势页叠加。

### `GET /api/chan_multi?combo=5F-30F-D`

多级别结构快照。`combo` 支持 `5F-30F-D`、`30F-D-W`、`D-W-M`。

### `GET /api/chan_multi/eval?combo=5F-30F-D`

walk-forward 评估：`balanced_accuracy`、混淆矩阵、逐窗口结果。

### `GET /api/chan/classic-cases`

经典案例索引与逐 bar 切片：

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

## 日模型

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

演示模式把 `个股最新` 裁剪到 Top 320，字段与真实模式一致。

## 分时

| 接口 | 说明 |
|---|---|
| `GET /api/intraday/analytics` | 分时汇总、预测路径、行业强度 |
| `GET /api/intraday/industry-ranking` | 行业相对强弱排名 |
| `GET /api/intraday/market-curve` | 市场当日曲线 |
| `GET /api/intraday/ic-trend?date=YYYY-MM-DD` | 分时 IC 历史 |
| `POST /api/intraday/timing` | 计算买卖窗口与持仓提醒 |
| `GET /api/market/volume-history?days=250` | 成交历史、涨停/连板聚合 |

## 纸上账户

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

`reset:true` 清空持仓与交易记录；`capital` 必须为正数，且不能低于当前持仓成本。

### `POST /api/intraday/positions`

```json
{"ts_code": "600519.SH", "shares": 100, "entry_price": 1480.0, "entry_date": "2026-10-23"}
```

记录纸上持仓并扣减现金；同一代码重复提交会覆盖旧仓并退回旧成本。

### `DELETE /api/intraday/positions/<id>`

撤销录入：退回现金，不产生交易记录。

### `POST /api/intraday/positions/<id>/sell`

```json
{"price": 1522.5, "reason": "手动平仓"}
```

写入平仓记录、计算盈亏与累计统计；`price` 省略时使用最新提醒价。

> 所有账户接口只操作本地 JSON 文件（真实模式）或内存字典（演示模式），**没有任何券商下单接口**。

## 行情

### `GET /api/market_snapshot`

| 参数 | 说明 |
|---|---|
| `mode` | `stock`（个股）或 `snapshot`（全市场） |
| `symbol` | `600519.SH` |
| `limit` | 1–200，默认 100 |
| `date` | 可选，`YYYYMMDD` |
| `industry` | 可选，行业代码 |

`mode=stock` 返回：

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

可视化兼容接口；真实计算不可用时返回 `503`，前端有降级路径。

## 错误示例

```json
{"available": false, "reason": "尚未生成（运行 chan_multi_engine.py --combo D-W-M）"}
```

前端会把 `reason` 直接显示在对应卡片里。新增接口请保持这一约定。
