# 给智能体的安装 README

目标：在用户的电脑上启动 QuantWeave **本地演示**，检查服务后交付可打开的地址。演示使用冻结快照和模拟任务，不需要 GPU、模型权重、行情账号或 API key。

这份流程不会启动实时行情下载、模型训练或实盘交易。真实研究环境的接入条件见 [README](README.md#真实研究环境)。

## 1. 检查环境与目录

1. 使用已有终端工具确认操作系统、Git、Python 版本。需要 Python **3.11 或更高**；macOS/Linux 通常用 `python3`，Windows 用 `py -3`。
2. 在用户指定的工作目录操作。目录不存在时克隆；已经有本仓库时检查 `git status --short` 并使用现有目录，保留本地修改。
3. 若存在同名但无关的文件夹，选择新的空目录，不覆盖它。若 Python/Git 缺失，报告缺少项并提供对应系统的安装办法。

首次克隆：

```bash
git clone https://github.com/DWJLULALA/QuantWeave.git
cd QuantWeave
```

## 2. 创建隔离环境并安装

macOS / Linux：

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python -m unittest test_demo_server -v
```

Windows PowerShell：

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
.\.venv\Scripts\python.exe -m unittest test_demo_server -v
```

若已有 `.venv`，先检查其中的 Python 是否可用且版本合适，正常则复用。直接调用虚拟环境解释器，无需改变全局 Python 或 PowerShell 执行策略。安装失败时保留完整错误，先处理具体依赖或网络问题。

## 3. 启动本地服务

在一个可以持续运行的终端中执行：

```bash
.venv/bin/python demo_server.py --host 127.0.0.1 --port 7072
```

Windows 对应：

```powershell
.\.venv\Scripts\python.exe demo_server.py --host 127.0.0.1 --port 7072
```

保持该终端运行。若 7072 已占用，先检查占用服务是否就是 QuantWeave；若不是，改用空闲端口，例如 7073，并在后续验证和交付中使用同一端口。不要终止不属于本次安装的进程。

## 4. 验证完成条件

在另一个终端，用当前系统的虚拟环境 Python 执行（换端口时同步修改 URL）：

```python
import json
from urllib.request import urlopen

base = "http://127.0.0.1:7072"
assert urlopen(base + "/", timeout=10).status == 200
health = json.load(urlopen(base + "/healthz", timeout=10))
status = json.load(urlopen(base + "/api/status", timeout=10))
assert health["ok"] is True and health["demo"] is True
assert status["demo"] is True
print("QuantWeave demo ready:", base)
```

有浏览器工具时打开地址，确认页面和 `DEMO · 冻结快照` 标记。安装验收无需录入持仓、重置账户或反复提交模拟任务。

完成时向用户返回：访问地址、测试是否通过、服务所在终端或进程、停止方式（前台终端按 Ctrl+C）以及再次启动的命令。未通过验证时，明确失败步骤，不能把“命令已启动”当作安装完成。

## 常见问题

| 现象 | 处理 |
|---|---|
| `No module named flask` | 用 `.venv` 内同一个 Python 运行 pip 和服务 |
| `ZoneInfoNotFoundError: Asia/Shanghai` | Windows 安装依赖时会带入 `tzdata`；精简 Linux 缺时区库时，在虚拟环境安装 `tzdata` |
| `Address already in use` | 核对原服务，或换空闲端口；不要直接杀进程 |
| 页面日期不是今天 | 演示读取冻结快照，属于正常行为 |
| 没有真实模型训练或行情更新 | 本流程仅安装 demo；完整研究环境需要上游脚本、数据、权重及行情授权 |

## 用户可复制的安装提示词

> 请在我的电脑上安装并启动 https://github.com/DWJLULALA/QuantWeave 的本地 demo。先阅读仓库 README_AGENT.md，检查 Python 和目录，使用独立虚拟环境，运行演示测试，验证首页、/healthz 和 /api/status，最后给我访问地址及停止、重启方法。保留已有文件和服务。
