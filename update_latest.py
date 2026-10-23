#!/usr/bin/env python3
"""Choose the newest safe data targets for the QuantWeave refresh.

The UI should not require a user to type a date.  This command is deliberately
data-only: while the market is open it refreshes completed 30-minute bars,
and at every time of day it catches up all missing completed daily sessions.  A
machine-readable status is written beside the canonical panels so the UI can
show exactly what is current, deferred, or partially unavailable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path


def _date_from_name(path: Path) -> dt.date | None:
    matches = re.findall(r"(?<!\d)(\d{8})(?!\d)", path.name)
    for value in reversed(matches):
        try:
            return dt.datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            continue
    return None


def _load_calendar(calendar_dir: Path) -> dict[dt.date, bool]:
    """Read explicit exchange sessions, including integer YYYYMMDD columns.

    Unknown weekdays are never treated as open: doing so would fabricate
    sessions on holidays when a cache is missing or outside its date range.
    Newer cache files win where cached ranges overlap.
    """
    import pandas as pd

    sessions: dict[dt.date, bool] = {}
    files = sorted(calendar_dir.glob("SSE_*_*.parquet"), key=lambda p: p.stat().st_mtime)
    for path in files:
        try:
            frame = pd.read_parquet(path)
            date_col = next((c for c in ("cal_date", "trade_date", "date") if c in frame), None)
            open_col = next((c for c in ("is_open", "is_trading", "open") if c in frame), None)
            if date_col is None or open_col is None:
                continue
            for value, is_open in zip(frame[date_col], frame[open_col]):
                # Passing integer 20260917 straight to pandas.to_datetime
                # parses nanoseconds since 1970 instead of a calendar date.
                text = str(value).strip()
                if re.fullmatch(r"\d{8}(?:\.0)?", text):
                    parsed = dt.datetime.strptime(text[:8], "%Y%m%d").date()
                else:
                    parsed = pd.to_datetime(value, errors="coerce")
                    if pd.isna(parsed):
                        continue
                    parsed = parsed.date()
                flag = str(is_open).strip().lower()
                if flag in {"0", "0.0", "false", "1", "1.0", "true"}:
                    sessions[parsed] = flag in {"1", "1.0", "true"}
        except (OSError, ValueError, TypeError, ImportError):
            continue
    return sessions


def _trading_day(day: dt.date, sessions: dict[dt.date, bool]) -> bool | None:
    if day in sessions:
        return sessions[day]
    if day.weekday() >= 5:
        return False
    return None


def _is_trading_day(day: dt.date, calendar_dir: Path) -> bool | None:
    return _trading_day(day, _load_calendar(calendar_dir))


def _latest_daily_panel(project: Path) -> Path | None:
    panels = project / "factor_horizon" / "shadow_live" / "data" / "panels"
    candidates = [p for p in panels.glob("astock_daily_ohlcva_*.parquet") if _date_from_name(p)]
    # This is the same seed used by update_data.sh before a live panel exists.
    seed = project / "outputs" / "20260902_official_eval" / "data" / "astock_daily_ohlcva_20260902.parquet"
    if seed.exists():
        candidates.append(seed)
    return max(candidates, key=_date_from_name) if candidates else None


def _daily_plan(now: dt.datetime, sessions: dict[dt.date, bool], latest: dt.date | None) -> dict:
    # The current session is eligible at 15:05, but yesterday's close must be
    # caught up during the next morning/session as well as on holidays.
    bound = now.date() if now.time().replace(tzinfo=None) >= dt.time(15, 5) else now.date() - dt.timedelta(days=1)
    completed = sorted(day for day, opened in sessions.items() if opened and day <= bound)
    target = completed[-1] if completed else None
    plan = {"target": target.strftime("%Y%m%d") if target else None,
            "latest_panel_date": latest.strftime("%Y%m%d") if latest else None,
            "pending_dates": [], "missing_calendar_dates": []}
    if latest is None:
        return dict(plan, status="blocked", reason="没有可用的日线基准面板，无法安全补齐历史")
    if latest > bound:
        return dict(plan, status="blocked", reason="日线面板日期晚于最近已收盘日期，需检查面板")
    day = latest + dt.timedelta(days=1)
    gap = False
    while day <= bound:
        trading = _trading_day(day, sessions)
        if trading is None:
            plan["missing_calendar_dates"].append(day.strftime("%Y%m%d"))
            gap = True
        elif trading and not gap:
            plan["pending_dates"].append(day.strftime("%Y%m%d"))
        day += dt.timedelta(days=1)
    if plan["missing_calendar_dates"]:
        return dict(plan, status="blocked", reason="交易日历缺少日期，不能猜测交易日或跨过缺口")
    return dict(plan, status="planned" if plan["pending_dates"] else "up_to_date")


def _refresh_daily(plan: dict, app_root: Path, plan_only: bool) -> dict:
    """Catch up oldest first: the single-date fetch helper cannot fill gaps.

    Stop on an unavailable/failed date. Earlier successful dates remain saved,
    so a late publication of today's close cannot hold older closes hostage.
    """
    result = dict(plan, attempts=[], completed_dates=[])
    if plan_only:
        return result
    for target in plan["pending_dates"]:
        print(f"[daily catch-up] 更新已收盘交易日 {target}", flush=True)
        rc, log = _run(["bash", str(app_root / "update_data.sh"), target], cwd=app_root)
        deferred = "DATA_UPDATE_DEFERRED" in log
        outcome = "deferred" if deferred else ("updated" if rc == 0 else "failed")
        result["attempts"].append({"target": target, "status": outcome, "returncode": rc, "log_tail": log})
        result.update(returncode=rc, log_tail=log)
        if outcome != "updated":
            result.update(status=outcome, blocked_at=target)
            return result
        result["completed_dates"].append(target)
        result["latest_panel_date"] = target
    if plan["status"] == "blocked":
        result["status"] = "blocked"
    elif result["completed_dates"]:
        result["status"] = "updated"
    return result


def _session_state(now: dt.datetime, trading: bool | None) -> str:
    if trading is None:
        return "calendar_unknown"
    if not trading:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "pre_open"
    if minutes < 15 * 60 + 5:
        return "intraday"
    return "after_close"


def _completed_cutoff(now: dt.datetime, day: dt.date) -> dt.datetime | None:
    if now.date() != day:
        return None
    # A 30-minute bar is considered usable at its endpoint.  At 10:12 the
    # latest complete bar is 10:00; at 13:31 it is 13:30.
    endpoints = [(10, 0), (10, 30), (11, 0), (11, 30), (13, 30), (14, 0), (14, 30), (15, 0)]
    available = [dt.datetime.combine(day, dt.time(h, m)) for h, m in endpoints if (h * 60 + m) <= now.hour * 60 + now.minute]
    return available[-1] if available else None


def _latest_panel(data_dir: Path) -> Path | None:
    files = [p for p in data_dir.glob("astock_30min_ohlcva_full_*.parquet")
             if "_em_" not in p.stem and "_tushare_" not in p.stem]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _run(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a refresh while forwarding progress to the control-plane log.

    The previous implementation captured stdout until the entire per-symbol
    refresh finished.  A 5,525-symbol run therefore looked frozen in the UI,
    and a broken data source could wait forever.  Keep a bounded tail for the
    status JSON, but stream each line and enforce a one-hour upper bound.
    """
    proc = subprocess.Popen(command, cwd=str(cwd) if cwd else None, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1)
    lines: list[str] = []
    selector = selectors.DefaultSelector()
    try:
        assert proc.stdout is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 3600
        while True:
            for _, _ in selector.select(timeout=1):
                line = proc.stdout.readline()
                if line:
                    line = line.rstrip("\n")
                    lines.append(line)
                    print(line, flush=True)
            if proc.poll() is not None:
                # Drain any final lines that were already buffered.
                lines.extend(x.rstrip("\n") for x in proc.stdout.readlines())
                break
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait()
                rc = 124
                lines.append("refresh timed out after 3600 seconds")
                print(lines[-1], flush=True)
                break
        if proc.returncode is not None and "rc" not in locals():
            rc = proc.returncode
    finally:
        selector.close()
        if proc.stdout is not None:
            proc.stdout.close()
    return rc, "\n".join(lines)[-4000:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--token-source", default="")
    ap.add_argument("--now", default="", help="测试用 ISO 时间（生产环境留空）")
    ap.add_argument("--plan-only", action="store_true", help="只输出自动计划，不请求数据")
    args = ap.parse_args()
    project = Path(args.project_root)
    app_root = Path(__file__).resolve().parent
    calendar = project / "factor_horizon" / "shadow_live" / "data" / "cache" / "trade_cal"
    data_dir = project / "finetune_lora" / "data"
    timezone = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(timezone)
    now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    day = now.date()
    sessions = _load_calendar(calendar)
    trading = _trading_day(day, sessions)
    state = _session_state(now, trading)
    cutoff = _completed_cutoff(now, day)
    status = {
        "status": "planned", "as_of": now.isoformat(timespec="seconds"),
        "session_date": day.strftime("%Y%m%d"), "trading_day": trading,
        "session_state": state, "calendar_status": "available" if trading is not None else "unknown",
        "intraday": {}, "daily": {},
    }

    # During the session, refresh only bars whose endpoint has passed.  This
    # avoids making a half-formed current bar look like model input.
    source = _latest_panel(data_dir)
    if trading and state == "intraday" and cutoff and source:
        output = data_dir / f"astock_30min_ohlcva_full_{day:%Y%m%d}_ui_parallel.parquet"
        updater = app_root / "update_intraday_market_parallel.py"
        command = [args.python, str(updater), "--input-panel", str(source), "--output-panel", str(output),
                   "--as-of-date", day.strftime("%Y%m%d"), "--scale", "30", "--datalen", "240",
                   "--workers", "8", "--retries", "4", "--cutoff", cutoff.strftime("%Y-%m-%d %H:%M:%S")]
        if args.plan_only:
            rc, log = 0, "plan-only"
        else:
            rc, log = _run(command)
        partial = False
        summary_path = output.with_suffix(".summary.json")
        if not args.plan_only and rc == 0 and summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                partial = bool(summary.get("failure_count")) or str(summary.get("max_timestamp", "")) < cutoff.isoformat(sep=" ")
            except (OSError, ValueError, TypeError):
                partial = True
        intraday_status = "planned" if args.plan_only else ("partial" if partial else ("updated" if rc == 0 else "failed"))
        status["intraday"] = {"status": intraday_status, "partial": partial, "cutoff": cutoff.isoformat(sep=" "),
                               "source_panel": str(source), "output_panel": str(output), "returncode": rc, "log_tail": log}
    elif state in {"pre_open", "intraday"}:
        status["intraday"] = {"status": "waiting", "reason": "暂无可更新的已完成 30 分钟 K 线" if not cutoff else "没有可用的基准分时面板"}
    elif state == "calendar_unknown":
        status["intraday"] = {"status": "blocked", "reason": "交易日历未覆盖今天，无法确定分时更新目标"}
    else:
        status["intraday"] = {"status": "skipped", "reason": "当前不在交易时段"}

    # Always catch up completed daily sessions, including before the open,
    # during today's session and on non-trading days. Intraday stays first.
    daily_panel = _latest_daily_panel(project)
    plan = _daily_plan(now, sessions, _date_from_name(daily_panel) if daily_panel else None)
    status["daily"] = _refresh_daily(plan, app_root, args.plan_only)

    incomplete = any(part.get("status") in {"failed", "partial", "blocked", "deferred"}
                     for part in (status["intraday"], status["daily"]))
    status["status"] = "planned" if args.plan_only else ("partial" if incomplete else "completed")
    # Validation must never erase a real refresh result or make a planned
    # cutoff look like data that was actually fetched.
    if not args.plan_only:
        out = project / "factor_horizon" / "shadow_live" / "data" / "update_status.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(".tmp")
        temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(out)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["status"] != "partial" else 1


if __name__ == "__main__":
    raise SystemExit(main())
