# 模型与口径

本文只描述**代码里实际执行的规则**。所有收益数字都来自历史回放，不构成未来收益承诺。

## 1. 因果因子回测（`factor_evaluate.py`）

### 执行假设

```text
信号日 T 收盘（15:00）生成分数
  → T+1 开盘按目标权重成交
  → 持有 H 个交易日后，T+1+H 开盘卖出/换仓
  → 每日按收盘价估值
  → 回放末日收盘强制平仓
```

- 成交价使用**次日开盘**，不使用信号日收盘价；
- 单边成本 `fee_bps`（默认 10bp）在买入与卖出各计一次；
- 现金账本逐日记录 `cash / holdings / market_value / total_asset`；
- 只允许在 `top_k` 个席位内持有；退出条件为跌出 `exit_rank` 或持有期到期；
- 涨跌停、停牌、缺失开盘价的标的当日不可成交，顺延到下一个可成交日。

### 分数

Kronos 分数与 SET10 分数先做横截面标准化（`normal_score`，默认至少 100 个样本），再按权重合成：

```text
raw = w_kronos × z(kronos) + w_set10 × z(set10)
score = ema(raw, decay)          # Kronos / SET10 各自衰减
```

`E1…E10` 是不同预测期限的横截面排名分位；纯 E 模式忽略 Kronos 与 SET10 权重，只按所选 E 序列等权或自定义权重排序。

### 门控

```text
gate_alpha = 0            关闭门控
gate_alpha ∈ [0.5, 1)     开启
gate_score = α × 市场浪型分 + (1-α) × 原始分
```

门控可以来源为 `wave`（市场浪型）或 `chan`（缠论因果阶段）。门控只影响**是否允许开仓**，不改变排序本身。

### 指标

- `total_return`、`annualized`、`sharpe`、`max_drawdown`、`volatility`；
- `turnover`、`hit_rate`、`avg_holding_days`；
- 逐日 `equity / exposure / holdings / cash`；
- 分组（按分数分位）的次日/持有期收益与 Rank IC。

回测结果写入 `runs/<job_id>/result.json` 与 `factor_daily.csv`，前端只读快照。

## 2. 组合因子（`composite_factor.py`）

把日模型分数与分时分数对齐到同一交易日，做横截面统一 Z：

```text
z_daily  = cross_section_z(daily_score)
z_intra  = cross_section_z(intraday_score)
composite = w_daily × z_daily + w_intra × z_intra
```

输出：

- `top`：按 `composite` 排序的 TopK，含 `E1…E10` 预测、排名、稳定性标签；
- `date_summary`：逐日横截面均值/离散度/覆盖率；
- `future`：次日组合预测均值；
- `horizons`：各期限权重；
- `weights`：实际生效权重与样本数。

稳定性标签（`stable / watch`）由分数在近 N 日的排名波动决定，仅作研究提示。

## 3. 缠论结构（`chan_core.py` / `chan_multi_engine.py` / `strict_chan.py`）

### 单级别因果链

```text
原始 K 线
  → 包含处理（向上/向下合并）
  → 分型（顶/底，等待右邻合并 K 线确认）
  → 笔（分型之间满足最少 K 线数）
  → 中枢（连续三笔重叠区间）
  → 背驰（b/c 段力度比较：MACD 面积/斜率）
  → 逐 bar 状态（上涨 / 下跌 / 盘整）
```

**因果约束**：任何结构只在「右邻 K 线已收盘」后才确认；回放时不允许使用未来 bar。结构一旦确认即冻结，后续只追加，不重写历史。

### 多级别拼装

三个级别各自独立跑因果链，再按「大级别定方向、小级别定入场」拼装：

| 组合 | 用途 |
|---|---|
| `5F-30F-D` | 分时入场，30F 结构，日线方向 |
| `30F-D-W` | 波段，30F 入场，日/周方向 |
| `D-W-M` | 中长线，日线入场，周/月方向 |

每个组合输出：

- 当前三级别阶段与一致性；
- 最近中枢、笔、背驰；
- 逐日标签（上涨/下跌/盘整）；
- walk-forward 的 balanced accuracy、混淆矩阵与逐窗口结果。

walk-forward 按时间切窗，训练窗口只包含窗口起点之前的数据，验证窗口不参与调参。

### 严格递归 1 买（`strict_chan.py`）

