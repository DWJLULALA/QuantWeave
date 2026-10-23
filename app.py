#!/usr/bin/env python3
"""QuantWeave control plane.

This is intentionally a thin UI/API layer. Expensive research jobs stay in the
existing project scripts and are launched through an allow-listed job map.
"""
from __future__ import annotations

import json
import csv
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
TEMPLATE = ROOT / "templates" / "index.html"
UPDATE_SCRIPT = ROOT / "update_data.sh"
RUN_ROOT = Path(os.getenv("STOCKLAB_RUN_ROOT", ROOT / "runs"))
REVIEW_ROOT = Path(os.getenv("STOCKLAB_REVIEW_ROOT", ROOT / "reviews"))
CONFIG_PATH = Path(os.getenv("STOCKLAB_CONFIG", ROOT / "config.json"))
SERVER_ROOT = os.getenv(
    "STOCKLAB_PROJECT_ROOT",
    os.getenv("KRONOS_ROOT", "/opt/kronos"),
)

DEFAULT_CONFIG = {
    "data_update": {
        "incremental": True,
        "daily_kline": True,
        "review_sources": ["market", "fundamental", "industry"],
    },
    "factor": {
        "kronos_source": "composite",
        "gate_mode": "up_only",
        "fee_bps": 10.0,
        "exit_rank": 1000,
        "gate_alpha": 0.65,
        "gate_source": "chan",
        "lookback_days": 10,
        "ema_kronos": 0.0,
        "ema_set10": 0.0,
        "kronos_weight": 1.0,
        "set10_weight": 0.0,
        "top_k": 100,
        "holding_days": 2,
        "benchmark": "沪深300",
    },
    "chan_multi": {
        "combo": "5F-30F-D",
        "horizon": 5,
        "theta": 0.015,
    },
    "predict": {
        "model": "rp10_full_bdecdf09",
        "signal_time": "14:30",
        "context_length": 160,
        "horizon": 16,
        "top_k": 5,
        "temperature": 0.6,
        "samples": 1,
    },
}

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
RUN_ROOT.mkdir(parents=True, exist_ok=True)
REVIEW_ROOT.mkdir(parents=True, exist_ok=True)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_snapshot_cache: dict[tuple, tuple[float, dict]] = {}
_intraday_analytics_cache: tuple[float, dict] | None = None
INTRADAY_POSITIONS = ROOT / "data" / "intraday_positions.json"
INTRADAY_POSITIONS.parent.mkdir(parents=True, exist_ok=True)
PAPER_ACCOUNT = ROOT / "data" / "paper_account.json"
DEFAULT_PAPER_CAPITAL = 1_000_000.0


def clean_json(value):
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def persist_job(job_id: str) -> None:
    """Caller holds _lock; replace prevents partial records after a restart."""
    path = RUN_ROOT / f"{job_id}.job.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(clean_json(_jobs[job_id]), ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def read_factor_report(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    daily = path.parent / "factor_daily.csv"
    if daily.exists():
        rows = []
        with daily.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                parsed = {}
                for key, value in row.items():
                    if key.endswith("date"):
                        parsed[key] = value
                    else:
                        try: parsed[key] = float(value) if value else None
                        except (ValueError, TypeError): parsed[key] = value
                rows.append(parsed)
        result["daily_metrics"] = rows
    return clean_json(result)


def latest_panel_date() -> str | None:
    panels = sorted(project_path("factor_horizon", "shadow_live", "data", "panels").glob("astock_daily_ohlcva_*.parquet"))
    return panels[-1].stem.rsplit("_", 1)[-1] if panels else None


