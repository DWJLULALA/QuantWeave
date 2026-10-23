# 多级别缠论阶段引擎（5F–30F–D / 30F–D–W / D–W–M）

> 2026-10-18 上线。目标是把原来的单级别、收盘价简化版 `chan_phase_engine.py` 升级为
> **OHLC、三级别、逐 bar 因果**的结构引擎，并在 10 年样本外检验“上涨 / 下跌 / 盘整”三分类的
> balanced accuracy。

## 1. 代码与入口

| 文件 | 作用 |
|---|---|
| `chan_core.py` | 单级别因果引擎：包含处理 → 分型 → 笔 → 中枢 → 背驰 → 逐 bar 状态 |
| `chan_multi_engine.py` | 三级别拼装、标签、walk-forward、balanced accuracy、产物写出 |
| `build_market_30f.py` | 用 2000+ 只 A 股的 30 分钟面板合成等权市场 30F 序列（含宽度） |
| `build_market_5f.py` | 用 HS300 前 30 大权重股合成 5F 篮子（Baostock，前复权），并重采样 30F |
| `app.py` | `POST /api/jobs` 的 `chan_multi`；`GET /api/chan_multi`；`GET /api/chan_multi/eval` |
| `static/app.js` | 因子页“多级别缠论阶段”面板 |

运行：

```bash
# 30F 全市场（一次性/数据更新后）
python3 build_market_30f.py

# 5F 篮子（首次约 30 分钟；断点续传）
python3 build_market_5f.py --top 30 --start-year 2020

# 三组合全量验证（或 --combo 5F-30F-D / 30F-D-W / D-W-M）
python3 chan_multi_engine.py --combo all
```

产物在 `data/chan_multi/`：

```
bars/                  market_30f.parquet, market_5f.parquet, 000300_SH_daily.parquet
levels/                D/W/M/5F/30F 的逐 bar 状态缓存（fingerprint 含 ENGINE_VERSION）
combos/<combo>/        daily.csv, eval.json, latest.json, structures.json
summary.json
```

## 2. 数据口径

| 级别 | 来源 | 覆盖 | 说明 |
|---|---|---|---|
| 5F | HS300 前 30 大权重股篮子（2020-06-30 权重固定），Baostock 5 分钟前复权 | 2020-01 → 至今 | 权重股加权收益合成；不是 510300 逐笔 |
| 30F（5F–30F–D） | 同一 5F 篮子重采样 30 分钟 | 2020-01 → 至今 | 与 5F 同源，避免跨品种拼接 |
| 30F（30F–D–W） | `astock_30min_2000_panel` 2000+ 只 A 股等权合成 | 2000-06 → 2026-09-16 | 市场宽度序列；含上涨家数、5 根上涨占比、20 均线上方占比、截面离散度 |
| D/W/M | 沪深 300 指数日线（Tushare `index_daily`） | 2005-01 → 至今 | W/M 由日线按自然周/月合成，时间戳为周期最后一个交易日 15:00 |

**为什么 5F 不是 510300 指数 5 分钟**：Tushare `stk_mins` 当前 token 限制为约 1 次/小时，
无法在当天补齐 2016–2026 的 5 分钟数据；东财/新浪/腾讯只给近期或约 1488 根。
Baostock 的指数/ETF 分钟线为空，但股票 5 分钟从 2020 年起可用，因此用固定 HS300 高权重
篮子合成市场 5F 代理。该限制在页面“数据来源与覆盖”里明确标注。

## 3. 结构规则（因果）

1. **包含处理**：后一根与当前合并 bar 存在包含关系时，按当前方向取高高/低高（向上）或
   低低/高低（向下）；合并 bar 只有在出现下一根非包含 bar 时才“收盘”。
2. **分型**：三根已收盘合并 bar 的顶/底分型。分型在右邻合并 bar 收盘的那根源 bar 才确认，
   绝不提前使用未来 bar。
3. **笔**：顶底交替；两个分型中心至少间隔 `min_gap=4` 根合并 bar。同类分型更极端时只替换
   **最后一笔**端点；已写出的历史 bar 状态不重写（`test_chan_core` 前缀不变性测试）。
4. **中枢**：连续三笔重叠区间 `ZD=max(低点)`、`ZG=min(高点)`；后续笔与 `[ZD,ZG]` 重叠则延伸，
   连续两笔离开后结束。ZG/ZD 由前三笔固定；活动中枢允许在末笔端点替换时更新，但历史快照冻结。
5. **阶段**：`close>ZG` 为上涨（1），`close<ZD` 为下跌（-1），区间内为盘整（0）。
   `trend_score` 连续值 = 0.45×中枢位置 + 0.25×中枢斜率 + 0.30×末笔方向。
6. **背驰**：相邻同向笔的 DIF 极值比较；顶背驰 -1、底背驰 +1。
7. **第三类买卖点**：突破 ZG/ZD 后回抽不进入中枢的简化标记。

## 4. 三级别特征

每个级别输出：`phase`、`trend_score`、`pivot_pos`、`pivot_width_pct`、`pivot_slope`、
`pivot_zd_slope`、`pivot_width_rank`、`stroke_accel`、`dist_zg_pct`、`dist_zd_pct`、
`pivot_age`、`pivot_strokes`、`last_dir`、`last_stroke_ret`、`div`、`third_buy`、
`third_sell`、`phase_run`、`bars_since_fractal`、`has_pivot`、`breakout_strength`。

拼装特征：