规则版本 `1.1.0`。一个「严格递归 1 买」需要同时满足：

1. 存在 R2+ 级别中枢；
2. b/c 段出现背驰（MACD 面积或斜率）；
3. c 段创出中枢新低；
4. c 段末端出现次级别双中枢（或等价的末端结构完成）；
5. 右侧确认：底分型 + 收盘站上分型上沿；
6. 尾部突破否决：确认后若快速跌回分型下沿，事件作废。

R1 笔级信号只记为**候选**，不升级为严格 1 买。事件写入 `strict_events.py` 的追加账本，包含 `event_id / code / level / confirm_dt / invalidated / evidence`。

### 数据质量

- 只用已收盘真实 K 线；盘中未收盘 bar 不进入结构；
- 申万行业缺日按官方交易日历补齐，补源记录在 `sources`；
- 多源冲突不自动覆盖，保留 `data_quality` 与 `failures`；
- 固定起点 `2022-01-01` 的严格递归样本单独标注，不与早期数据混算。

## 4. 趋势分（`trend_analysis.py`）

```text
structure = (0.7 × phase_d + 0.3 × phase_w + 1) / 2      # phase ∈ {-1, 0, 1}
score = 100 × [0.40 × structure
             + 0.30 × p_smooth
             + 0.20 × rs20_percentile
             + 0.10 × rs60_percentile]
```

- `phase_d / phase_w`：日线/周线缠论阶段，取值 `-1/0/1`，映射到 `[0,1]`；
- `p_smooth`：七票 P 的指数平滑（`α=0.9`），取值范围 `[0,1]`；
- `rs20 / rs60`：相对沪深 300 的 20/60 日超额收益，在横截面内取分位；
- `position120`：当前价在近 120 日区间的位置，**不是概率**。

七票 P：

| 票 | 多头条件 |
|---|---|
| 道氏结构 | 高点/低点同步抬高 |
| Supertrend | 价格在趋势带之上 |
| ADX | ADX ≥ 20 且 +DI > -DI |
| Choppiness | Choppiness < 38.2（趋势市） |
| 均线排列 | MA5 > MA10 > MA20 |
| 20 日动量 | 动量为正 |
| 推动计数 | 当前推动浪未走完 |

每票 0/1，等权平均得到原始 P；再做 `p_smooth = 0.9×p_smooth_prev + 0.1×p_raw`。

**事后数据隔离**：ZigZag 浪段需要未来确认，只用于灰色复盘图层，不进入 P、不进入排名。`ranking_uses_retrospective_labels` 必须为 `false`，否则前端拒绝展示排名。

## 5. 日模型（`predict`）

- 输入：最近完整交易日的日线面板 + Kronos 模型权重；
- 输出：下一交易日目标组合、计划暴露、`NO_TRADE` 状态、候选排序；
- 风控闸门：市场状态 `risk_off`、流动性不足、数据缺口超阈值时，允许输出 0 目标与 0% 暴露；
- 结果写入 `runs/<job_id>/result.json`，前端展示为「日模型 · 下一交易日计划」。

**NO_TRADE 是正常输出，不是失败。** 它表示策略主动选择不参与，而不是没有算完。

## 6. 分时模型（`engine_intraday.py` / `intraday_timing.py`）

- 输入：最新**已收盘** 30 分钟 bar（盘中最后一根未收盘 bar 不进入）；
- 输出：每只股票的 16 bar 预测路径、EMA 历史、分位、状态、持仓提醒；
- 买入窗口：14:45–14:55（尾盘），T+1 后可卖；
- 卖出规则（先到先执行）：
  1. T+1 且 EMA 信号转负；
  2. 亏损达到 5% 止损；
  3. 持有满 8 bar；
- 行业排名与市场曲线用于相对强弱过滤，不改变单票预测路径。

分时结果只用于研究和提醒，**不保证最佳买卖点**，也不连接任何交易接口。

## 7. 评价与边界

- 所有指标都是样本内/样本外的历史回放，存在选择偏差、幸存者偏差与数据修订风险；
- 交易成本只建模显性费用，未建模冲击成本、滑点与容量；
- 缠论结构依赖参数（最少 K 线数、中枢重叠规则），不同参数会改变信号；
- 趋势分是**展示规则**，不是校准后的上涨概率；
- 任何模型都可能长期失效。请自行验证，不要直接用于实盘。
