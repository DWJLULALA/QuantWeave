<div align="center">
<img src="docs/media/logo.png" width="100" alt="QuantWeave logo">

# QuantWeave

**Market context before model rankings.**

An open-source A-share research workbench connecting wave and Chan structures, Kronos forecasts, SET10 factors, data updates and traceable replay.

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-22d3ee)](LICENSE)
[![Demo](https://img.shields.io/badge/demo-zero--data-34d399)](README_AGENT.md)

[Product film (Mandarin)](docs/media/quantweave-promo.mp4) · [Portrait cut (Mandarin)](docs/media/quantweave-douyin.mp4) · [Install with your AI agent](README_AGENT.md) · [Research evidence](docs/RESEARCH_EVIDENCE.md)

[![QuantWeave: a top model rank is a prediction, not an observed outcome](docs/media/poster.png)](docs/media/quantweave-promo.mp4)
</div>

## The model ranks it first. Is that a buy signal?

In this workflow, model forecasts become predictions of relative stock strength. **The predicted ranking can be wrong.** RankIC measures the rank correlation between prediction scores and realized returns; it is neither a percentage accuracy score nor a portfolio return. Market direction, costs and execution require separate evaluation.

QuantWeave organizes research around four questions:

1. **What phase is the market in?** Examine the broad market, industries and individual stocks across timeframes. Wave tendencies and Chan structures describe advancing, declining and consolidating conditions.
2. **What relative strength does the model predict?** Convert Kronos price-volume forecasts into cross-sectional scores. Study recent-window LoRA adaptation, historical replay and EMA smoothing, with training labels ending before evaluation begins.
3. **What happens when market conditions change?** Compare the adapted model with SET10, an equal-weight blend of ten price-volume factors with reversal and range-trading characteristics.
4. **Can the result be inspected?** Follow data sources, parameters and chronological replay through paper positions, job logs and downloadable CSV/JSON artifacts.

Recovered research notes report five-day mean RankIC of **0.0021** for base and **0.0809** for the rp10 full-speed fine-tuned variant under the same evaluation protocol, for August 24–28, 2026. These are transcribed research results, not recomputed predictions or isolated LoRA/EMA ablations. See [research evidence](docs/RESEARCH_EVIDENCE.md) for the protocol, recovery status and complementary SET10 results.

## Let your AI agent install it

Give this prompt to an agent with access to your local terminal:

> Install and start the local demo from https://github.com/DWJLULALA/QuantWeave. Read README_AGENT.md first, check the environment, use an isolated virtual environment, run the demo tests and verify the service. Return the working URL and stop/restart instructions. Preserve existing files and services.

[README_AGENT.md](README_AGENT.md) contains the installation and acceptance procedure; [AGENTS.md](AGENTS.md) provides agent entry points. **The local demo needs no GPU, market-data account or model weights.** It uses frozen snapshots, simulated update jobs and a paper account. The current application interface and videos are in Chinese; repository documentation is in English.

Manual setup on macOS / Linux, with Python 3.11+:

```bash
git clone https://github.com/DWJLULALA/QuantWeave.git
cd QuantWeave
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python demo_server.py --host 127.0.0.1 --port 7072
```

Open <http://127.0.0.1:7072> and check the frozen-demo badge, `DEMO · 冻结快照`. See the [agent installation guide](README_AGENT.md) for Windows, port conflicts and health checks.

Alternatively, build a container:

```bash
docker build -t quantweave-demo .
docker run --rm -p 127.0.0.1:7072:7072 quantweave-demo
```

## From market structure to factor evaluation

<img src="docs/media/trends_industry.png" alt="Industry rankings and market structure across timeframes; Chinese interface" width="100%">

Wave analysis checks whether highs, lows, moving averages, momentum and trend bands support continued movement. Chan analysis organizes bars into fractals, strokes, segments and overlapping central zones. Daily, weekly and monthly views expose differences across timeframes. The trend score is a fixed display rule; P is a rule-based tendency score, **not a calibrated probability of a price increase**. Confirmed structures are separated from developing candidates, and retrospective wave labels do not enter trend rankings.

<img src="docs/media/factors.png" alt="Factor parameters, EMA controls and evaluation results; Chinese interface" width="100%">

Factor experiments expose Kronos sources, SET10 weights, EMA, TopK, exit ranks, costs and phase gates. The modes have different semantics:

| Mode | What it computes |
|---|---|
| Daily / intraday Kronos + SET10 | Normalize and smooth each component with EMA, then blend by weight |
| Pure E composite replay | Equal-weight cross-sectional percentiles for E1/E3/E5/E10; no SET10 or additional daily EMA |
| Custom E composite display | Select E1–E10 horizons and weights, then inspect rankings and stability indicators |

Replay forms signals at the close and executes at a subsequent tradable open. It tracks fees, cash, shares and realized exposure, including price-limit, suspension and missing-price constraints. Gates control position eligibility. Evaluate ranking quality and portfolio outcomes separately. See [model semantics](docs/MODELS.md).

## Data and daily workflow

| Capability | Purpose |
|---|---|
| Data updates | Check daily and 30-minute panels separately; ingest closed bars and inspect gaps or failures |
| Classic cases | Move through history bar by bar using information available at each point |
| Daily model | Inspect forecast horizons, predicted rankings, candidates and `NO_TRADE` states |
| Intraday model | Inspect forecast paths, EMA history, industry strength and position reminders |
| Paper account | Record simulated entries and exits; inspect cash, holdings and P&L without a broker connection |
| Historical market views | Compare price limits, consecutive limit-up events, turnover and industries |
| Logs and artifacts | Inspect parameters, job status, exit codes, logs and CSV/JSON outputs |

The live research pipeline uses **Tushare** for daily stock data and **East Money** for default 30-minute stock updates. Broad-market daily indices and Shenwan industry bars use SSE CSIP and SWS Research interfaces respectively. Separate repair scripts do not imply automatic failover across all providers. See [data sources](docs/DATA_SOURCES.md).

<img src="docs/media/runs.png" alt="Job history, execution logs and downloadable artifacts; Chinese interface" width="100%">

## Live research environment

This repository includes the workbench and a runnable demo. Full research also requires licensed market data, daily/30-minute panels, industry series, model weights and the upstream research scripts called by the application. **Installing the demo does not reproduce LoRA training or live predictions.**

```bash
pip install -r requirements.txt
export KRONOS_ROOT=/path/to/Kronos
export STOCKLAB_PROJECT_ROOT="$KRONOS_ROOT"
export STOCKLAB_HOST=127.0.0.1
export STOCKLAB_PORT=7070
python app.py
```

Set `STOCKLAB_RUN_ROOT` for job artifacts, `STOCKLAB_CONFIG` for local configuration and `STOCKLAB_*_PYTHON` for task interpreters. Check each reported capability when connecting a live environment; a working homepage alone does not establish that data and models are available.

## Development and documentation

Flask backend, plain HTML/CSS/JavaScript frontend, no frontend build step. Validate the demo with:

```bash
.venv/bin/python -m unittest test_demo_server -v
```

Algorithm tests require the relevant modules, `requirements.txt` and their data dependencies. A CI example is available in [docs/ci.example.yml](docs/ci.example.yml).

| Document | Contents |
|---|---|
| [Agent installation](README_AGENT.md) | Environment checks, setup, launch, acceptance and troubleshooting |
| [Research evidence](docs/RESEARCH_EVIDENCE.md) | Kronos / SET10 comparisons, recovered sources and metric definitions |
| [Data sources](docs/DATA_SOURCES.md) | Daily, intraday, broad-market and industry data connections |
| [Model semantics](docs/MODELS.md) | Scores, EMA, gates, execution and structure confirmation |
| [Architecture](docs/ARCHITECTURE.md) / [API](docs/API.md) | Module responsibilities, job lifecycle and endpoints |
| [Multi-level Chan](docs/CHAN_MULTI_LEVEL.md) / [Strict Chan](docs/STRICT_CHAN.md) | Timeframes, recursive structures and data quality |
| [Trend analysis](docs/TREND_ANALYSIS.md) | Fixed trend scores, P and relative strength |
| [Demo snapshots](docs/DEMO.md) / [Privacy](docs/PRIVACY.md) | Frozen data, simulated jobs and sanitization |
| [Full film](docs/media/quantweave-promo.mp4) / [Portrait cut](docs/media/quantweave-douyin.mp4) | Product introduction and research workflow, in Mandarin |

## License

Project code is [MIT licensed](LICENSE). `chan_vendor/` derives from [chan.py](https://github.com/Vespa314/chan.py); attribution and the pinned revision are in `chan_vendor/LICENSE` and `chan_vendor/UPSTREAM.json`. Upstream [Kronos](https://github.com/shiyu-coder/Kronos) has its own license; model weights are not included here.

The application and research results are for research and education, not investment advice. Historical results do not establish future returns.