- `align_score = 0.25×低级 trend_score + 0.35×中级 trend_score + 0.40×高级 trend_score`
- `align_phase`、`consensus`、`all_up/all_down`、`phase_gap`、`score_gap`
- `div_sum`、`breakout_higher`
- 市场宽度（辅助，不属于缠论级别）：`breadth_up`、`breadth_5`、`breadth_ma20`、
  `dispersion` 及其 5 日变化
- 价格控制变量：1/5/20/60 日收益、20/60 日波动、MA5/20/60 距离、RSI14、MACD 柱、
  ATR14、20 日区间位置、成交量比

## 5. 标签与验证

**主标签（path，5 日）**：

```
fwd_ret = close[t+5]/close[t] - 1
fwd_max = max(close[t+1..t+5])/close[t] - 1
fwd_min = min(close[t+1..t+5])/close[t] - 1
上涨  if fwd_ret >= +1.5% 且 fwd_min >= -2.5%
下跌  if fwd_ret <= -1.5% 且 fwd_max <= +2.5%
其余  盘整
```

路径型标签把“冲高回落”归入盘整，和缠论“趋势 vs 中枢”的语义一致。端点型标签保留为对照
（`endpoint_comparison`）。

**Walk-forward**：测试年份 Y 的模型只用 `label_end < Y-01-01` 的样本训练；每年重训。
不存在未来标签或未来 bar 进入训练集（`test_chan_multi_engine` 有对应测试）。

**模型**：

| 名称 | 说明 |
|---|---|
| `hgb_logit` | HistGradientBoosting，balanced 样本权重（主模型） |
| `hier_logit` | 两阶段：趋势/盘整 → 方向；趋势阈值在训练尾 20% 校准 |
| `combined_logit` | 多项 Logistic（缠论+宽度+价格） |
| `chan_logit` | 只用缠论特征 |
| `price_logit` | 只用宽度+价格特征 |
| `rule` | 三级别阶段共振规则（无训练） |
| `momentum` | 20 日动量 + 1% 死区 |
| `single_chan` | 仅 D 级别 phase |

**指标**：balanced accuracy = 三类召回率宏平均；附混淆矩阵、逐年 BA、按月的 block bootstrap
95% CI、相对规则的 ΔBA 的 95% CI。

## 6. 结果（2026-10-18 实跑）

| 组合 | OOS | n | HGB BA | 分层 BA | 多因子Logit BA | 规则 BA | 单级别 BA |
|---|---|---|---|---|---|---|---|
| 5F–30F–D | 2022-01 → 2026-10 | 1152 | 0.364 | 0.380 | 0.382 | 0.338 | 0.350 |
| 30F–D–W | 2016-01 → 2026-09 | 2597 | 0.384 | 0.349 | 0.338 | 0.333 | 0.323 |
| D–W–M | 2016-01 → 2026-10 | 2613 | 0.361 | 0.323 | 0.318 | 0.337 | 0.326 |

- **30F–D–W 是最强的 10 年结果**：HGB 0.384，相对规则 +0.052，95% CI **[0.016, 0.088]**（显著）。
- 5F–30F–D：HGB 0.364，但分层/多因子 Logit 0.380–0.382；OOS 只有 4.8 年（5F 数据 2020 起），
  训练窗仅 2020–2021，波动大；ΔBA 95% CI 跨 0。
- D–W–M：HGB 0.361，相对规则 +0.024，95% CI **[-0.009, 0.064]**（不显著）。
- 路径标签优于端点标签：30F–D–W 端点 BA 0.351 vs 路径 0.384（+3.3pp）。
- 结构阶段本身高度持续：未来 3 日 D 阶段用“当前阶段”持久化预测的 BA ≈ 0.81；HGB 0.72，
  **没有超过持久化基线**。因此这套模型的价值在“三分类的边际改善 + 中枢/趋势可解释状态”，
  而不是替代简单趋势跟随。

**结论**：多级别缠论 + 市场宽度 + HGB 在 10 年样本外把三分类 BA 从约 0.33 提到 0.38，
提升真实但幅度有限；方向预测仍是弱信号。建议把它作为**风险/阶段覆盖层**（例如下跌阶段减仓、
盘整阶段降低暴露），而不是单独 alpha。

## 7. 限制

1. 5F 数据从 2020 起；要满足“5F 十年”，需更换有 2016+ 分钟历史的 Tushare token 或付费源。
2. 30F 全市场面板截至 2026-09-16；30F–D–W 的最新状态为 2026-09-16（D 2026-09-16、W 2026-09-11），页面会标注滞后。
3. 30F 是 2000+ 只股票的等权合成市场序列，不是 HS300 指数本身；5F 是 30 只高权重股篮子。
4. 标签重叠（每日采样、5 日前瞻），bootstrap 按月分块只能部分处理自相关。
5. 未计交易成本、滑点、涨跌停；BA 不是可交易收益。
6. 规则/阈值/模型选择中，主口径（H=5、θ=1.5%、path、HGB）是预先声明的；网格结果仅作诊断。

## 8. 前端与 API

- 页面：因子与 EMA → “多级别缠论阶段”面板；组合选择、运行验证、KPI、三级状态、混淆矩阵、
  逐年 BA、H×θ 网格、数据来源/滞后。
- API：
  - `GET /api/chan_multi?combo=5F-30F-D|30F-D-W|D-W-M`
  - `GET /api/chan_multi/eval?combo=...`
  - `POST /api/jobs {"kind":"chan_multi","params":{"combo":"..."}}`
- 改 `static/app.js`/`templates/index.html` 必须 bump `?v=`；本次为 `20261018-chan-multi3`。
