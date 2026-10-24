# Agent installation guide

Goal: start the **local QuantWeave demo** on the user's computer, verify it and deliver a working URL. The demo uses frozen snapshots and simulated jobs. It requires no GPU, model weights, market-data account or API key.

This procedure does not provision live data downloads, model training or brokerage trading. Requirements for full research are listed in [Live research environment](README.md#live-research-environment).

## 1. Inspect the environment and destination

1. Use the available terminal to identify the operating system and check Git and Python. Python **3.11 or newer** is required. Typically use `python3` on macOS/Linux and `py -3` on Windows.
2. Work in the user's chosen directory. Clone if the checkout is absent. If this repository already exists, inspect `git status --short`, reuse it and preserve local changes.
3. If an unrelated folder has the same name, choose a new empty destination. If Git or Python is missing, report the missing prerequisite and provide installation steps for that operating system.

For a new checkout:

```bash
git clone https://github.com/DWJLULALA/QuantWeave.git
cd QuantWeave
```

## 2. Install into an isolated environment

macOS / Linux:

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python -m unittest test_demo_server -v
```

Windows PowerShell:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
.\.venv\Scripts\python.exe -m unittest test_demo_server -v
```

If `.venv` exists, check its interpreter and version before reusing it. Invoke that interpreter directly; changing global Python or PowerShell execution policy is unnecessary. Keep the complete error if installation fails, then address the specific dependency or network problem.

## 3. Start the local service

Run this in a terminal that can remain open:

```bash
.venv/bin/python demo_server.py --host 127.0.0.1 --port 7072
```

On Windows:

```powershell
.\.venv\Scripts\python.exe demo_server.py --host 127.0.0.1 --port 7072
```

Keep the terminal running. If port 7072 is occupied, first identify whether the listener is already QuantWeave. Otherwise use a free port such as 7073, and use that same port in verification and handoff. Do not terminate unrelated processes.

## 4. Verify acceptance criteria

In another terminal, run the following with the virtual environment's Python. Update the URL if the port changed.

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

If browser tools are available, open the URL and check the page and its frozen-demo badge, `DEMO · 冻结快照`. Acceptance does not require entering positions, resetting the account or repeatedly submitting simulated jobs.

Return the URL, test outcome, service terminal or process, stop instructions (Ctrl+C in the foreground terminal) and the restart command. If verification fails, identify the failing step. A command having started is not evidence of a completed installation.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No module named flask` | Use the same `.venv` interpreter for pip and the server |
| `ZoneInfoNotFoundError: Asia/Shanghai` | Windows dependencies include `tzdata`; install it in the virtual environment on minimal Linux systems without a timezone database |
| `Address already in use` | Identify the existing service or choose another free port; do not kill it blindly |
| Dates are not current | Expected: the demo reads frozen snapshots |
| No real training or live updates | This procedure installs the demo; full research needs upstream scripts, data, weights and provider authorization |

## Copyable installation prompt

> Install and start the local demo from https://github.com/DWJLULALA/QuantWeave on my computer. Read README_AGENT.md first. Check Python and the destination, use an isolated virtual environment, run the demo tests, and verify the homepage, /healthz and /api/status. Return the working URL and stop/restart instructions. Preserve existing files and services.
