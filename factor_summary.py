#!/usr/bin/env python3
"""Produce a bounded, read-only factor performance snapshot for the UI.

The full joint_strategy research commands need many large, date-aligned inputs.
This command is the safe daily default: it reads the latest shadow-live
summaries and their existing CSV ledgers, then writes a small JSON/CSV report.
It never retrains models, changes source data, or submits orders.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="signal date YYYYMMDD")
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    root = Path(os.getenv("STOCKLAB_PROJECT_ROOT", "/opt/kronos"))
    results = root / "factor_horizon" / "shadow_live" / "results"
    summaries = sorted(results.glob("*/summary.json"), key=lambda p: p.stat().st_mtime) if results.exists() else []
    latest = summaries[-1] if summaries else None
    summary = {}
    if latest:
        try:
            summary = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
    # Prefer the latest realised shadow ledger and expose a compact table.
    ledger = latest.parent / "shadow_target_next_open.csv" if latest else None
    rows: list[dict[str, str]] = []
    if ledger and ledger.exists():
        try:
            with ledger.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))[:50]
        except OSError:
            rows = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    factor_rows = [
        {"factor": "Kronos", "weight": 0.45, "ema": 0.88, "source": "shadow-live frozen contract"},
        {"factor": "SET10", "weight": 0.55, "ema": 0.88, "source": "shadow-live frozen contract"},
    ]
    # If a prior bounded/research replay has produced pooled_summary.csv, add
    # its actual mode-level metrics so the UI can plot recent performance.
    pooled_rows: list[dict[str, str]] = []
    pooled_files = sorted((root / "factor_horizon" / "results").glob("**/pooled_summary.csv"), key=lambda p: p.stat().st_mtime) if (root / "factor_horizon" / "results").exists() else []
    if pooled_files:
        try:
            with pooled_files[-1].open(newline="", encoding="utf-8") as fh:
                pooled_rows = list(csv.DictReader(fh))[:20]
        except OSError:
            pooled_rows = []
    with (args.output_dir / "factor_performance.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(factor_rows[0]))
        writer.writeheader(); writer.writerows(factor_rows)
    payload = {
        "available": bool(latest),
        "kind": "factor_snapshot",
        "requested_signal_date": args.date,
        "source_summary": str(latest) if latest else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "factor_performance": factor_rows,
        "recent_replay": pooled_rows,
        "candidate_count": len(rows),
        "candidates": rows,
        "summary": summary,
        "read_only": True,
        "note": "bounded snapshot from existing shadow-live artifacts; no full回测 or model training",
    }
    (args.output_dir / "factor_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
