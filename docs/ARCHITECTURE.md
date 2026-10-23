# 架构

QuantWeave 是一个「单进程 Flask API + 无构建前端 + 子进程任务」的研究工作台。它刻意保持小而透明：没有消息队列、没有数据库、没有前端框架，所有状态都能在文件系统里找到。

## 分层

| 层 | 位置 | 职责 |
|---|---|---|
| 表现层 | `templates/index.html`、`static/*` | 原生 HTML/CSS/JS，哈希路由，内联 SVG 图表 |
| API 层 | `app.py` | 状态、配置、复盘、任务、只读快照、产物下载 |
| 任务层 | `app.py` 的 `job_spec` / `run_command` / `monitor_job` | 参数校验、argv 白名单、子进程监督、日志与退出码 |
| 计算层 | `factor_evaluate.py`、`chan_*.py`、`trend_analysis.py` 等 | 纯 Python 计算，读写约定目录 |
| 数据层 | `data/`、`runs/`、`config.json` | 面板、结构快照、运行记录、产物 |
| 演示层 | `demo_server.py`、`demo/` | 冻结快照 + 内存任务，复现同一前端 |

前端没有构建步骤。`templates/index.html` 直接加载 `static/app.js`，其余页面脚本按需挂载：

```text
app.js            路由、shell、任务轮询、通用图表
trend-analysis.js 趋势页（大盘双图 / 行业榜 / 结构详情）
chan-cases.js     经典案例回放
stock_views.js    个股 K 线弹窗
visuals.js        小型可视化辅助
```

## 一次请求的生命周期

### 只读快照

```text
GET /api/factors/latest
  → app.py 读取 runs/factor_*/result.json（或配置的 latest 指针）
  → 返回 {available, path, result}
  → app.js 的 resultInto() 按 result.kind 分派到 factorResult / compositeResult / predictionResult
```

只读接口不做计算。页面刷新不会触发任何重活，计算永远由显式任务产生。

### 写任务

```text
POST /api/jobs {kind, params}
  → kind 必须在 CAPABILITIES 中且当前 available
  → 参数白名单校验（日期、代码、枚举、数值范围）
  → job_spec(kind, params) 生成 argv 数组 + cwd + metadata
  → uuid12 建 runs/<id>/，写 job.json，启动线程
  → 线程用 subprocess 执行 argv（shell=False）
  → 子进程退出后写 .exit.json（原子替换）
  → monitor_job 轮询日志，解析 [stage n/m]，更新 job.json
  → 前端每 2.5s GET /api/runs/<id>，完成时刷新对应 latest
```

关键点：

- **不使用 shell**：`argv` 数组直接交给 `subprocess`，参数不经过字符串拼接；
- **白名单**：前端只能触发枚举内的任务类型，不能传任意命令；
- **原子状态**：`.exit.json` 先写临时文件再 `os.replace`，避免半个 JSON；
- **幂等复用**：`data_update` / `chan_multi` / `trend_refresh` 同类任务运行中会复用已有 job，避免重复点击放大负载；
- **失败可见**：非零退出码、超时、日志尾部都会回到前端，不吞错。

## 能力探测

`capability_state()` 不假设环境完整，而是按文件存在性与配置逐项判断：

```text
data_update      更新脚本 + 数据目录
review_write     复盘文件可写
factor_evaluate  因子脚本 + 面板数据
predict          日模型脚本 + 模型目录
composite_factor 合成脚本 + 所需快照
intraday_predict 分时脚本 + 30F 面板
chan_multi       多级别脚本 + 结构数据
trend_refresh    趋势脚本 + 行业目录
```

不可用的能力不会出现在可执行按钮里；即使被直接调用，API 也返回 409/503 和原因。这样部署到新机器时，界面是「能力清单」而不是一堆报错。

## 数据契约

所有 `result.json` 共享一层外壳：

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

前端只依赖 `kind` 与少数稳定字段；新增字段向后兼容。详细字段见 [API.md](API.md)。

## 计算模块地图

| 模块 | 职责 |
|---|---|
| `update_latest.py` | 交易日历、日线增量、指数尾部、质量审计 |
| `update_intraday_rt.py` / `update_intraday_market_parallel.py` | 30 分钟 bar 增量与全市场并行补齐 |
| `factor_evaluate.py` | 因果现金账本回测、IC、分组、换手 |
| `composite_factor.py` | 日模型 × 分时分数横截面合成 |
| `chan_core.py` | 包含处理、分型、笔、中枢、背驰、逐 bar 状态 |
| `chan_multi_engine.py` | 三级别拼装、标签、walk-forward 验证 |
| `strict_chan.py` / `strict_events.py` | 严格递归 1 买、事件账本 |
| `build_market_5f.py` / `build_market_30f.py` | 市场级 5F/30F 等权与宽度序列 |
| `trend_analysis.py` | 趋势分、P、相对强弱、行业排名、快照 |
| `classic_cases.py` | 历史案例的逐 bar 可回放切片 |
| `engine_intraday.py` / `intraday_timing.py` | 分时推演、买卖窗口、持仓提醒 |
| `market_snapshot.py` | 个股 OHLC、因子历史、horizon |
| `market_volume_history.py` | 成交历史、涨停/连板聚合 |
| `market_wave_factor.py` / `market_wave_rules.py` / `market_wave_eval.py` | 市场浪型特征、规则与评估 |
| `chan_vendor/` | vendored chan.py（MIT，固定提交） |

## 演示层

`demo_server.py` 不复制前端，也不改前端契约。它只做三件事：

1. 把 `demo/snapshots/*.json` 按原路径返回；
2. 用内存线程模拟 `POST /api/jobs` 的阶段与完成状态；
3. 用内存字典模拟纸上账户的增删改。

因此演示模式看到的就是真实前端，只是数据源被替换。`test_demo_server.py` 用 Flask test client 覆盖关键路径，保证演示层不会悄悄偏离契约。

## 设计取舍

| 取舍 | 原因 |
|---|---|
| 单进程 + 线程监督，而不是 Celery/Redis | 单机研究场景，部署成本低；任务量可控；日志与退出码直接可见 |
| 文件系统即状态，而不是数据库 | 快照可 diff、可打包、可直接给同事；失败时不用修数据库 |
| 原生前端，而不是 React/Vue | 无构建、无依赖、单文件可读；图表用 SVG 足够 |
| 演示快照随仓库分发 | 评审者零门槛看到完整产品；同时强制代码与数据解耦 |
| 冻结结构而不是每次重算 | 研究结论可复现；页面加载不依赖算力 |

## 已知边界

- 单机单进程，任务并发度有限；重计算应放到独立 worker（当前用「同类任务复用 + 队列式串行」控制）。
- 快照读取是「最后写入者胜」，没有多版本并发控制；研究场景可接受。
- 前端无虚拟 DOM，超长表格（>5,000 行）依赖分页；工作簿默认裁剪到 Top 320。
- 真实模式依赖上游数据布局；不同数据源需要适配 `update_*` 与列名映射。
