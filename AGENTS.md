# QuantWeave agent entry points

- Installation or “run this project”: follow [README_AGENT.md](README_AGENT.md). The default target is the frozen local demo.
- Product and research scope: [README.md](README.md).
- Model semantics: [docs/MODELS.md](docs/MODELS.md). Research claims and their limits: [docs/RESEARCH_EVIDENCE.md](docs/RESEARCH_EVIDENCE.md).
- Data connections: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). Live research requires external assets; demo setup does not provision those assets.
- Demo changes: run `python -m unittest test_demo_server -v` with the virtual environment interpreter. Algorithm changes need the relevant algorithm tests and dependencies.
- Public files must use portable paths and sanitized demo content. Keep credentials, private research assets, real accounts, raw recordings and generated media intermediates outside this repository.
