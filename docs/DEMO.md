# 演示模式

演示模式的目标只有一个：**让任何人在没有行情账号、GPU 和私有数据的情况下，看到完整产品。**

```bash
pip install -r requirements-demo.txt
python demo_server.py --port 7072
# http://127.0.0.1:7072
```

右上角出现 `DEMO · 冻结快照` 即表示演示模式生效。

## 它是怎么工作的

`demo_server.py` 复用真实的 `templates/` 与 `static/`，不维护第二套前端。它替换的只是数据源：

| 真实模式 | 演示模式 |
|---|---|
| 读取 `data/`、`runs/` 下的快照 | 读取 `demo/snapshots/*.json` |
| 子进程执行真实脚本 | 内存线程模拟阶段与进度（约 6 秒） |
| 纸上账户写入 JSON 文件 | 纸上账户保存在内存，重启即恢复 |
| 个股 K 线来自行情面板 | 由代码哈希合成可复现的 OHLC 序列 |
| 趋势详情读取逐代码资产 | 读取 `demo/snapshots/trends_detail/` |

任务仍然走同一套 API：`POST /api/jobs` → 轮询 `GET /api/runs/<id>` → 完成后前端回到 `latest` 快照。因此演示模式能真实展示进度条、日志尾部、完成态和产物。

## 快照清单

| 文件 | 内容 | 约 |
|---|---|---|
| `status.json` | 能力、数据日期、分时覆盖 | 6 KB |
| `config.json` | 面板默认参数 | 0.6 KB |
| `factors_latest.json` | 因子回测结果 | 29 KB |
| `composite_latest.json` | 组合因子 TopK / 逐日摘要 | 64 KB |
| `intraday_latest.json` | 分时预测与路径 | 344 KB |
| `intraday_analytics.json` | 分时汇总 | 153 KB |
| `intraday_industry_ranking.json` | 行业相对强弱 | 76 KB |
| `intraday_market_curve.json` | 市场曲线 | 6 KB |
| `intraday_ic_trend.json` | IC 历史（裁剪） | 100 KB |
| `market_volume_history.json` | 成交历史 / 涨停 / 连板 | 103 KB |
| `market_wave.json` | 市场浪型（最后 900 日） | 117 KB |
| `chan_phase.json` | 缠论阶段 | 52 KB |
| `chan_multi_*.json` | 三组合结构（各最后 900 日） | 约 700 KB × 3 |
| `chan_multi_eval_*.json` | walk-forward 评估 | 112–159 KB |
| `chan_classic_cases.json` | 经典案例逐 bar 切片 | 282 KB |
| `trends.json` | 大盘 + 31 个申万一级行业 | 813 KB |
| `trends_detail/` | 34 代码 × D/W/M，共 102 个文件 | 11 MB |
| `champion_workbook.json` | 冠军工作簿（个股裁剪到 Top 320） | 397 KB |
| `runs.json` | 7 条合成运行记录 | 4.5 KB |
| `artifacts.json` | 8 个演示产物索引 | 1.3 KB |

另有 `demo/artifacts/` 下的 CSV / JSON / 日志样例，用于产物下载。

## 裁剪与脱敏

原始快照远大于仓库可接受的大小，生成时做了三类处理：

1. **时间裁剪**：趋势详情按级别保留最后 420 / 260 / 120 根 bar；市场浪型与多级别结构保留最后 900 日；
2. **行数裁剪**：冠军工作簿只保留排名前 320 的股票；IC 历史只保留 260 只；经典案例保留事件窗口；
3. **字段脱敏**：递归删除绝对路径、用户名、主机名、内网地址与运行环境信息，所有路径统一为 `/opt/quantweave` 与 `/opt/kronos`。

详细规则见 [PRIVACY.md](PRIVACY.md)。

## 演示模式的边界

- 数据是冻结的，不会随日期推进；页面上的「最新」指快照生成日（2026-10-22 / 23）；
- 个股 K 线为合成序列，只用于展示交互，不代表真实行情；
- 纸上账户的持仓与盈亏是演示数据，不是任何人的真实账户；
- 任务不会真的执行计算，只模拟阶段；
- 部分真实模式才有的能力（如按任意日期重跑）在演示模式返回固定的成功结果。

这些边界不影响评审代码与交互，但**不能用于任何交易决策**。

## 重新生成快照

仓库不包含抓取脚本，因为不同研究环境的数据接口不同。通用步骤：

1. 在真实模式运行 `app.py`，确认 `/api/status` 中能力可用；
2. 用只读方式请求各 `GET /api/*`，保存为同名 JSON；
3. 按上文规则裁剪长序列与大表；
4. 递归替换私有路径与身份信息；
5. 用 `python -m json.tool` 校验每个文件，`test_demo_server.py` 跑通后再提交。

演示层测试会检查关键文件存在、字段可读、Top 320 裁剪生效、产物可下载、任务生命周期与 404 行为。