def latest_update_status() -> dict:
    """Return the last automatic daily/intraday refresh decision, if any."""
    path = project_path("factor_horizon", "shadow_live", "data", "update_status.json")
    if not path.exists():
        return {"available": False, "reason": "尚未执行自动数据检查"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {"available": True, **clean_json(value)}
    except (OSError, ValueError, TypeError) as exc:
        return {"available": False, "reason": f"更新状态不可读: {exc}"}


def intraday_data_status() -> dict:
    """Report observed bar coverage from the selected panel's own summary.

    A planner cutoff is a requested timestamp, never evidence of downloaded
    rows. Do not read a large parquet on every UI poll.
    """
    panel = _intraday_panel_path()
    if panel is None:
        return {"available": False, "status": "unknown", "reason": "尚无分时面板"}
    summary = panel.with_suffix(".summary.json")
    try:
        if summary.stat().st_mtime + 1 < panel.stat().st_mtime:
            raise ValueError("面板已变更，摘要需要重新核验")
        data = json.loads(summary.read_text(encoding="utf-8"))
        timestamp = data.get("max_timestamp")
        if not timestamp or timestamp == "NaT":
            raise ValueError("摘要未提供真实 bar 时间")
        failed = int(data.get("failure_count") or 0)
        codes = int(data.get("codes") or 0)
        latest_codes = int(data.get("latest_codes") or 0)
        return {"available": True, "status": "partial" if failed or latest_codes < codes else "updated",
                "latest_timestamp": timestamp, "latest_codes": latest_codes,
                "codes": codes, "failure_count": failed, "source": data.get("source"),
                "panel": str(panel), "updated_at": datetime.fromtimestamp(panel.stat().st_mtime).astimezone().isoformat(timespec="seconds")}
    except (OSError, ValueError, TypeError) as exc:
        return {"available": False, "status": "unknown", "panel": str(panel), "reason": f"分时新鲜度未核验：{exc}"}


def factor_parameters(params: dict, config: dict | None = None) -> dict:
    cfg = config if config is not None else load_config().get("factor", {})
    if str(params.get("kronos_source") or cfg.get("kronos_source") or "daily") == "composite":
        params = dict(params, kronos_weight=1.0, set10_weight=0.0, ema_kronos=0.0, ema_set10=0.0)
    values = {}
    for key, low, high, integer in (
        ("ema_kronos", 0, 1, False), ("ema_set10", 0, 1, False),
        ("kronos_weight", 0, 1, False), ("set10_weight", 0, 1, False),
        ("lookback_days", 1, 252, True), ("holding_days", 1, 10, True), ("top_k", 1, 200, True),
        ("exit_rank", 1, 100000, True),
        ("gate_alpha", 0, 0.99, False),
        ("fee_bps", 0, 1000, False),
    ):
        raw = params.get(key)
        if raw is None or raw == "": raw = cfg.get(key, DEFAULT_CONFIG["factor"][key])
        try: number = float(raw)
        except (ValueError, TypeError): raise ValueError(f"{key} 必须是有效数字")
        if not math.isfinite(number) or number < low or number > high or (key.startswith("ema_") and number == 1):
            raise ValueError(f"{key} 超出允许范围 {low}–{high}" + ("（不含 1）" if key.startswith("ema_") else ""))
        if integer and number != int(number): raise ValueError(f"{key} 必须为整数")
        values[key] = int(number) if integer else number
    if abs(values["kronos_weight"] + values["set10_weight"] - 1) > 1e-6:
        raise ValueError("Kronos 与 SET10 权重之和必须为 1")
    return values


def _read_parquet_rows(path: Path, columns: list[str], filters=None, limit: int = 240):
    """Read a small, filtered slice for charts; never load the full panel."""
    try:
        import pandas as pd
        frame = pd.read_parquet(path, columns=columns, filters=filters, engine="pyarrow")
        if len(frame) > limit:
            frame = frame.tail(limit)
        return frame
    except Exception:
        return None


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return merge(DEFAULT_CONFIG, loaded)
        except (OSError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(value: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(base: dict, update: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in update.items():
        if value is None or value == "":
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def project_path(*parts: str) -> Path:
    return Path(SERVER_ROOT, *parts)


def capability_state() -> dict:
    # The app reports capability availability instead of pretending a command
    # succeeded when a server checkout has not been mounted.
    shadow = project_path("factor_horizon", "shadow_live")
    intraday = project_path("finetune_lora", "data")
    runner = shadow / "run_shadow_day.sh"
    factor_script = project_path("factor_horizon", "joint_strategy.py")
    return {
        "data_update": {
            "available": UPDATE_SCRIPT.exists() and runner.exists(),
            "reason": "自动检查日线与已完成分时 K 线" if UPDATE_SCRIPT.exists() and runner.exists() else "数据更新脚本或服务器项目目录不可用",
        },
        "review_write": {"available": True, "reason": "写入本地 reviews/，可由同步作业归档"},
        "factor_evaluate": {
            "available": factor_script.exists() and (ROOT / "factor_evaluate.py").exists(),
            "reason": "默认因子评估已就绪" if factor_script.exists() and (ROOT / "factor_evaluate.py").exists() else "因子脚本不可用",
        },
        "predict": {
            "available": intraday.exists(),
            "reason": "Kronos 数据目录已挂载" if intraday.exists() else "服务器项目目录不可用",
        },
        "composite_factor": {
            "available": (ROOT / "composite_factor.py").exists() and project_path("factor_horizon", "shadow_live", "curves", "rp10_current.parquet").exists(),
            "reason": "E1–E10 组合因子入口已就绪" if (ROOT / "composite_factor.py").exists() else "组合因子脚本不可用",
        },
        "intraday_predict": {
            "available": (ROOT / "intraday_live_workflow.py").exists() and (ROOT / "engine_intraday.py").exists() and project_path("finetune_lora", "data", "astock_30min_ohlcva_full_20260912_parallel.parquet").exists(),
            "reason": "分时模型动态推演入口已就绪" if (ROOT / "intraday_live_workflow.py").exists() else "分时推演脚本不可用",
        },
        "chan_multi": {
            "available": (ROOT / "chan_multi_engine.py").exists() and (ROOT / "chan_core.py").exists(),
            "reason": "多级别缠论引擎已就绪" if (ROOT / "chan_multi_engine.py").exists() else "多级别缠论引擎脚本不可用",
        },
        "trend_refresh": {
            "available": (ROOT / "trend_analysis.py").exists(),
            "reason": "刷新官方指数与行业趋势快照",
        },
    }


# The small supervisor writes its exit code even if the web service restarts.
# It executes an argv array; user input is never interpreted as shell code.
RUNNER_CODE = """import json, os, subprocess, sys
from datetime import datetime
path, argv = sys.argv[1], json.loads(sys.argv[2])
try:
    rc = subprocess.call(argv)
except Exception as exc:
    print(str(exc), flush=True)
    rc = 127
with open(path + '.tmp', 'w') as handle:
    json.dump({'returncode': rc, 'finished_at': datetime.now().astimezone().isoformat(timespec='seconds')}, handle)
os.replace(path + '.tmp', path)
sys.exit(rc)
"""


def monitor_job(job_id: str, proc=None) -> None:
    exit_path = RUN_ROOT / f"{job_id}.exit.json"
    while True:
        with _lock:
            value = dict(_jobs[job_id])
        log_path = Path(value.get("log", RUN_ROOT / f"{job_id}.log"))
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-1] if lines else ""
        except OSError:
            tail = ""
        updates = {}
        if tail:
            updates["stage"] = tail[-240:]
            match = re.search(r"\[stage (\d+)/(\d+)\]\s*(.*)", tail)
            if match and int(match.group(2)):
                updates["progress"] = min(99, round(int(match.group(1)) / int(match.group(2)) * 100, 1))
        finished = False
        if exit_path.exists():
            try:
                outcome = json.loads(exit_path.read_text(encoding="utf-8"))
                rc = int(outcome["returncode"])
                updates.update(status="completed" if rc == 0 else "failed", returncode=rc, finished_at=outcome.get("finished_at", now()))
                if rc == 0: updates["progress"] = 100.0
                finished = True
            except (OSError, ValueError, KeyError):
                pass
        if not finished:
            alive = proc.poll() is None if proc is not None else False
            if proc is None and value.get("pid"):
                try:
                    os.kill(int(value["pid"]), 0)
                    alive = True
                except (ProcessLookupError, PermissionError, ValueError):
                    pass
            if not alive:
                updates.update(status="error", error="任务进程已退出但未留下退出状态；请检查日志后重试。", finished_at=now())
                finished = True
        with _lock:
            if any(_jobs[job_id].get(k) != v for k, v in updates.items()):
                _jobs[job_id].update(updates)
                persist_job(job_id)
        if finished:
            if proc is not None: proc.wait()
            # A current trading day can be open in the calendar before the
            # official daily rows are published.  update_data.sh preserves
            # the previous complete panel and exits 0 with an explicit
            # DATA_UPDATE_DEFERRED marker.  Keep that state visible and do
            # not run RankIC against stale data.
            deferred = False
            if value.get("kind") == "data_update" and updates.get("status") == "completed" and not value.get("params", {}).get("validate_only"):
                # Preserve partial intraday refresh diagnostics from the
                # planner; a zero process return code alone is not proof that
                # every symbol reached the requested cutoff.
                try:
                    planner = latest_update_status()
                    intraday_state = planner.get("intraday") or {}
                    if intraday_state.get("status") == "partial":
                        updates["intraday_status"] = "partial"
                        updates["intraday_partial"] = True
                        updates["stage"] = "分时部分更新：数据源存在失败股票，已保存可用结果；请稍后重试"
                        summary_path = intraday_state.get("output_panel", "")
                        if summary_path:
                            summary = Path(summary_path).with_suffix(".summary.json")
                            if summary.exists():
                                data = json.loads(summary.read_text(encoding="utf-8"))
                                updates["intraday_failure_count"] = data.get("failure_count", 0)
                                updates["intraday_latest_codes"] = data.get("latest_codes", 0)
                except (OSError, ValueError, TypeError):
                    pass
                try:
                    deferred = any("DATA_UPDATE_DEFERRED" in line for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:])
                except OSError:
                    pass
                if not deferred:
                    status_path = project_path("factor_horizon", "shadow_live", "data", "fetch_status.json")
                    try:
                        fetch_status = json.loads(status_path.read_text(encoding="utf-8"))
                        deferred = fetch_status.get("status") == "deferred" and fetch_status.get("signal_date") == value.get("metadata", {}).get("signal_date") and not any("DATA_UPDATE_OK" in line for line in lines[-80:])
                    except (OSError, ValueError, TypeError):
                        pass
                # Automatic session refreshes can finish successfully after
                # refreshing intraday bars while the official daily close is
                # still waiting for publication (or outside trading hours).
                # Do not launch a daily RankIC job against that partial state.
                # The catch-up planner reports up_to_date when the panel
                # already covers the newest completed session (weekends and
                # holidays) — that is a healthy no-op, not a deferred refresh.
                if value.get("metadata", {}).get("mode") == "automatic_latest_session":
                    try:
                        planner = latest_update_status()
                        daily_state = (planner.get("daily") or {}).get("status")
                        updates["daily_status"] = daily_state or "unknown"
                        if daily_state not in {"updated", "up_to_date"}:
                            deferred = True
                    except (AttributeError, TypeError):
                        updates["daily_status"] = "unknown"
                        deferred = True
                if deferred:
                    updates["deferred"] = True
                    stage_by_daily = {
                        "deferred": "等待官方日线发布，已保留上一份完整面板；稍后重试",
                        "blocked": "交易日历缺失或无基准面板，已保留现有面板；请检查日历缓存",
                        "failed": "数据更新失败，已保留上一份完整面板；详见运行日志",
                    }
                    updates["stage"] = stage_by_daily.get(updates.get("daily_status")) or "等待官方日线发布，已保留上一份完整面板；稍后重试"
                    with _lock:
                        _jobs[job_id].update(updates)
                        persist_job(job_id)
            # A successful daily close automatically refreshes the RankIC
            # series with the saved EMA/weight defaults.  It is a separate
            # auditable job so a failed evaluation never hides a data update.
            # up_to_date means no new close arrived (repeat click on a
            # weekend), so the identical evaluation is not re-run.
            if updates.get("status") == "completed" and not updates.get("deferred") and updates.get("daily_status") != "up_to_date" and value.get("kind") == "data_update" and not value.get("params", {}).get("validate_only"):
                threading.Thread(target=_auto_factor_after_close, args=(value,), daemon=True).start()
            return
        time.sleep(1)


def _auto_factor_after_close(data_job: dict) -> None:
    """Queue the default causal factor evaluation after a daily refresh."""
    try:
        auto_id = uuid.uuid4().hex[:12]
        signal = data_job.get("daily_latest_date") or (data_job.get("metadata") or {}).get("signal_date") or latest_panel_date()
        params = {"date": f"{signal[:4]}-{signal[4:6]}-{signal[6:]}" if signal and len(str(signal)) == 8 else safe_date(None), "_job_id": auto_id, "auto_trigger": "daily_close"}
        argv, cwd, metadata = job_spec("factor_evaluate", params)
        job = {"id": auto_id, "kind": "factor_evaluate", "status": "queued", "created_at": now(), "params": {"date": params["date"], "auto_trigger": "daily_close"}, "metadata": metadata, "progress": 0.0}
        with _lock:
            _jobs[auto_id] = job; persist_job(auto_id)
        threading.Thread(target=run_command, args=(auto_id, argv, cwd), daemon=True).start()
    except Exception as exc:
        # The data update remains successful; the reason is visible in app.out.
        print(f"[auto-factor] unable to queue RankIC evaluation: {exc}", flush=True)


def run_command(job_id: str, argv: list[str], cwd: str | None = None) -> None:
    log_path = RUN_ROOT / f"{job_id}.log"
    exit_path = RUN_ROOT / f"{job_id}.exit.json"
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[{now()}] command={shlex.join(argv)}\n")
            log.flush()
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", RUNNER_CODE, str(exit_path), json.dumps(argv)],
                cwd=cwd, stdout=log, stderr=subprocess.STDOUT, text=True,
                env=os.environ.copy(), start_new_session=True,
            )
            with _lock:
                _jobs[job_id].update(pid=proc.pid, status="running", log=str(log_path))
                persist_job(job_id)
            monitor_job(job_id, proc)
    except Exception as exc:
        with _lock:
            _jobs[job_id].update(status="error", error=str(exc), finished_at=now(), log=str(log_path))
            persist_job(job_id)


def restore_jobs() -> None:
    for path in RUN_ROOT.glob("*.job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if not re.fullmatch(r"[a-f0-9]{12}", str(job.get("id", ""))): continue
            _jobs[job["id"]] = job
        except (OSError, ValueError):
            continue
    for job in list(_jobs.values()):
        if job.get("status") in {"queued", "running"}:
            threading.Thread(target=monitor_job, args=(job["id"],), daemon=True).start()


restore_jobs()


def safe_date(value: str | None) -> str:
    if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return datetime.now().strftime("%Y-%m-%d")


def job_spec(kind: str, params: dict) -> tuple[list[str], str | None, dict]:
    requested_date = params.get("date")
    date = safe_date(requested_date)
    ymd = date.replace("-", "")
    if kind == "data_update":
        # No date means automatic session planning.  It checks the calendar and
        # refreshes completed intraday bars during the session, then requests
        # the official daily close after the close.  An explicit date remains a
        # deterministic backfill/repair operation for operators.
        if UPDATE_SCRIPT.exists():
            validate_only = bool(params.get("validate_only"))
            if requested_date:
                argv = ["bash", str(UPDATE_SCRIPT), ymd]
                metadata = {"signal_date": ymd, "scope": "validation" if validate_only else "daily_panel,index_tail", "mode": "explicit_date"}
            else:
                if validate_only:
                    # Keep validation on the same calendar-aware planner as
                    # the real button; the planner's plan-only mode performs
                    # no network writes and reports both source states.
                    argv = ["bash", str(UPDATE_SCRIPT), "--auto", "--validate-only"]
                    metadata = {"scope": "validation", "mode": "automatic_validation"}
                else:
                    argv = ["bash", str(UPDATE_SCRIPT), "--auto"]
                    metadata = {"scope": "daily_and_intraday_auto", "mode": "automatic_latest_session"}
            if validate_only:
                if argv[-1] != "--validate-only":
                    argv.append("--validate-only")
            return argv, str(ROOT), metadata
        raise FileNotFoundError(f"data update script unavailable: {UPDATE_SCRIPT}")
    if kind == "factor_evaluate":
        script = ROOT / "factor_evaluate.py"
        if not script.exists():
            raise FileNotFoundError("default factor evaluation script unavailable")
        panel_date = latest_panel_date()
        if not params.get("date") and panel_date:
            ymd = panel_date
        # Keep runs for different parameter sets on the same signal date
        # separate so a later experiment cannot overwrite an earlier result.
        suffix = str(params.get("_job_id", "latest"))
        output_dir = RUN_ROOT / f"factor_{ymd}_{suffix}"
        values = factor_parameters(params)
        source = str(params.get("kronos_source") or load_config().get("factor", {}).get("kronos_source") or "daily").strip().lower()
        gate_source = str(params.get("gate_source") or load_config().get("factor", {}).get("gate_source") or "wave").strip().lower()
        if gate_source not in {"off", "wave", "chan"}:
            raise ValueError("gate_source 必须是 off / wave / chan")
        gate_mode = str(params.get("gate_mode") or load_config().get("factor", {}).get("gate_mode") or ("up_only" if gate_source == "chan" else "off"))
        if gate_mode not in {"off", "non_down", "up_only"}:
            raise ValueError("gate_mode 必须是 off / non_down / up_only")
        if source == "composite" and gate_source == "wave" and gate_mode != "off":
            raise ValueError("纯四期限组合请选缠论信号与三种持仓规则；不支持把浪型标签当成缠论回测")
        if source not in {"daily", "intraday", "composite"}:
            raise ValueError("kronos_source 必须是 daily / intraday / composite")
        argv = [os.getenv("STOCKLAB_FACTOR_PYTHON", sys.executable), str(script), "--date", ymd, "--output-dir", str(output_dir),
                "--kronos-source", source,
                "--kronos-ema", str(values["ema_kronos"]), "--set10-ema", str(values["ema_set10"]),
                "--kronos-weight", str(values["kronos_weight"]), "--set10-weight", str(values["set10_weight"]),
                "--lookback-days", str(values["lookback_days"]), "--holding-days", str(values["holding_days"]), "--top-k", str(values["top_k"]), "--exit-rank", str(values["exit_rank"]), "--gate-alpha", str(values["gate_alpha"]), "--gate-source", gate_source, "--gate-mode", gate_mode, "--fee-bps", str(values["fee_bps"])]
        if not Path(argv[0]).exists(): argv[0] = os.getenv("STOCKLAB_PYTHON", sys.executable)
        return argv, None, {"signal_date": ymd, "output_dir": str(output_dir), "mode": "causal_factor_evaluation", "kronos_source": source, "gate_source": gate_source, "gate_mode": gate_mode, "effective_parameters": values}
    if kind == "predict":
        # The exact shadow-live command is allow-listed. Its script performs
        # trading-calendar lookup, data freshness checks, GPU selection, and
        # T+1 planning; the UI never constructs arbitrary shell commands.
        latest = latest_panel_date()
        if latest and ymd > latest:
            raise ValueError(f"信号日 {date} 晚于最新完整日线 {latest[:4]}-{latest[4:6]}-{latest[6:]}；预测明日请使用最新完整日线作为信号日，目标交易日由交易日历自动推导")
        script = project_path("factor_horizon", "shadow_live", "run_shadow_day.sh")
        if script.exists():
            return ["bash", str(script), ymd], None, {"signal_date": ymd, "target": "next trading day"}
        raise FileNotFoundError(f"prediction runner unavailable: {script}")
    if kind == "composite_factor":
        script = ROOT / "composite_factor.py"
        curves = project_path("factor_horizon", "shadow_live", "curves", "rp10_current.parquet")
        if not script.exists() or not curves.exists(): raise FileNotFoundError("组合因子曲线不可用")
        panel_date = latest_panel_date() or ymd
        output_dir = RUN_ROOT / f"composite_{panel_date}_{params.get('_job_id','latest')}"
        horizons = str(params.get("horizons", "1,3,5,10")); weights = str(params.get("weights", "equal"))
        py = os.getenv("STOCKLAB_FACTOR_PYTHON", sys.executable)
        if not Path(py).exists(): py = os.getenv("STOCKLAB_PYTHON", sys.executable)
        argv = [py, str(script), "--curves", str(curves), "--date", panel_date, "--output-dir", str(output_dir), "--horizons", horizons, "--weights", weights, "--lookback-days", str(params.get("lookback_days", 5)), "--top-k", str(params.get("top_k", 10))]
        return argv, None, {"signal_date": panel_date, "output_dir": str(output_dir), "mode": "kronos_e1_e10_composite"}
    if kind == "trend_refresh":
        script = ROOT / "trend_analysis.py"
        if not script.exists(): raise FileNotFoundError("趋势分析脚本不可用")
        py = os.getenv("STOCKLAB_FACTOR_PYTHON", sys.executable)
        if not Path(py).exists(): py = os.getenv("STOCKLAB_PYTHON", sys.executable)
        return [py, str(script), "--data-dir", str(ROOT / "data" / "trends")], str(ROOT), {"mode": "index_industry_trends", "output_dir": str(ROOT / "data" / "trends")}
    if kind == "chan_multi":
        script = ROOT / "chan_multi_engine.py"
        if not script.exists():
            raise FileNotFoundError("多级别缠论引擎脚本不可用")
        combo = str(params.get("combo") or "5F-30F-D")
        if combo not in {"5F-30F-D", "30F-D-W", "D-W-M", "all"}:
            raise ValueError("combo 必须是 5F-30F-D / 30F-D-W / D-W-M / all")
        py = os.getenv("STOCKLAB_CHAN_PYTHON", sys.executable)
        if not Path(py).exists():
            py = os.getenv("STOCKLAB_PYTHON", sys.executable)
        argv = [py, str(script), "--combo", combo]
        if params.get("quick"):
            argv.append("--quick")
        return argv, str(ROOT), {"mode": "chan_multi_walk_forward", "combo": combo,
                                 "output_dir": str(ROOT / "data" / "chan_multi" / "combos" / combo)}
    if kind == "intraday_predict":
        script = ROOT / "intraday_live_workflow.py"
        if not script.exists(): raise FileNotFoundError("分时动态推演脚本不可用")
        output_dir = RUN_ROOT / f"intraday_live_{ymd}_{params.get('_job_id','latest')}"
        # The Kronos checkpoint loader requires safetensors/transformers; the
        # lightweight aloha environment on the host does not ship them.  Use
        # the project's GPU runtime by default, while still allowing an
        # explicit STOCKLAB_INTRADAY_PYTHON override.
        py = os.getenv("STOCKLAB_INTRADAY_PYTHON", sys.executable)
        if not Path(py).exists(): py = os.getenv("STOCKLAB_PYTHON", sys.executable)
        argv = [py, str(script), "--project-root", SERVER_ROOT, "--output-dir", str(output_dir), "--device", str(params.get("device", "auto")), "--gpu-wait-seconds", str(params.get("gpu_wait_seconds", 900)), "--top-k", str(params.get("top_k", 5)), "--max-gpus", str(params.get("max_gpus", 4)), "--refresh", str(params.get("refresh", "auto"))]
        return argv, None, {"signal_date": ymd, "output_dir": str(output_dir), "mode": "latest_observed_intraday_bars"}
    raise ValueError(f"unsupported job kind: {kind}")


@app.get("/")
def index():
    return send_from_directory(str(ROOT / "templates"), "index.html")


@app.get("/api/status")
def status():
    cfg = load_config()
    shadow = project_path("factor_horizon", "shadow_live")
    data_files = sorted((shadow / "data" / "panels").glob("astock_daily_ohlcva_*.parquet")) if shadow.exists() else []
    latest = data_files[-1].stem.rsplit("_", 1)[-1] if data_files else None
    latest_file = data_files[-1] if data_files else None
    latest_updated = datetime.fromtimestamp(latest_file.stat().st_mtime).astimezone().isoformat(timespec="seconds") if latest_file else None
    review_count = len(list(REVIEW_ROOT.glob("*.md")))
    update_status = latest_update_status()
    intraday = intraday_data_status()
    last_updated = max(filter(None, [latest_updated, intraday.get("updated_at")]), default=None)
    return jsonify({
        "server_root": SERVER_ROOT,
        "time": now(),
        "capabilities": capability_state(),
        "data": {"latest_panel_date": latest, "latest_date": latest, "panel_count": len(data_files), "review_count": review_count, "last_updated": last_updated, "daily_updated_at": latest_updated, "intraday": intraday, "update_status": update_status},
        "config": cfg,
    })


@app.get("/api/config")
def get_config():
    return jsonify(load_config())


@app.put("/api/config")
def put_config():
    body = request.get_json(silent=True) or {}
    value = merge(load_config(), body)
    save_config(value)
    return jsonify(value)


@app.get("/api/review")
def get_review():
    date = safe_date(request.args.get("date"))
    path = REVIEW_ROOT / f"{date}.md"
    return jsonify({"date": date, "content": path.read_text(encoding="utf-8") if path.exists() else "", "path": str(path)})


@app.put("/api/review")
def put_review():
    body = request.get_json(silent=True) or {}
    date = safe_date(body.get("date"))
    content = str(body.get("content", ""))
    path = REVIEW_ROOT / f"{date}.md"
    path.write_text(content, encoding="utf-8")
    return jsonify({"date": date, "path": str(path), "bytes": len(content.encode("utf-8"))})


@app.post("/api/jobs")
def create_job():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return jsonify({"error": "params 必须是 JSON 对象"}), 400
    caps = capability_state()
    if kind not in caps:
        return jsonify({"error": "unsupported job kind"}), 400
    if not caps[kind].get("available"):
        return jsonify({"error": caps[kind].get("reason", "capability unavailable")}), 503
    # A browser reconnect must attach to the existing data refresh instead of
    # starting a second 30-minute, per-symbol download.
    if kind in {"data_update", "chan_multi", "trend_refresh"}:
        with _lock:
            existing = next((dict(x) for x in _jobs.values()
                             if x.get("kind") == kind and x.get("status") in {"queued", "running"}), None)
        if existing:
            existing["reused"] = True
            return jsonify(existing), 202
    job_id = uuid.uuid4().hex[:12]
    try:
        spec_params = dict(params)
        if kind in {"factor_evaluate", "composite_factor", "intraday_predict"}: spec_params["_job_id"] = job_id
        argv, cwd, metadata = job_spec(kind, spec_params)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 503
    _jobs[job_id] = {"id": job_id, "kind": kind, "status": "queued", "created_at": now(), "params": params, "metadata": metadata, "progress": 0.0}
    with _lock:
        persist_job(job_id)
    thread = threading.Thread(target=run_command, args=(job_id, argv, cwd), daemon=True)
    thread.start()
    return jsonify(_jobs[job_id]), 202


@app.get("/api/runs")
def list_runs():
    with _lock:
        values = list(_jobs.values())
    return jsonify(sorted(values, key=lambda x: x.get("created_at", ""), reverse=True))


def _live_intraday_preview(output_dir: Path, log_path: str | None):
    """Partial per-shard rows while a multi-GPU intraday run is still in flight."""
    import pandas as pd
    frames = []
    for path in sorted(output_dir.glob("shard_live_*.csv")):
        if ".tmp." in path.name:
            continue  # atomic-write leftover from a dead worker
        try:
            frame = pd.read_csv(path)
            if not frame.empty:
                frames.append(frame)
        except Exception:
            continue  # torn read of an in-flight rewrite; next poll will retry
    if not frames:
        return None
    merged = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code", keep="last")
    total = None
    if log_path and Path(log_path).exists():
        try:
            match = re.search(r'"candidate_count":\s*(\d+)', Path(log_path).read_text(encoding="utf-8", errors="replace")[-400000:])
            if match:
                total = int(match.group(1))
        except OSError:
            pass
    count = int(merged.shape[0])
    merged = merged.sort_values("pred_return", ascending=False).head(12)
    top = [{"ts_code": str(r.ts_code), "pred_return": float(r.pred_return)} for r in merged.itertuples()]
    return {"count": count, "total": total, "top": top}


@app.get("/api/runs/<job_id>")
def get_run(job_id: str):
    value = _jobs.get(job_id)
    if not value:
        return jsonify({"error": "run not found"}), 404
    result = dict(value)
    if result.get("log") and Path(result["log"]).exists():
        lines = Path(result["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
        result["tail"] = lines[-80:]
    output_dir = (result.get("metadata") or {}).get("output_dir")
    if output_dir and result.get("status") not in {"completed", "failed", "error"}:
        live = _live_intraday_preview(Path(output_dir), result.get("log"))
        if live:
            result["live_predictions"] = live
    if output_dir and result.get("status") == "completed":
        report = Path(output_dir) / "factor_snapshot.json"
        if report.exists():
            try:
                result["result"] = read_factor_report(report)
            except (OSError, json.JSONDecodeError):
                pass
    return jsonify(result)


@app.get("/api/artifacts")
def artifacts():
    roots = [RUN_ROOT, project_path("factor_horizon", "shadow_live", "results")]
    items = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.stat().st_size < 250 * 1024 * 1024:
                if path.is_relative_to(RUN_ROOT): artifact_id = "runs/" + str(path.relative_to(RUN_ROOT))
                else: artifact_id = "shadow_results/" + str(path.relative_to(project_path("factor_horizon", "shadow_live", "results")))
                items.append({"id": artifact_id, "name": path.name, "path": str(path), "size": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")})
    return jsonify(sorted(items, key=lambda x: x["updated_at"], reverse=True)[:200])


@app.get("/api/artifacts/<path:artifact_id>")
def artifact_download(artifact_id: str):
    if artifact_id.startswith("runs/"):
        path = RUN_ROOT / artifact_id.removeprefix("runs/")
    elif artifact_id.startswith("shadow_results/"):
        path = project_path("factor_horizon", "shadow_live", "results") / artifact_id.removeprefix("shadow_results/")
    else:
        # backwards compatibility for old links; still enforce containment.
        path = Path("/") / artifact_id
    allowed_roots = [RUN_ROOT.resolve(), project_path("factor_horizon", "shadow_live", "results").resolve()]
    resolved = path.resolve()
    if not any((resolved == root or root in resolved.parents) for root in allowed_roots) or not resolved.is_file():
        return jsonify({"error": "artifact not found"}), 404
    return send_from_directory(str(resolved.parent), resolved.name, as_attachment=True)


@app.get("/api/results/latest")
def latest_results():
    root = project_path("factor_horizon", "shadow_live", "results")
    files = sorted(root.glob("*/summary.json"), key=lambda p: p.stat().st_mtime) if root.exists() else []
    if not files:
        return jsonify({"available": False, "reason": "暂无 shadow-live summary.json"})
    path = files[-1]
    try:
        return jsonify(clean_json({"available": True, "path": str(path), "summary": json.loads(path.read_text(encoding="utf-8"))}))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"available": False, "reason": str(exc)})


@app.get("/api/factors/latest")
def latest_factor_results():
    files = sorted(RUN_ROOT.glob("factor_*/factor_snapshot.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return jsonify({"available": False, "reason": "暂无因子评估产物"})
    path = files[-1]
    try:
        return jsonify({"available": True, "path": str(path), "result": read_factor_report(path)})
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"available": False, "reason": str(exc)})


@app.get("/api/trends")
def trends_overview():
    from trend_analysis import load_snapshot
    try:
        return jsonify(clean_json(load_snapshot(ROOT / "data" / "trends")))
    except (OSError, ValueError, KeyError) as exc:
        return jsonify({"available": False, "reason": f"趋势快照不可读: {exc}"}), 503


@app.get("/api/chan/classic-cases")
def chan_classic_cases():
    path=ROOT/"data"/"chan_classic"/"cases.json"
    if not path.exists():
        return jsonify({"available":False,"reason":"经典案例数据尚未生成"}),503
    try:
        return jsonify(json.loads(path.read_text()))
    except (OSError,ValueError) as exc:
        return jsonify({"available":False,"reason":f"经典案例不可读: {exc}"}),503


@app.get("/api/trends/<code>")
def trends_asset(code):
    from trend_analysis import load_asset
    try:
        return jsonify(clean_json(load_asset(code, request.args.get("level", "D"), ROOT / "data" / "trends")))
    except ValueError as exc:
        return jsonify({"available": False, "reason": str(exc)}), 400
    except (OSError, KeyError) as exc:
        return jsonify({"available": False, "reason": f"趋势数据不可读: {exc}"}), 503


@app.get("/api/market_wave")
def market_wave_api():
    """大盘浪型阶段因子（艾略特推动/调整二相概率），由 market_wave_factor.py 生成。"""
    path = ROOT / "data" / "market_wave.json"
    if not path.exists():
        return jsonify({"available": False, "reason": "尚未生成（运行 market_wave_factor.py --refresh）"})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("summary", {}).get("available"):
            return jsonify({"available": False, "reason": "浪型因子数据为空"})
        # Preserve the historical smoothed series; add explicitly independent
        # causal fast-rule and observed index-position overlays when available.
        try:
            from trend_analysis import load_asset
            trend = load_asset("000300", "D", ROOT / "data" / "trends")
            overlay = {r["signal_date"]: r for r in trend.get("wave", [])}
            for row in data.get("daily", []):
                other = overlay.get(str(row["date"]), {})
                for field in ("p_fast", "csi300_pos", "sse_pos"):
                    if row.get(field) is None and other.get(field) is not None:
                        row[field] = other[field] * (100 if field.endswith("_pos") else 1)
        except (OSError, ValueError, KeyError):
            pass
        return jsonify({"available": True, **data})
    except (OSError, ValueError) as exc:
        return jsonify({"available": False, "reason": f"浪型因子不可读: {exc}"})


@app.get("/api/chan_phase")
def chan_phase_api():
    """缠论阶段引擎（门控主信号）：summary + 全历史 daily。"""
    csv_path = ROOT / "data" / "chan_phase.csv"
    if not csv_path.exists():
        return jsonify({"available": False, "reason": "尚未生成（运行 chan_phase_engine.py）"})
    try:
        import pandas as pd
        cf = pd.read_csv(csv_path, dtype={"date": str})
        cf = cf.dropna(subset=["phase"]).tail(2600).reset_index(drop=True)
        daily = [{"date": r.date, "close": round(float(r.close), 2), "phase": int(r.phase),
                  "div": (None if pd.isna(r.div) else int(r.div))} for r in cf.itertuples()]
        cur = daily[-1]
        days = 0
        for d in reversed(daily):
            if d["phase"] == cur["phase"]:
                days += 1
            else:
                break
        b1s = [d["date"] for d in daily if d["div"] == 1]
        s1s = [d["date"] for d in daily if d["div"] == -1]
        summary = {"available": True, "kind": "chan_phase", "date": cur["date"],
                   "phase": {1: "上涨", 0: "震荡", -1: "下跌"}[cur["phase"]], "phase_code": cur["phase"],
                   "days_in_phase": days,
                   "last_bottom_div": b1s[-1] if b1s else None, "last_top_div": s1s[-1] if s1s else None,
                   "bottom_div_count": len(b1s), "top_div_count": len(s1s),
                   "edge": None, "engine_version": "causal_online_v2",
                   "method": "收盘价简化结构：分型→笔→中枢；逐日冻结输出；收盘确认、次日执行。历史表现请查看修复后的组合回测"}
        return jsonify({"available": True, "summary": summary, "daily": daily})
    except Exception as exc:
        return jsonify({"available": False, "reason": f"缠论数据不可读: {exc}"})


@app.get("/api/chan_multi")
def chan_multi_api():
    """Multi-timeframe Chan regime: latest state, daily tail, grid and structures."""
    combo = str(request.args.get("combo") or "5F-30F-D")
    if combo not in {"5F-30F-D", "30F-D-W", "D-W-M"}:
        return jsonify({"error": "combo 必须是 5F-30F-D / 30F-D-W / D-W-M"}), 400
    base = ROOT / "data" / "chan_multi" / "combos" / combo
    latest_path = base / "latest.json"
    if not latest_path.exists():
        return jsonify({"available": False, "combo": combo,
                        "reason": "尚未生成（运行 chan_multi_engine.py --combo %s）" % combo})
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        eval_path = base / "eval.json"
        evaluation = json.loads(eval_path.read_text(encoding="utf-8")) if eval_path.exists() else {}
        daily = []
        csv_path = base / "daily.csv"
        if csv_path.exists():
            import pandas as pd
            frame = pd.read_csv(csv_path)
            wanted = [c for c in (
                "date", "close", "label", "fwd_ret", "align_score", "align_phase",
                "consensus", "rule_pred", "rule2_pred", "test_flag", "pred",
                "pred_hier", "pred_model", "pred_hier_full", "proba_down",
                "proba_flat", "proba_up", "proba_hier_down", "proba_hier_flat",
                "proba_hier_up") if c in frame.columns]
            daily = clean_json(frame[wanted].tail(1800).to_dict(orient="records"))
        structures = {}
        struct_path = base / "structures.json"
        if struct_path.exists():
            structures = json.loads(struct_path.read_text(encoding="utf-8"))
        return jsonify(clean_json({
            "available": True, "combo": combo, "latest": latest,
            "headline": latest.get("headline"),
            "coverage": latest.get("coverage"),
            "data_sources": latest.get("data_sources"),
            "daily": daily,
            "grid": evaluation.get("grid", []),
            "primary": evaluation.get("primary", {}),
            "structures": structures,
        }))
    except (OSError, ValueError, TypeError) as exc:
        return jsonify({"available": False, "combo": combo, "reason": "多级别缠论数据不可读: %s" % exc})


@app.get("/api/chan_multi/eval")
def chan_multi_eval_api():
    combo = str(request.args.get("combo") or "5F-30F-D")
    if combo not in {"5F-30F-D", "30F-D-W", "D-W-M"}:
        return jsonify({"error": "combo 必须是 5F-30F-D / 30F-D-W / D-W-M"}), 400
    path = ROOT / "data" / "chan_multi" / "combos" / combo / "eval.json"
    if not path.exists():
        return jsonify({"available": False, "combo": combo,
                        "reason": "尚未生成（运行 chan_multi_engine.py --combo %s）" % combo})
    try:
        return jsonify(clean_json({"available": True, "combo": combo,
                                   "eval": json.loads(path.read_text(encoding="utf-8"))}))
    except (OSError, ValueError) as exc:
        return jsonify({"available": False, "combo": combo, "reason": str(exc)})


@app.get("/api/champion_workbook")
def champion_workbook():
    """Serve the curated, read-only summary extracted from the supplied XLSX."""
    path = ROOT / "data" / "champion_workbook.json"
    if not path.exists():
        return jsonify({"available": False, "reason": "冠军模型工作簿摘要尚未导入"}), 404
    try:
        return jsonify({"available": True, **json.loads(path.read_text(encoding="utf-8"))})
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"available": False, "reason": str(exc)}), 500


def latest_snapshot(prefix: str, filename: str):
    files = sorted(RUN_ROOT.glob(f"{prefix}*/{filename}"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {"available": False, "reason": "暂无结果"}
    path = files[-1]
    try:
        return {"available": True, "path": str(path), "result": json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": str(exc)}


@app.get("/api/composite/latest")
def latest_composite_results():
    result = latest_snapshot("composite_", "composite_snapshot.json")
    if result.get("available"):
        snap = Path(result["path"]); csv_path = snap.parent / "composite_scores_latest.csv"
        rows = []
        if csv_path.exists():
            with csv_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    parsed = {}
                    for key, value in row.items():
                        try: parsed[key] = float(value) if value not in (None, "") else None
                        except (TypeError, ValueError): parsed[key] = value
                    rows.append(parsed)
            result["result"]["top"] = rows[:100]
    return jsonify(result)



_stock_ema_cache: dict = {"key": None, "ema": {}, "days": {}}


def _stock_ema_history(alpha: float = 0.25):
    """Causal EMA of each stock's published pred_return across signal days.

    The timing adapter only sees the current live CSV (one bar per stock), which
    makes its ema_signal degenerate to the raw prediction.  Rebuilding the EMA
    over every published signal day restores the intended "predict earlier,
    then overlay EMA" signal.  Memoised on the set of live-run CSVs.
    """
    import pandas as pd
    csvs = sorted(RUN_ROOT.glob("intraday_live_*/intraday_next_session_predictions.csv"), key=lambda p: p.stat().st_mtime)
    key = tuple((p.name, int(p.stat().st_mtime)) for p in csvs)
    if _stock_ema_cache.get("key") == key:
        return _stock_ema_cache["ema"], _stock_ema_cache["days"]
    per_day: dict[str, dict[str, float]] = {}
    for path in csvs:
        try:
            frame = pd.read_csv(path, usecols=lambda c: c in {"ts_code", "signal_timestamp", "pred_return"})
        except Exception:
            continue
        ts = pd.to_datetime(frame.get("signal_timestamp"), errors="coerce")
        frame = frame.assign(_ts=ts).dropna(subset=["_ts", "pred_return"])
        frame = frame[frame["_ts"].dt.strftime("%H:%M") == "15:00"]
        if frame.empty:
            continue
        day = str(frame["_ts"].dt.strftime("%Y-%m-%d").max())
        bucket = per_day.setdefault(day, {})
        for code, val in zip(frame["ts_code"], pd.to_numeric(frame["pred_return"], errors="coerce")):
            if pd.notna(val):
                bucket[str(code)] = float(val)  # same-day reruns: newer mtime wins
    ema: dict[str, float] = {}
    days: dict[str, int] = {}
    for day in sorted(per_day):
        for code, val in per_day[day].items():
            ema[code] = val if code not in ema else (1 - alpha) * ema[code] + alpha * val
            days[code] = days.get(code, 0) + 1
    _stock_ema_cache["key"], _stock_ema_cache["ema"], _stock_ema_cache["days"] = key, ema, days
    return ema, days


def _enrich_ema_signal(result: dict) -> None:
    """Attach history-based EMA + cross-sectional pct rank to timing/prediction rows."""
    ema, days = _stock_ema_history()
    if not ema:
        return
    ranked = sorted(v for v in ema.values() if v is not None)
    total = len(ranked)

    def pct_rank(v):
        if v is None or not total:
            return None
        return (sum(1 for x in ranked if x <= v) + 0.5) / total

    for key in ("predictions", "latest", "buy_candidates"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("ts_code") or "")
            if code in ema:
                row["ema_signal_hist"] = ema[code]
                row["ema_days"] = days.get(code)
                row["signal_rank_hist"] = pct_rank(ema[code])


@app.get("/api/intraday/latest")
def latest_intraday_results():
    # Accept both the legacy job layout and the standalone timing adapter.
    files = sorted(list(RUN_ROOT.glob("intraday*/intraday_timing_*.json")) + list(RUN_ROOT.glob("intraday/intraday_timing_*.json")), key=lambda p: p.stat().st_mtime)
    if not files: return jsonify({"available": False, "reason": "暂无分时时机结果"})
    try:
        result = json.loads(files[-1].read_text(encoding="utf-8"))
        # Join the timing audit with the full model output so the UI can show
        # an inspectable ranked table and 16-bar prediction path, rather than
        # exposing only a handful of timing rows.
        manifest = files[-1].parent / "manifest.json"
        # A timing refresh can write to the shared `runs/intraday` state
        # directory while its source model CSV remains in an earlier live-run
        # directory.  Follow that recorded source so the latest endpoint still
        # exposes the full ranked prediction table.
        if not manifest.exists():
            for source in result.get("sources", []):
                candidate = Path(source).parent / "manifest.json"
                if candidate.exists():
                    manifest = candidate
                    break
        if manifest.exists():
            result["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
            if result["manifest"].get("invalidated"):
                return jsonify({"available": False, "path": str(files[-1]), "reason": result["manifest"].get("invalidated_reason", "该预测的输入数据已被撤销，请重新推理")})
        csv_path = Path(result.get("manifest", {}).get("output_csv", ""))
        if not csv_path.exists():
            for source in result.get("sources", []):
                if Path(source).suffix.lower() == ".csv" and Path(source).exists():
                    csv_path = Path(source); break
        if csv_path.exists():
            if "manifest" not in result:
                result["manifest"] = {"output_csv": str(csv_path), "observed_latest_bar": result.get("signal_date")}
            rows = []
            with csv_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    parsed = dict(row)
                    for key in ("pred_return", "pred_return_last", "pred_return_std", "last_close", "rank"):
                        try: parsed[key] = float(parsed[key]) if parsed.get(key) not in (None, "") else None
                        except (TypeError, ValueError): pass
                    try: parsed["pred_close_path"] = json.loads(parsed["pred_close_path"])
                    except (KeyError, TypeError, json.JSONDecodeError): pass
                    rows.append(parsed)
            result["predictions"] = rows[:100]
        _enrich_ema_signal(result)
        if INTRADAY_POSITIONS.exists():
            result["paper_positions"] = json.loads(INTRADAY_POSITIONS.read_text(encoding="utf-8"))
        return jsonify({"available": True, "path": str(files[-1]), "result": clean_json(result)})
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return jsonify({"available": False, "reason": str(exc)})
    except (OSError, json.JSONDecodeError) as exc: return jsonify({"available": False, "reason": str(exc)})


def _latest_intraday_prediction_csv():
    """Return the newest non-invalidated intraday prediction CSV and manifest.

    Timing snapshots may live in ``runs/intraday`` while their model output is
    kept in a separate ``intraday_live_*`` directory.  Looking up the source
    directly keeps the analytics endpoint useful even when the timing adapter
    has not been refreshed yet.
    """
    candidates = []
    for path in RUN_ROOT.glob("intraday_live_*/intraday_next_session_predictions.csv"):
        manifest_path = path.with_name("manifest.json")
        manifest = {}
        try:
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("invalidated"):
                continue
            candidates.append((path.stat().st_mtime, path, manifest))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    if not candidates:
        return None, {}
    _, path, manifest = sorted(candidates, key=lambda item: item[0])[-1]
    return path, manifest


def _intraday_panel_path():
    """Choose the latest canonical intraday panel.

    Experimental EM/Tushare panels are deliberately excluded: they may have
    partial coverage or different volume units and must never silently feed a
    user-facing market curve.
    """
    root = project_path("finetune_lora", "data")
    paths = []
    for path in root.glob("astock_30min_ohlcva_full_*.parquet"):
        name = path.name.lower()
        if "_em_" in name or "_tushare_" in name or "backfill" in name:
            continue
        match = re.search(r"full_(\d{8})", path.name)
        if match:
            paths.append((match.group(1), path.stat().st_mtime, path))
    return sorted(paths, key=lambda item: (item[0], item[1]))[-1][2] if paths else None


def _intraday_industry_ranking(csv_path: Path):
    """Aggregate model predictions by industry without truncating to top-k."""
    buckets = {}
    total = 0
    observed = set()
    target = None
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            industry = (row.get("industry") or "未知").strip() or "未知"
            bucket = buckets.setdefault(industry, {"industry": industry, "returns": [], "last": [], "std": [], "positive": 0, "stocks": []})
            try:
                value = float(row.get("pred_return", ""))
            except (TypeError, ValueError):
                value = None
            if value is not None and math.isfinite(value):
                bucket["returns"].append(value)
                if value > 0: bucket["positive"] += 1
            try:
                value = float(row.get("pred_return_last", ""))
                if math.isfinite(value): bucket["last"].append(value)
            except (TypeError, ValueError):
                pass
            try:
                value = float(row.get("pred_return_std", ""))
                if math.isfinite(value): bucket["std"].append(value)
            except (TypeError, ValueError):
                pass
            code = (row.get("ts_code") or "").strip()
            if code: bucket["stocks"].append({"ts_code": code, "name": row.get("name") or "", "pred_return": value})
            if row.get("observed_latest_bar"): observed.add(row["observed_latest_bar"])
            target = target or row.get("target_trade_date")
    ranking = []
    for bucket in buckets.values():
        values = bucket["returns"]
        if not values: continue
        stocks = sorted(bucket["stocks"], key=lambda item: item.get("pred_return") if item.get("pred_return") is not None else -float("inf"), reverse=True)
        ranking.append({
            "industry": bucket["industry"], "stock_count": len(stocks),
            "mean_pred_return": sum(values) / len(values),
            "median_pred_return": sorted(values)[len(values) // 2],
            "positive_ratio": bucket["positive"] / len(values),
            "mean_pred_return_last": (sum(bucket["last"]) / len(bucket["last"])) if bucket["last"] else None,
            "mean_pred_return_std": (sum(bucket["std"]) / len(bucket["std"])) if bucket["std"] else None,
            "top_stocks": stocks[:5],
        })
    ranking.sort(key=lambda item: (item["mean_pred_return"], item["positive_ratio"]), reverse=True)
    for idx, item in enumerate(ranking, 1): item["rank"] = idx
    return {"rows": ranking, "stock_count": total, "observed_latest_bars": sorted(observed), "target_trade_date": target}


def _intraday_market_curve(panel_path: Path, days: int = 5):
    """Build a normalized cross-sectional intraday market curve.

    Panels are sorted by symbol, so reading only their tail would miss most
    stocks.  Row-group statistics let us skip groups that cannot contain the
    recent window while keeping the request bounded to a few trading days.
    """
    try:
        import pyarrow.parquet as pq
        import pandas as pd
        pf = pq.ParquetFile(panel_path)
        ts_index = pf.schema.names.index("timestamps")
        max_values = []
        for i in range(pf.num_row_groups):
            stats = pf.metadata.row_group(i).column(ts_index).statistics
            if stats and stats.max is not None: max_values.append(stats.max)
        if not max_values: return {"available": False, "reason": "分时面板没有时间戳"}
        max_ts = pd.Timestamp(max(max_values))
        start_ts = max_ts - pd.Timedelta(days=max(1, int(days)))
        frames = []
        for i in range(pf.num_row_groups):
            stats = pf.metadata.row_group(i).column(ts_index).statistics
            if stats and stats.max is not None and pd.Timestamp(stats.max) < start_ts: continue
            table = pf.read_row_group(i, columns=["ts_code", "timestamps", "close"])
            frame = table.to_pandas()
            frame["timestamps"] = pd.to_datetime(frame["timestamps"], errors="coerce")
            frame = frame[(frame["timestamps"] >= start_ts) & (frame["timestamps"] <= max_ts)]
            if not frame.empty: frames.append(frame)
        if not frames: return {"available": False, "reason": "最近窗口没有分时数据"}
        frame = pd.concat(frames, ignore_index=True).dropna(subset=["ts_code", "timestamps", "close"])
        frame = frame[frame["close"] > 0].sort_values(["ts_code", "timestamps"])
        frame["base"] = frame.groupby("ts_code")["close"].transform("first")
        frame["normalized"] = frame["close"] / frame["base"] * 100.0
        frame["ret"] = frame["close"] / frame["base"] - 1.0
        grouped = frame.groupby("timestamps", sort=True).agg(
            value=("normalized", "mean"), breadth=("ret", lambda values: float((values > 0).mean())),
            stock_count=("ts_code", "nunique"), close_mean=("close", "mean"),
        ).reset_index()
        points = [{"timestamp": row.timestamps.isoformat(), "value": float(row.value), "breadth": float(row.breadth), "stock_count": int(row.stock_count), "close_mean": float(row.close_mean)} for row in grouped.itertuples()]
        return {"available": True, "points": points, "as_of": max_ts.isoformat(), "window_start": start_ts.isoformat(), "stock_count": int(frame["ts_code"].nunique()), "bar_count": len(points), "panel": str(panel_path)}
    except Exception as exc:
        # The Flask service is intentionally lightweight and may not have
        # pyarrow installed.  Delegate the bounded parquet read to the GPU
        # runtime, which is already the canonical intraday environment.
        try:
            py = os.getenv("STOCKLAB_INTRADAY_PYTHON", sys.executable)
            code = ("import json,sys,pandas as pd; p=sys.argv[1]; d=pd.read_parquet(p,columns=['timestamps','close']); "
                    "d['timestamps']=pd.to_datetime(d['timestamps'],errors='coerce'); d['close']=pd.to_numeric(d['close'],errors='coerce'); "
                    "d=d.dropna().sort_values('timestamps'); g=d.groupby('timestamps')['close'].median().tail(32); "
                    "b=float(g.iloc[0]) if len(g) else 1.0; print(json.dumps({'points':[{'timestamp':str(k),'value':float(v/b-1.0)} for k,v in g.items()], 'as_of':str(g.index[-1]) if len(g) else None},default=str))")
            proc = subprocess.run([py, "-c", code, str(panel_path)], capture_output=True, text=True, timeout=180)
            if proc.returncode == 0:
                result = json.loads(proc.stdout)
                result.update(available=bool(result.get("points")), panel=str(panel_path), fallback_runtime=py)
                return result
        except Exception as fallback_exc:
            return {"available": False, "reason": f"{exc}; fallback: {fallback_exc}", "panel": str(panel_path)}
        return {"available": False, "reason": str(exc), "panel": str(panel_path)}


def _intraday_analytics():
    global _intraday_analytics_cache
    if _intraday_analytics_cache and time.time() - _intraday_analytics_cache[0] < 120:
        return _intraday_analytics_cache[1]
    csv_path, manifest = _latest_intraday_prediction_csv()
    if not csv_path:
        return {"available": False, "reason": "暂无有效分时模型预测，先运行分时推理"}
    try:
        industry = _intraday_industry_ranking(csv_path)
        panel = _intraday_panel_path()
        curve = _intraday_market_curve(panel) if panel else {"available": False, "reason": "暂无规范分时面板"}
        # Keep both structured forms for API consumers and flat arrays for the
        # browser visual layer.  The values are all derived from the same
        # current panel/prediction snapshot and carry an explicit as-of time.
        result = {"available": True, "generated_at": now(),
                  "asof": manifest.get("observed_latest_bar") or (industry.get("observed_latest_bars") or [None])[-1],
                  "source": {"predictions": str(csv_path), "manifest": manifest, "observed_latest_bar": manifest.get("observed_latest_bar") or (industry.get("observed_latest_bars") or [None])[-1]},
                  "industry_ranking": industry, "industry_rankings": industry.get("rows", []),
                  "market_curve": curve, "market_curve_points": curve.get("points", [])}
        _intraday_analytics_cache = (time.time(), clean_json(result))
        return _intraday_analytics_cache[1]
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


@app.get("/api/intraday/analytics")
def intraday_analytics_api():
    return jsonify(_intraday_analytics())


@app.get("/api/intraday/industry-ranking")
def intraday_industry_ranking_api():
    result = _intraday_analytics()
    return jsonify({"available": result.get("available", False), "source": result.get("source", {}), "industry_ranking": result.get("industry_ranking", {}), "reason": result.get("reason")})


@app.get("/api/intraday/market-curve")
def intraday_market_curve_api():
    result = _intraday_analytics()
    return jsonify({"available": result.get("available", False), "source": result.get("source", {}), "market_curve": result.get("market_curve", {}), "reason": result.get("reason")})


_HISTORY_TOOLS_DIR = Path(__file__).resolve().parent


def _history_fingerprint() -> str:
    """Inputs that change the history tool outputs (live CSV, OOS parquet, panel)."""
    parts = []
    try:
        csvs = sorted(RUN_ROOT.glob("intraday_live_*/intraday_next_session_predictions.csv"), key=lambda p: p.stat().st_mtime)
        if csvs:
            parts.append(f"csv={csvs[-1].stat().st_mtime:.0f}")
    except OSError:
        pass
    try:
        oos = sorted(project_path("finetune_lora", "output").glob("oos_intraday*/summary_lora/oos_predictions_with_trade_filter.parquet"), key=lambda p: p.stat().st_mtime)
        if oos:
            parts.append(f"oos={oos[-1].stat().st_mtime:.0f}")
    except OSError:
        pass
    try:
        panel = _intraday_panel_path()
        if panel:
            parts.append(f"panel={Path(panel).stat().st_mtime:.0f}")
    except Exception:
        pass
    for tool in ("intraday_ic_history.py", "market_volume_history.py"):
        tp = _HISTORY_TOOLS_DIR / tool
        try:
            parts.append(f"{tool}={int(tp.stat().st_mtime)}")
        except OSError:
            pass
    return "|".join(parts) or "none"


def _run_history_tool(script: str, cache_name: str, timeout: int = 300):
    """Run an aloha-python history tool, memoised on disk by input fingerprint."""
    py = os.getenv("STOCKLAB_HISTORY_PYTHON", sys.executable)
    cache_path = RUN_ROOT / cache_name
    fingerprint = _history_fingerprint()
    try:
        blob = json.loads(cache_path.read_text(encoding="utf-8"))
        if blob.get("fingerprint") == fingerprint and isinstance(blob.get("payload"), dict):
            return blob["payload"]
    except Exception:
        pass
    try:
        proc = subprocess.run([py, str(_HISTORY_TOOLS_DIR / script), "--runs-dir", str(RUN_ROOT)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "历史统计工具超时"}
    except OSError as exc:
        return {"available": False, "reason": f"无法执行历史统计工具: {exc}"}
    if proc.returncode != 0:
        return {"available": False, "reason": (proc.stderr or proc.stdout or "工具失败")[-400:]}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"available": False, "reason": f"工具输出解析失败: {exc}"}
    try:
        cache_path.write_text(json.dumps({"fingerprint": fingerprint, "payload": payload}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return payload


@app.get("/api/intraday/ic-trend")
def intraday_ic_trend_api():
    return jsonify(_run_history_tool("intraday_ic_history.py", "intraday_ic_cache.json", timeout=300))


@app.get("/api/market/volume-history")
def market_volume_history_api():
    return jsonify(_run_history_tool("market_volume_history.py", "market_volume_cache.json", timeout=240))

@app.post("/api/intraday/timing")
def run_intraday_timing():
    """Generate a current timing snapshot and append its change audit."""
    script = ROOT / "intraday_timing.py"
    if not script.exists(): return jsonify({"available": False, "reason": "分时时机脚本不可用"}), 503
    py = os.getenv("STOCKLAB_INTRADAY_PYTHON", sys.executable)
    if not Path(py).exists(): py = os.getenv("STOCKLAB_PYTHON", sys.executable)
    args = [py, str(script), "--project-root", SERVER_ROOT, "--output-dir", str(RUN_ROOT / "intraday")]
    # Prefer the newest live model CSV.  The standalone endpoint is used by
    # the UI to refresh timing/positions without rerunning the expensive model.
    live_csvs = []
    for csv_path in RUN_ROOT.glob("intraday_live_*/intraday_next_session_predictions.csv"):
        try:
            if json.loads(csv_path.with_name("manifest.json").read_text(encoding="utf-8")).get("invalidated"): continue
        except (OSError, ValueError): continue
        live_csvs.append(csv_path)
    live_csvs.sort(key=lambda p: p.stat().st_mtime)
    if live_csvs: args += ["--predictions", str(live_csvs[-1])]
    if INTRADAY_POSITIONS.exists(): args += ["--positions", str(INTRADAY_POSITIONS)]
    date = request.args.get("date", "").strip()
    if date: args += ["--date", date]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0: return jsonify({"available": False, "reason": (proc.stderr or proc.stdout)[-1000:]}), 503
        return jsonify(json.loads(proc.stdout))
    except Exception as exc:
        return jsonify({"available": False, "reason": str(exc)}), 503


def _load_paper_account() -> dict:
    """Single source of truth for the paper account; migrates the legacy flat file."""
    if PAPER_ACCOUNT.exists():
        try:
            acct = json.loads(PAPER_ACCOUNT.read_text(encoding="utf-8"))
            if isinstance(acct, dict) and "capital" in acct:
                acct.setdefault("positions", []); acct.setdefault("trades", [])
                return acct
        except (OSError, json.JSONDecodeError):
            pass
    positions = []
    if INTRADAY_POSITIONS.exists():
        try: positions = json.loads(INTRADAY_POSITIONS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): positions = []
    positions = [p for p in positions if isinstance(p, dict) and p.get("ts_code")]
    cost = sum(float(p.get("entry_px") or 0) * int(p.get("shares") or 0) for p in positions)
    acct = {"capital": DEFAULT_PAPER_CAPITAL, "cash": round(max(DEFAULT_PAPER_CAPITAL - cost, 0.0), 2),
            "positions": positions, "trades": [], "created_at": now(), "updated_at": now()}
    _save_paper_account(acct)
    return acct


def _save_paper_account(acct: dict) -> None:
    acct["updated_at"] = now()
    PAPER_ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    PAPER_ACCOUNT.write_text(json.dumps(acct, ensure_ascii=False, indent=2), encoding="utf-8")
    # mirror the open positions so the timing adapter and legacy readers keep working
    INTRADAY_POSITIONS.write_text(json.dumps(acct.get("positions", []), ensure_ascii=False, indent=2), encoding="utf-8")


def _paper_stats(acct: dict) -> dict:
    trades = acct.get("trades") or []
    wins = [t for t in trades if float(t.get("pnl") or 0) > 0]
    losses = [t for t in trades if float(t.get("pnl") or 0) <= 0]
    gp = sum(float(t.get("pnl") or 0) for t in wins)
    gl = abs(sum(float(t.get("pnl") or 0) for t in losses))
    return {"n_trades": len(trades), "n_wins": len(wins), "n_losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "total_pnl": round(sum(float(t.get("pnl") or 0) for t in trades), 2),
            "avg_win": round(gp / len(wins), 2) if wins else None,
            "avg_loss": round(-gl / len(losses), 2) if losses else None,
            "profit_factor": round(gp / gl, 3) if gl > 0 else None}


def _timing_marks() -> dict:
    """ts_code -> {last_price, name, action, return, ema_signal} from the newest timing snapshot."""
    try:
        files = sorted(list(RUN_ROOT.glob("intraday*/intraday_timing_*.json")) + list(RUN_ROOT.glob("intraday/intraday_timing_*.json")), key=lambda p: p.stat().st_mtime)
        if not files:
            return {}
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        rows = data.get("positions") or data.get("result", {}).get("positions") or []
        return {str(r.get("ts_code")): r for r in rows if isinstance(r, dict) and r.get("ts_code")}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _paper_overview(acct: dict) -> dict:
    marks = _timing_marks()
    positions = []
    for pos in acct.get("positions", []):
        row = dict(pos)
        mark = marks.get(str(pos.get("ts_code"))) or {}
        last = float(mark.get("last_price") or 0) or float(pos.get("entry_px") or 0)
        row["name"] = mark.get("name") or pos.get("name")
        row["last_price"] = last
        row["action"] = mark.get("action") or "HOLD"
        row["return"] = (last / float(pos.get("entry_px")) - 1) if float(pos.get("entry_px") or 0) > 0 else None
        row["market_value"] = round(last * int(pos.get("shares") or 0), 2)
        row["float_pnl"] = round((last - float(pos.get("entry_px") or 0)) * int(pos.get("shares") or 0), 2)
        positions.append(row)
    market_value = round(sum(float(x["market_value"]) for x in positions), 2)
    return {"capital": round(float(acct.get("capital") or 0), 2), "cash": round(float(acct.get("cash") or 0), 2),
            "positions": positions, "trades": acct.get("trades", []), "stats": _paper_stats(acct),
            "market_value": market_value, "total_asset": round(float(acct.get("cash") or 0) + market_value, 2),
            "asof": (marks and next(iter(marks.values())).get("asof")) or None}


@app.get("/api/intraday/account")
def intraday_account_api():
    return jsonify({"available": True, "account": _paper_overview(_load_paper_account())})


@app.put("/api/intraday/account")
def intraday_account_update_api():
    """Set capital, or hard-reset the whole paper account."""
    body = request.get_json(silent=True) or {}
    try: capital = float(body.get("capital", DEFAULT_PAPER_CAPITAL))
    except (TypeError, ValueError): capital = 0
    if capital <= 0:
        return jsonify({"available": False, "reason": "本金需为正数"}), 400
    if body.get("reset"):
        acct = {"capital": capital, "cash": capital, "positions": [], "trades": [], "created_at": now(), "updated_at": now()}
    else:
        acct = _load_paper_account()
        used = sum(float(p.get("entry_px") or 0) * int(p.get("shares") or 0) for p in acct.get("positions", []))
        acct["capital"] = capital
        acct["cash"] = round(capital - used, 2)
        if acct["cash"] < 0:
            return jsonify({"available": False, "reason": f"本金低于持仓成本 {used:.0f} 元，先平仓或调高本金"}), 400
    _save_paper_account(acct)
    return jsonify({"available": True, "account": _paper_overview(acct), "message": "账户已重置" if body.get("reset") else f"本金已设为 {capital:.0f} 元"})


@app.post("/api/intraday/positions")
def add_intraday_position():
    """Create/update a paper position; no broker order is ever submitted."""
    body = request.get_json(silent=True) or {}
    code = str(body.get("ts_code", body.get("symbol", ""))).strip().upper()
    try:
        shares = int(float(body.get("shares", 0))); entry_px = float(body.get("entry_px", body.get("entry_price", 0)))
    except (TypeError, ValueError): shares, entry_px = 0, 0
    entry_day = str(body.get("entry_day", body.get("entry_date", ""))).replace("/", "-")[:10]
    if not code or shares <= 0 or entry_px <= 0 or len(entry_day) != 10:
        return jsonify({"available": False, "reason": "需要 ts_code、shares、entry_price、entry_date"}), 400
    acct = _load_paper_account()
    marks = _timing_marks()
    cost = entry_px * shares
    old = next((p for p in acct["positions"] if p.get("ts_code") == code), None)
    if old: acct["cash"] = round(float(acct.get("cash") or 0) + float(old.get("entry_px") or 0) * int(old.get("shares") or 0), 2)
    if cost > float(acct.get("cash") or 0):
        return jsonify({"available": False, "reason": f"现金不足：需 {cost:,.0f} 元，可用 {float(acct.get('cash') or 0):,.0f} 元（可在纸上账户调整本金）"}), 400
    acct["cash"] = round(float(acct.get("cash") or 0) - cost, 2)
    acct["positions"] = [p for p in acct["positions"] if p.get("ts_code") != code]
    acct["positions"].append({"id": uuid.uuid4().hex[:12], "ts_code": code, "name": marks.get(code, {}).get("name"),
                              "shares": shares, "entry_px": entry_px, "entry_day": entry_day, "created_at": now()})
    _save_paper_account(acct)
    return jsonify({"available": True, "account": _paper_overview(acct), "message": "已记录纸上持仓（现金已扣减）；下一次时机刷新将计算卖出提醒。"})


@app.delete("/api/intraday/positions/<position_id>")
def close_intraday_position(position_id):
    """Cancel a mis-entered position: refund cash, no trade record."""
    acct = _load_paper_account()
    pos = next((p for p in acct["positions"] if p.get("id") == position_id), None)
    if not pos:
        return jsonify({"available": False, "reason": "仓位不存在或已平仓"}), 404
    acct["cash"] = round(float(acct.get("cash") or 0) + float(pos.get("entry_px") or 0) * int(pos.get("shares") or 0), 2)
    acct["positions"] = [p for p in acct["positions"] if p.get("id") != position_id]
    _save_paper_account(acct)
    return jsonify({"available": True, "account": _paper_overview(acct), "message": f"已取消 {pos['ts_code']} 并退回现金（不计盈亏）"})


@app.post("/api/intraday/positions/<position_id>/sell")
def sell_intraday_position(position_id):
    """Sell to close: move to the trade log with PnL, and credit cash back."""
    body = request.get_json(silent=True) or {}
    acct = _load_paper_account()
    pos = next((p for p in acct["positions"] if p.get("id") == position_id), None)
    if not pos:
        return jsonify({"available": False, "reason": "仓位不存在或已平仓"}), 404
    try: exit_px = float(body.get("price") or 0)
    except (TypeError, ValueError): exit_px = 0
    if exit_px <= 0:
        exit_px = float(_timing_marks().get(str(pos.get("ts_code")), {}).get("last_price") or 0)
    if exit_px <= 0:
        exit_px = float(pos.get("entry_px") or 0)
    if exit_px <= 0:
        return jsonify({"available": False, "reason": "无可用卖价，请在请求中填写 price"}), 400
    shares = int(pos.get("shares") or 0)
    entry_px = float(pos.get("entry_px") or 0)
    cost = entry_px * shares
    pnl = (exit_px - entry_px) * shares
    trade = {"id": pos.get("id"), "ts_code": pos.get("ts_code"), "name": pos.get("name"),
             "shares": shares, "entry_px": entry_px, "exit_px": exit_px,
             "entry_day": pos.get("entry_day"), "exit_day": now()[:10],
             "pnl": round(pnl, 2), "ret": round(pnl / cost, 6) if cost > 0 else None,
             "reason": str(body.get("reason") or "手动平仓"), "closed_at": now()}
    acct["positions"] = [p for p in acct["positions"] if p.get("id") != position_id]
    acct["trades"] = [trade] + acct.get("trades", [])
    acct["cash"] = round(float(acct.get("cash") or 0) + exit_px * shares, 2)
    _save_paper_account(acct)
    return jsonify({"available": True, "account": _paper_overview(acct), "trade": trade, "stats": _paper_stats(acct),
                    "message": f"已平仓 {pos.get('ts_code')} @ {exit_px:.2f}，盈亏 {pnl:+,.2f} 元（{trade['ret'] * 100:+.2f}%）" if trade["ret"] is not None else f"已平仓 {pos.get('ts_code')} @ {exit_px:.2f}"})

def _snapshot_cli(mode: str, symbol: str = "", limit: int = 100, date: str = "", industry: str = ""):
    cache_key = (mode, symbol, int(limit), date, industry)
    cached = _snapshot_cache.get(cache_key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    script = ROOT / "market_snapshot.py"
    if not script.exists():
        raise FileNotFoundError(script)
    py = os.getenv("STOCKLAB_MARKET_PYTHON", sys.executable)
    if not Path(py).exists():
        py = os.getenv("STOCKLAB_PYTHON", sys.executable)
    argv = [py, str(script), "--project-root", SERVER_ROOT, "--mode", mode, "--limit", str(max(1, min(int(limit), 200)))]
    if symbol: argv += ["--symbol", symbol]
    if date: argv += ["--date", date.replace("-", "")]
    if industry: argv += ["--industry", industry]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "market snapshot failed").strip()[-1000:])
    result = json.loads(proc.stdout)
    _snapshot_cache[cache_key] = (time.time(), result)
    return result


@app.get("/api/market_snapshot")
def market_snapshot_api():
    mode = request.args.get("mode", "snapshot")
    if mode not in {"snapshot", "stock"}:
        return jsonify({"available": False, "reason": "mode must be snapshot or stock"}), 400
    try:
        data = _snapshot_cli(mode, request.args.get("symbol", "").strip().upper(), int(request.args.get("limit", 100)), request.args.get("date", ""), request.args.get("industry", ""))
        return jsonify(data)
    except Exception as exc:
        return jsonify({"available": False, "reason": str(exc)}), 503


@app.get("/api/visuals/market")
def visuals_market():
    try:
        data = _snapshot_cli("stock", request.args.get("symbol", "").strip().upper(), 90)
        return jsonify({"available": True, "symbol": data.get("symbol"), "as_of": data.get("signal_date"), "rows": data.get("ohlc", [])})
    except Exception as exc:
        return jsonify({"available": False, "reason": str(exc)}), 503


@app.get("/api/visuals/factors")
def visuals_factors():
    try:
        data = _snapshot_cli("stock", request.args.get("symbol", "").strip().upper(), 90)
        horizon = data.get("horizon", [])
        return jsonify({"available": True, "symbol": data.get("symbol"), "signal_date": data.get("signal_date"), "kronos": [x.get("score") for x in horizon], "factor_history": data.get("factor_history", [])})
    except Exception as exc:
        return jsonify({"available": False, "reason": str(exc)}), 503


if __name__ == "__main__":
    app.run(host=os.getenv("STOCKLAB_HOST", "0.0.0.0"), port=int(os.getenv("STOCKLAB_PORT", "7070")), debug=False)
