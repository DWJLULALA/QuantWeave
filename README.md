<div align="center">
<img src="docs/media/logo.png" width="100" alt="QuantWeave logo">

# QuantWeave

**先看市场阶段，再看选股排名。**

面向 A 股的开源研究工作台：把升浪与缠论结构、Kronos 因子、SET10、数据更新和可追溯回放接到一起。

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-22d3ee)](LICENSE)
[![Demo](https://img.shields.io/badge/demo-zero--data-34d399)](README_AGENT.md)

[观看宣传片](docs/media/quantweave-promo.mp4) · [29 秒竖屏版](docs/media/quantweave-douyin.mp4) · [让 AI 帮你安装](README_AGENT.md) · [研究证据与边界](docs/RESEARCH_EVIDENCE.md)

[![QuantWeave 宣传片](docs/media/poster.png)](docs/media/quantweave-promo.mp4)
</div>

## 股票排得很准，账户为什么还在亏？

因为排第一，也可能只是跌得最少。RankIC 评价相对排序；市场方向、交易成本与能否成交，要另外判断。

QuantWeave 按四个问题组织研究：

1. **现在是什么阶段？** 从大盘、行业到个股，结合升浪倾向、缠论结构和多时间级别观察上涨、下跌与盘整。顺势环境可能有利于多头，但结构判定不是收益保证。
2. **谁更有相对优势？** Kronos 做量价时序预测，研究近期 LoRA 适配、历史回放与 EMA 平滑，再比较横截面排名。训练窗口和标签截止日期必须与测试分开。
3. **行情换挡怎么办？** SET10 用十个量价因子等权合成，补充反转、震荡风格。研究样本中的互补不是永久对冲。
4. **判断能不能复查？** 从数据源、参数、逐 bar 回放到模拟账户、日志和 CSV 产物，保留可检查的过程。

恢复的五日研究记录（2026-08-24 至 08-28）中，同评估协议下 base 平均 RankIC 为 **0.0021**，rp10 全速微调方案为 **0.0809**。这是笔记转录的短窗口比较，未从原始预测重新计算，也不能分离 LoRA 与 EMA 的独立贡献。详细训练设计、反面证据和 SET10 选择偏差见 [研究证据](docs/RESEARCH_EVIDENCE.md)。

## 把安装交给你的 AI

复制下面这段给能操作本机终端的智能体：

> 请安装并启动 https://github.com/DWJLULALA/QuantWeave 的本地 demo。先阅读 README_AGENT.md，检查环境，创建独立虚拟环境，运行演示测试，验证服务后给我访问地址以及停止、重启方法。保留已有文件和服务。

[README_AGENT.md](README_AGENT.md) 是完整安装与验收流程；[AGENTS.md](AGENTS.md) 提供智能体入口。**本地 demo 无需 GPU、行情账号或模型权重**，使用冻结快照、模拟更新任务和纸上账户。

手动安装（macOS / Linux，Python 3.11+）：

```bash
git clone https://github.com/DWJLULALA/QuantWeave.git
cd QuantWeave
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python demo_server.py --host 127.0.0.1 --port 7072
```

打开 <http://127.0.0.1:7072>，确认 `DEMO · 冻结快照` 标记。Windows 命令、端口冲突处理和健康检查见 [安装 README](README_AGENT.md)。

也可构建容器：

```bash
docker build -t quantweave-demo .
docker run --rm -p 127.0.0.1:7072:7072 quantweave-demo
```

## 从市场结构到因子验证

<img src="docs/media/trends_industry.png" alt="行业排名与多级别趋势结构" width="100%">

升浪观察趋势是否推进：高低点、均线、动量和趋势带提供不同证据。缠论从包含处理、分型、笔、线段到重叠中枢组织价格结构；日、周、月对照用于检查时间尺度。趋势分是固定展示规则，P 是规则倾向值，**不是校准后的上涨概率**。已确认结构与延伸候选分开显示，事后浪段不进入趋势排名。

<img src="docs/media/factors.png" alt="因子与 EMA 参数、回测和结果" width="100%">

因子实验可以选择 Kronos 来源、SET10 权重、EMA、TopK、退出排名、交易成本与阶段门控。不同模式必须区分：

| 模式 | 实际做什么 |
|---|---|
| 日线 / 分时 Kronos + SET10 | 各自标准化与 EMA，再按权重合成 |
| 纯 E 组合回放 | E1/E3/E5/E10 截面分位等权；不加入 SET10，不再做日间 EMA |
| 自定义 E 组合展示 | 选择 E1–E10 期限与权重，查看排名和稳定性提示 |

回放使用收盘信号、下一交易日开盘成交，记录费用、现金、股数与实际暴露，并考虑涨跌停、停牌和缺价。门控影响持仓资格；排序能力与账户收益分别检查。实现细节见 [模型口径](docs/MODELS.md)。

## 数据与日常工作流

| 能力 | 用途 |
|---|---|
| 数据更新 | 日线、30 分钟线分开检查；只补已收盘数据，查看缺口和失败记录 |
| 经典案例 | 拖动时间轴，按当时已知信息逐 bar 回放 |
| 日模型 | 查看预测期限、排名、候选及 `NO_TRADE` 状态 |
| 分时模型 | 查看预测路径、EMA 历史、行业相对强度和提醒 |
| 纸上账户 | 模拟录入与卖出，核对现金、持仓和盈亏；没有实盘下单接口 |
| 历史行情 | 涨停、连板、成交热度与行业对照 |
| 日志与产物 | 复查参数、任务状态、退出码、日志以及 CSV / JSON |

研究环境的日线链路使用 **Tushare**，默认个股 30 分钟更新使用**东方财富**；宽基指数日线与申万行业日线分别接入上交所 CSIP、申万宏源研究接口。其他补缺脚本不等于自动全源切换。详细入口与边界见 [数据来源](docs/DATA_SOURCES.md)。

<img src="docs/media/runs.png" alt="运行日志与可下载产物" width="100%">

## 真实研究环境

公开仓库提供工作台代码和可直接运行的 demo。完整研究还需自备行情授权、日线/30 分钟面板、行业序列、模型权重，以及本项目调用的上游研究脚本；**安装 demo 不等于复现 LoRA 训练或实时预测**。

```bash
pip install -r requirements.txt
export KRONOS_ROOT=/path/to/Kronos
export STOCKLAB_PROJECT_ROOT="$KRONOS_ROOT"
export STOCKLAB_HOST=127.0.0.1
export STOCKLAB_PORT=7070
python app.py
```

`STOCKLAB_RUN_ROOT` 设置任务产物目录，`STOCKLAB_CONFIG` 设置本地配置文件，`STOCKLAB_*_PYTHON` 设置任务解释器。真实模式的能力探测结果应逐项检查，不能只以首页可打开作为接入成功的依据。

## 开发与文档

Flask 后端 + 原生 HTML/CSS/JS 前端，无前端打包步骤。演示层验证：

```bash
.venv/bin/python -m unittest test_demo_server -v
```

算法测试按相应模块运行，需要 `requirements.txt` 及其数据依赖。CI 示例保留在 [docs/ci.example.yml](docs/ci.example.yml)。

| 文档 | 内容 |
|---|---|
| [智能体安装](README_AGENT.md) | 环境检查、安装、启动、验收和故障处理 |
| [研究证据](docs/RESEARCH_EVIDENCE.md) | Kronos / SET10 对照、数据恢复状态、指标边界 |
| [数据来源](docs/DATA_SOURCES.md) | 日线、30 分钟线、宽基与行业数据入口 |
| [模型口径](docs/MODELS.md) | 分数、EMA、门控、成交与结构确认 |
| [架构](docs/ARCHITECTURE.md) / [API](docs/API.md) | 模块职责、任务生命周期与接口 |
| [多级别缠论](docs/CHAN_MULTI_LEVEL.md) / [严格缠论](docs/STRICT_CHAN.md) | 多级别、结构规则与数据质量 |
| [趋势分析](docs/TREND_ANALYSIS.md) | 固定趋势分、P 与相对强弱 |
| [演示快照](docs/DEMO.md) / [隐私](docs/PRIVACY.md) | 冻结数据、模拟任务与脱敏范围 |
| [宣传片脚本](docs/VIDEO_STORYBOARD.md) / [抖音文案](docs/DOUYIN_COPY.md) | 叙事、镜头、开场 hook、标题与发布文案 |
| [视频制作工具](tools/promo/README.md) | 可编辑分镜、配音、动态图解和合成命令 |

## 许可

本项目代码使用 [MIT](LICENSE)。`chan_vendor/` 来源于 [chan.py](https://github.com/Vespa314/chan.py)，版权与固定版本见 `chan_vendor/LICENSE` 和 `chan_vendor/UPSTREAM.json`。上游 [Kronos](https://github.com/shiyu-coder/Kronos) 遵循其自身许可，本仓库不附模型权重。

界面和研究结果用于研究与教学，不构成投资建议；历史表现不代表未来收益。

## English

**QuantWeave** is an open-source A-share research workbench. Assess market structure before using stock rankings, compare Kronos adaptation and SET10 factor styles, and trace each experiment through its data, parameters and artifacts.

The local demo uses frozen, sanitized snapshots and simulated jobs. No GPU, market-data account or model weights are needed. Ask your coding agent to follow [README_AGENT.md](README_AGENT.md). Live research and LoRA reproduction require additional data, weights and upstream research assets.
