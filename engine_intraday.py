"""Live Kronos #29 (ctx160/pr16) next-session inference.

This module deliberately separates live inference from the leak-free OOS
evaluator: only the most recent 160 observed bars per instrument are used and
no future labels are required.  It can be imported by the UI backend or run as
``python engine_intraday.py --output-dir ...`` on the GPU host.

Instruments are independent of each other, so ``--max-gpus N`` splits the
candidate list across the N GPUs with the most free memory.  The parent builds
the context records once, one subprocess per GPU scores its interleaved shard,
and the final ranked CSV is published only when every shard succeeds.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import pickle
import subprocess
import sys
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

FINETUNE_ROOT = os.environ.get(
    "KRONOS_FINETUNE_ROOT",
    "/opt/kronos/finetune_lora",
)
if FINETUNE_ROOT not in sys.path:
    sys.path.insert(0, FINETUNE_ROOT)

try:
    from signal_eval import FEATURES, CLOSE_IDX, load_model, preprocess_context, forecast, set_seed
except ImportError as exc:  # helpful when the module is imported on a laptop
    raise ImportError("Run on the Kronos host or set KRONOS_FINETUNE_ROOT") from exc

DEFAULT_PANEL = os.path.join(FINETUNE_ROOT, "data/astock_30min_ohlcva_full_20260912_parallel.parquet")
DEFAULT_CHECKPOINT = "/opt/kronos/ckpts/lora_intraday30_ctx160_pr16_cutoff20260901_full_20260912/checkpoints/best_lora.pt"
DEFAULT_STOCK_BASIC = "/opt/kronos/factor_horizon/shadow_live/data/stock_basic_all.parquet"
DEFAULT_CALENDAR_DIR = os.environ.get("KRONOS_TRADE_CAL_DIR", "/opt/kronos/factor_horizon/shadow_live/data/cache/trade_cal")
MARKET_TIMES = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
GPU_FREE_GATE = 16 * 1024 * 1024 * 1024


def _trading_days(calendar_dir: str | os.PathLike | None = DEFAULT_CALENDAR_DIR) -> list[pd.Timestamp]:
    """Read the project SSE calendar; filenames are SSE_YYYYMMDD_YYYYMMDD.parquet.

    The live panel can be ahead of the checked-in calendar, so a best-effort
    fallback to weekday dates is retained when the cache is unavailable.
    """
    days: set[pd.Timestamp] = set()
    root = Path(calendar_dir) if calendar_dir else None
    if root and root.exists():
        for f in root.glob("SSE_*_*.parquet"):
            m = re.search(r"SSE_(\d{8})_\d{8}\.parquet$", f.name)
            if m:
                try:
                    days.add(pd.Timestamp(datetime.strptime(m.group(1), "%Y%m%d").date()))
                except ValueError:
                    pass
    return sorted(days)


def _next_trading_days(ts: pd.Timestamp, count: int, calendar_dir: str | os.PathLike | None = DEFAULT_CALENDAR_DIR) -> list[pd.Timestamp]:
    base = ts.normalize()
    days = [d for d in _trading_days(calendar_dir) if d > base]
    if len(days) >= count:
        return days[:count]
    # Calendar caches are sometimes only topped up through the last observed
    # day. Extend from the last known date using the weekday rule.
    cursor = days[-1] if days else base
    while len(days) < count:
        cursor = cursor + pd.Timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor += pd.Timedelta(days=1)
        if cursor not in days:
            days.append(cursor)
    return days[:count]


def _next_weekday(ts: pd.Timestamp) -> pd.Timestamp:
    """Backward-compatible one-session helper using the project calendar."""
    return _next_trading_days(ts, 1)[0]

def _visible_gpus() -> list[tuple[int, int, int]]:
    """Return (free, idx, total) for every CUDA device, sorted by free memory."""
    available = []
    for idx in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(idx)
        available.append((int(free), idx, int(total)))
    return sorted(available, reverse=True)


def _auto_device(wait_seconds: int = 0, poll_seconds: int = 15) -> str:
    """Pick the GPU with the most free memory; utilization is advisory only.

    Shared inference cards can report high utilization while still having a
    large allocation available.  The 16GiB admission gate is not a measured
    peak for every batch size; batch limits and OOM recovery apply below.
    """
    if not torch.cuda.is_available():
        raise RuntimeError("当前 Python 环境无法使用 CUDA；请检查 GPU 环境或显式选择 device=cpu")
    deadline = time.monotonic() + max(0, int(wait_seconds))
    while True:
        try:
            available = [(free, idx, total) for free, idx, total in _visible_gpus() if free >= GPU_FREE_GATE]
            if available:
                free, idx, total = available[0]
                print(json.dumps({"stage": "gpu_selected", "gpu": int(idx), "free_mib": int(free // (1024 * 1024)), "total_mib": int(total // (1024 * 1024)), "selection": "max_free_memory"}, ensure_ascii=False), flush=True)
                return f"cuda:{idx}"
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"无法读取 CUDA GPU 显存：{exc}") from exc
        if time.monotonic() >= deadline:
            raise RuntimeError("暂无可用 GPU（需至少 16GiB 空闲显存）；请稍后重试或显式选择 device=cpu")
        remaining = max(1, int(deadline - time.monotonic()))
        print(json.dumps({"stage":"gpu_wait","remaining_seconds":remaining,"reason":"all_visible_gpus_below_16GiB_free"},ensure_ascii=False),flush=True)
        time.sleep(min(max(1, int(poll_seconds)), remaining))


def _auto_devices(max_gpus: int, wait_seconds: int = 0, poll_seconds: int = 15) -> list[int]:
    """Pick up to ``max_gpus`` GPUs (most free memory first) for shard workers.

    Fewer eligible cards simply means fewer shards; the run only fails when no
    card passes the 16GiB gate within ``wait_seconds``.
    """
    if not torch.cuda.is_available():
        raise RuntimeError("当前 Python 环境无法使用 CUDA；请检查 GPU 环境或显式选择 device=cpu")
    wanted = max(1, int(max_gpus))
    deadline = time.monotonic() + max(0, int(wait_seconds))
    while True:
        try:
            eligible = [(free, idx, total) for free, idx, total in _visible_gpus() if free >= GPU_FREE_GATE]
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"无法读取 CUDA GPU 显存：{exc}") from exc
        chosen = eligible[:wanted]
        if len(chosen) >= 1:
            if len(chosen) < wanted:
                print(json.dumps({"stage": "gpu_shards_reduced", "requested": wanted, "eligible": len(chosen),
                                  "reason": "fewer GPUs above the 16GiB free gate; running with what is free"}, ensure_ascii=False), flush=True)
            print(json.dumps({"stage": "gpus_selected", "gpus": [idx for _, idx, _ in chosen],
                              "free_mib": [int(free // (1024 * 1024)) for free, _, _ in chosen],
                              "selection": "top_free_memory"}, ensure_ascii=False), flush=True)
            return [idx for _, idx, _ in chosen]
        if time.monotonic() >= deadline:
            raise RuntimeError("暂无可用 GPU（需至少 16GiB 空闲显存）；请稍后重试或显式选择 device=cpu")
        remaining = max(1, int(deadline - time.monotonic()))
        print(json.dumps({"stage": "gpu_wait", "remaining_seconds": remaining, "reason": "all_visible_gpus_below_16GiB_free"}, ensure_ascii=False), flush=True)
        time.sleep(min(max(1, int(poll_seconds)), remaining))


def _recommended_batch_size(device: torch.device, requested: int) -> int:
    """Choose a conservative batch from the current free-memory snapshot."""
    requested = max(1, int(requested))
    if device.type != "cuda":
        return requested
    free, total = torch.cuda.mem_get_info(device)
    free_mib = int(free / (1024 * 1024))
    if free < GPU_FREE_GATE:
        raise RuntimeError(f"GPU 显存已变化，当前仅 {free_mib}MiB 空闲，需至少 16GiB；请重试自动选卡")
    # Limit this process's torch allocator using the current free snapshot.
    # This leaves headroom at launch, not a reservation against other jobs.
    budget = min(free - 4 * 1024**3, 24 * 1024**3)
    torch.cuda.set_per_process_memory_fraction(float(budget / total), device)
    print(json.dumps({"stage": "gpu_budget", "free_mib": free_mib,
                      "allocator_budget_mib": int(budget / 1024**2), "headroom_mib": 4096}), flush=True)
    if free_mib < 20000:
        chosen = 128
    elif free_mib < 30000:
        chosen = 256
    else:
        chosen = 512
    return min(requested, chosen)


def _forecast_batch(tokenizer, model, batch, lookback, predict, clip, max_context,
                    sample_count, temperature, top_k, top_p, greedy, device):
    """Forecast one batch so failed tensors do not remain in the caller frame."""
    x, xs, ys, means, stds = preprocess_context(batch, lookback - 1, predict, clip, "calendar")
    pred = forecast(tokenizer, model, x.to(device), xs.to(device), ys.to(device), predict,
                    max_context, sample_count, temperature, top_k, top_p, greedy).detach().cpu().numpy()
    return pred[:, :, CLOSE_IDX] * stds[:, CLOSE_IDX, None] + means[:, CLOSE_IDX, None]


def _records_latest(panel: pd.DataFrame, lookback: int, signal_ts: pd.Timestamp,
                    predict: int = 16, calendar_dir: str | os.PathLike | None = DEFAULT_CALENDAR_DIR
                    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build records from recent bars, dropping instruments with stale data.

    A suspended instrument whose last bar is several sessions behind the live
    panel must not produce a fresh signal.  We allow one session of lag (for a
    delayed final bar) and require enough distinct timestamps for context.
    Drop reasons are counted so a low candidate ratio is diagnosable instead of
    a silent surprise.
    """
    panel = panel.sort_values(["ts_code", "timestamps"])
    latest_day = signal_ts.normalize()
    sessions = _trading_days(calendar_dir)
    if sessions:
        prior = [d for d in sessions if d <= latest_day]
        floor_day = prior[-2] if len(prior) >= 2 else latest_day - pd.Timedelta(days=2)
    else:
        floor_day = latest_day - pd.Timedelta(days=2)
    future_days = _next_trading_days(signal_ts, max(1, int(np.ceil(predict / len(MARKET_TIMES)))), calendar_dir)
    future_dates = [pd.Timestamp(f"{day.date()} {clock}") for day in future_days for clock in MARKET_TIMES][:predict]
    records: list[dict[str, Any]] = []
    stats = {"dropped_stale_data": 0, "dropped_insufficient_history": 0, "dropped_nonfinite_or_nonpositive": 0}
    for code, group in panel.groupby("ts_code", sort=False):
        group = group.dropna(subset=FEATURES).sort_values("timestamps")
        if group.empty or pd.Timestamp(group["timestamps"].iloc[-1]) < floor_day:
            stats["dropped_stale_data"] += 1
            continue
        group = group.tail(lookback)
        if len(group) < lookback or group["timestamps"].nunique() < lookback:
            stats["dropped_insufficient_history"] += 1
            continue
        values = group[FEATURES].to_numpy(np.float32)
        if not np.isfinite(values).all() or values[-1, CLOSE_IDX] <= 0:
            stats["dropped_nonfinite_or_nonpositive"] += 1
            continue
        last_ts = pd.Timestamp(group["timestamps"].iloc[-1])
        records.append({
            "ts_code": str(code),
            "context_end_date": last_ts.isoformat(),
            "pred_end_date": future_dates[-1].date().isoformat(),
            "last_close": float(values[-1, CLOSE_IDX]),
            "values": values,
            "dates": np.concatenate([group["timestamps"].to_numpy(), np.asarray(future_dates, dtype="datetime64[ns]")]),
            "start": 0,
            "actual_return": np.nan,
            "actual_last_return": np.nan,
            "actual_next_return": np.nan,
        })
    return records, stats


def _prepare_context(panel_path: str, signal_timestamp: str | None, lookback: int, predict: int,
                     calendar_dir: str | os.PathLike | None) -> dict[str, Any]:
    """Read the panel once and build every instrument's context records."""
    # Always use the newest completed 30-minute panel after a refresh; never pin
    # a stale dated filename when the user asks for a live run.
    if panel_path == DEFAULT_PANEL:
        candidates = sorted(Path(FINETUNE_ROOT, "data").glob("astock_30min_ohlcva_full_*_parallel.parquet"), key=lambda p: p.stat().st_mtime)
        if candidates: panel_path = str(candidates[-1])
    panel = pd.read_parquet(panel_path)
    panel["timestamps"] = pd.to_datetime(panel["timestamps"])
    observed_latest = pd.Timestamp(panel["timestamps"].max())
    signal_ts = pd.Timestamp(signal_timestamp) if signal_timestamp else observed_latest
    if signal_ts < observed_latest:
        signal_ts = observed_latest
    target_date = _next_weekday(signal_ts) if calendar_dir is None else _next_trading_days(signal_ts, 1, calendar_dir)[0]
    records, filter_stats = _records_latest(panel, lookback, signal_ts, predict=predict, calendar_dir=calendar_dir)
    return {"records": records, "filter_stats": filter_stats, "panel_path": panel_path,
            "signal_ts": signal_ts, "observed_latest": observed_latest, "target_date": target_date}


def _run_shard(ctx: dict[str, Any], records: list[dict[str, Any]], output_dir: Path,
               checkpoint: str, lookback: int, predict: int, batch_size: int, max_context: int,
               clip: float, sample_count: int, temperature: float, top_k: int, top_p: float,
               greedy: bool, device: str, gpu_wait_seconds: int, seed: int,
               shard_index: int = 0, shard_count: int = 1) -> dict[str, Any]:
    """Score one interleaved slice of the records on a single device."""
    set_seed(seed)
    out = output_dir
    tag = {"shard": shard_index, "shard_count": shard_count}
    if device == "auto":
        device = _auto_device(gpu_wait_seconds)
    dev = torch.device(device)
    effective_batch = _recommended_batch_size(dev, batch_size)
    initial_batch = effective_batch
    oom_retries = 0
    if dev.type == "cuda": torch.cuda.reset_peak_memory_stats(dev)
    if effective_batch != batch_size:
        print(json.dumps({"stage": "batch_adjusted", "requested": int(batch_size), "effective": int(effective_batch), **tag}, ensure_ascii=False), flush=True)
    print(json.dumps({"stage": "inference_start", "device": str(dev), "stocks": len(records), **tag}, ensure_ascii=False), flush=True)
    tokenizer, model = load_model(dev, checkpoint, "base")
    rows: list[dict[str, Any]] = []
    signal_ts = ctx["signal_ts"]; target_date = ctx["target_date"]
    start = 0
    while start < len(records):
        current_batch = min(effective_batch, len(records) - start)
        batch = records[start:start + current_batch]
        retry = False
        try:
            close = _forecast_batch(tokenizer, model, batch, lookback, predict, clip, max_context,
                                    sample_count, temperature, top_k, top_p, greedy, dev)
        except torch.cuda.OutOfMemoryError:
            if dev.type != "cuda" or current_batch <= 1:
                raise RuntimeError("分时推理显存不足：即使 batch=1 仍无法完成；请释放 GPU 或显式选择其他 device")
            retry = True
        if retry:
            # Clear after the except block so the failed helper's traceback
            # and device tensors are no longer held by the exception.
            effective_batch = max(1, current_batch // 2)
            oom_retries += 1
            gc.collect(); torch.cuda.empty_cache()
            print(json.dumps({"stage": "batch_oom_retry", "next_batch_size": int(effective_batch), **tag}, ensure_ascii=False), flush=True)
            continue
        if len(close) != current_batch:
            raise RuntimeError(f"分时推理输出行数不匹配：{len(close)} / {current_batch}")
        for rec, path in zip(batch, close):
            returns = path / rec["last_close"] - 1.0
            rows.append({
                "ts_code": rec["ts_code"], "signal_timestamp": signal_ts.isoformat(),
                "observed_latest_bar": rec["context_end_date"], "target_trade_date": target_date.date().isoformat(),
                "last_close": rec["last_close"], "pred_return": float(returns.mean()),
                "pred_return_last": float(returns[-1]), "pred_return_std": float(returns.std()),
                "pred_close_path": json.dumps([float(v) for v in path]),
            })
        start += current_batch
        print(json.dumps({"stage": "inference_batch", "done": start, "total": len(records), "batch_size": int(current_batch), **tag}, ensure_ascii=False), flush=True)
        # Live preview: flush scored rows so the UI can show partial results
        # while other shards are still running. Atomic tmp+replace so a
        # concurrent reader never sees a torn CSV; removed after publication.
        try:
            live_tmp = out / f"shard_live_{shard_index}.tmp.csv"
            pd.DataFrame(rows).to_csv(live_tmp, index=False)
            live_tmp.replace(out / f"shard_live_{shard_index}.csv")
        except OSError:
            pass
    if len(rows) != len(records) or len({r['ts_code'] for r in rows}) != len(records):
        raise RuntimeError("分时预测数量或股票唯一性校验失败，未发布不完整结果")
    shard_manifest: dict[str, Any] = {
        "shard_index": shard_index, "shard_count": shard_count, "device": str(dev),
        "stocks": len(records), "requested_batch_size": int(batch_size),
        "initial_batch_size": int(initial_batch), "effective_batch_size": int(effective_batch),
        "oom_retries": oom_retries,
        "peak_allocated_mib": round(torch.cuda.max_memory_allocated(dev) / 1024**2, 1) if dev.type == "cuda" else None,
        "peak_reserved_mib": round(torch.cuda.max_memory_reserved(dev) / 1024**2, 1) if dev.type == "cuda" else None,
        "filter_stats": ctx["filter_stats"],
    }
    if shard_count > 1:
        # Shard workers only persist their slice; the parent merges and ranks.
        pd.DataFrame(rows).to_csv(out / f"shard_predictions_{shard_index}.csv", index=False)
        (out / f"shard_manifest_{shard_index}.json").write_text(json.dumps(shard_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return shard_manifest, rows


def _finalize(ctx: dict[str, Any], rows: list[dict[str, Any]], output_dir: Path,
              stock_basic_path: str | None, shard_manifests: list[dict[str, Any]],
              lookback: int, predict: int, checkpoint: str, requested_batch: int,
              started_at: float, multi: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge shards (or the single run), rank, and publish the final artifacts."""
    out = output_dir
    frame = pd.DataFrame(rows).sort_values("pred_return", ascending=False).reset_index(drop=True)
    if stock_basic_path and Path(stock_basic_path).exists() and len(frame):
        basic = pd.read_parquet(stock_basic_path)
        keep = [c for c in ("ts_code", "name", "industry", "market") if c in basic.columns]
        if "ts_code" in keep:
            frame = frame.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
            frame = frame.sort_values("pred_return", ascending=False).reset_index(drop=True)
    if len(frame):
        frame["rank"] = np.arange(1, len(frame) + 1)
        frame["top5"] = frame["rank"] <= 5
        frame["positive_signal"] = frame["pred_return"] > 0
    csv_path = out / "intraday_next_session_predictions.csv"; frame.to_csv(csv_path, index=False)
    primary = shard_manifests[0]
    aggregated = {
        "device": primary["device"] if len(shard_manifests) == 1 else "multi_gpu",
        "requested_batch_size": requested_batch,
        "initial_batch_size": primary.get("initial_batch_size"),
        "effective_batch_size": primary.get("effective_batch_size"),
        "oom_retries": int(sum(s.get("oom_retries", 0) for s in shard_manifests)),
        "peak_allocated_mib": max((s.get("peak_allocated_mib") or 0) for s in shard_manifests) or None,
        "peak_reserved_mib": max((s.get("peak_reserved_mib") or 0) for s in shard_manifests) or None,
        "filter_stats": primary.get("filter_stats", {}),
    }
    manifest = {
        "model_id": "KRN-260912-ID30", "legacy_label": "#29",
        "checkpoint": checkpoint, "panel_path": ctx["panel_path"],
        "stock_basic_path": stock_basic_path,
        "context_bars": lookback, "prediction_bars": predict,
        "signal_timestamp": ctx["signal_ts"].isoformat(), "observed_latest_bar": ctx["observed_latest"].isoformat(),
        "target_trade_date": ctx["target_date"].date().isoformat(),
        **aggregated,
        "gpu_selection": multi["gpu_selection"] if multi else "max_free_memory",
        "shards": [{k: v for k, v in s.items() if k != "filter_stats"} for s in shard_manifests],
        "wall_seconds": round(time.monotonic() - started_at, 1),
        "candidate_count": int(sum(s["stocks"] for s in shard_manifests)),
        "prediction_count": len(frame),
        "top5": frame.head(5)[["ts_code", "pred_return", "pred_return_last"]].to_dict("records") if len(frame) else [],
        "output_csv": str(csv_path), "protocol": "latest observed 160 bars; calendar stamps; greedy decode; no future labels",
    }
    if multi: manifest.update(multi)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def run_intraday_prediction(
    output_dir: str | os.PathLike,
    panel_path: str = DEFAULT_PANEL,
    checkpoint: str = DEFAULT_CHECKPOINT,
    signal_timestamp: str | None = None,
    stock_basic_path: str | None = DEFAULT_STOCK_BASIC,
    lookback: int = 160,
    predict: int = 16,
    batch_size: int = 1024,
    max_context: int = 512,
    clip: float = 5.0,
    sample_count: int = 1,
    temperature: float = 0.6,
    top_k: int = 0,
    top_p: float = 0.9,
    greedy: bool = True,
    device: str = "auto",
    gpu_wait_seconds: int = 0,
    seed: int = 42,
    calendar_dir: str | os.PathLike | None = DEFAULT_CALENDAR_DIR,
    max_gpus: int = 1,
    limit_stocks: int = 0,
    workers_per_gpu: int = 1,
) -> dict[str, Any]:
    """Generate #29 signals and write CSV/JSON manifest, returning its manifest.

    ``max_gpus > 1`` fans the candidate list out over that many GPUs; see the
    module docstring.  ``limit_stocks`` shrinks the run for smoke tests.
    """
    started_at = time.monotonic()
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    common = dict(checkpoint=checkpoint, lookback=lookback, predict=predict,
                  batch_size=batch_size, max_context=max_context, clip=clip,
                  sample_count=sample_count, temperature=temperature, top_k=top_k,
                  top_p=top_p, greedy=greedy, gpu_wait_seconds=gpu_wait_seconds, seed=seed)
    if int(max_gpus) > 1 and device in ("auto", ""):
        return _run_multi_gpu(out, panel_path, signal_timestamp, calendar_dir,
                              device=device, max_gpus=max_gpus, limit_stocks=limit_stocks,
                              started_at=started_at, workers_per_gpu=workers_per_gpu, **common)
    ctx = _prepare_context(panel_path, signal_timestamp, lookback, predict, calendar_dir)
    records = ctx["records"][: int(limit_stocks)] if int(limit_stocks) > 0 else ctx["records"]
    shard_manifest, rows = _run_shard(ctx, records, out, device=device, shard_index=0, shard_count=1, **common)
    return _finalize(ctx, rows, out, stock_basic_path, [shard_manifest], lookback, predict,
                     checkpoint, batch_size, started_at)


def _run_multi_gpu(out: Path, panel_path: str, signal_timestamp: str | None,
                   calendar_dir: str | os.PathLike | None, device: str, max_gpus: int,
                   limit_stocks: int, started_at: float, checkpoint: str, lookback: int,
                   predict: int, batch_size: int, max_context: int, clip: float,
                   sample_count: int, temperature: float, top_k: int, top_p: float,
                   greedy: bool, gpu_wait_seconds: int, seed: int,
                   workers_per_gpu: int = 1) -> dict[str, Any]:
    """Parent orchestrator: build records once, score shards on N GPUs, merge."""
    import hashlib
    gpus = _auto_devices(max_gpus, gpu_wait_seconds)
    # Resolve the panel exactly like _prepare_context so the cache signature
    # tracks the real input file (mtime+size) instead of a glob placeholder.
    resolved = panel_path
    if resolved == DEFAULT_PANEL:
        candidates = sorted(Path(FINETUNE_ROOT, "data").glob("astock_30min_ohlcva_full_*_parallel.parquet"), key=lambda p: p.stat().st_mtime)
        if candidates: resolved = str(candidates[-1])
    cache_dir = out.parent / "intraday_ctx_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        st = Path(resolved).stat(); stat_sig = f"{int(st.st_mtime_ns)}|{st.st_size}"
    except OSError:
        stat_sig = "-"
    cache_key = hashlib.sha1("|".join([resolved, stat_sig, str(lookback), str(predict),
                                       str(signal_timestamp or ""), str(calendar_dir)]).encode()).hexdigest()[:24]
    cache_path = cache_dir / f"{cache_key}.pkl"
    ctx = None
    if cache_path.exists() and int(limit_stocks) <= 0:
        try:
            ctx = pickle.loads(cache_path.read_bytes())
            print(json.dumps({"stage": "context_cache_hit", "cache": str(cache_path)}, ensure_ascii=False), flush=True)
        except Exception:
            ctx = None
    if ctx is None:
        print(json.dumps({"stage": "context_build_start", "panel_path": resolved}, ensure_ascii=False), flush=True)
        ctx = _prepare_context(resolved, signal_timestamp, lookback, predict, calendar_dir)
        tmp = cache_path.with_suffix(".tmp.pkl")
        tmp.write_bytes(pickle.dumps(ctx)); tmp.replace(cache_path)
        for old in sorted(cache_dir.glob("*.pkl"), key=lambda p: p.stat().st_mtime)[:-3]:
            old.unlink(missing_ok=True)
    if int(limit_stocks) > 0:
        ctx["records"] = ctx["records"][: int(limit_stocks)]
    records = ctx["records"]
    print(json.dumps({"stage": "context_build_done", "candidate_count": len(records),
                      "filter_stats": ctx["filter_stats"]}, ensure_ascii=False), flush=True)
    if not records:
        raise RuntimeError("候选股票为空：面板可能过期或过滤条件过严，未发布任何结果")
    # One context pickle shared by all shard workers (NFS-visible to the pool).
    cache = out / "shard_records.pkl"
    for stale in out.glob("shard_live_*.csv"):
        stale.unlink(missing_ok=True)
    cache.write_bytes(pickle.dumps(ctx))
    workers_per_gpu = max(1, int(workers_per_gpu))
    shard_count = min(len(gpus) * workers_per_gpu, max(1, len(records)))
    assignment = [g for g in gpus for _ in range(workers_per_gpu)]
    engine = Path(__file__).resolve()
    procs: list[tuple[int, subprocess.Popen]] = []
    for shard_index in range(shard_count):
        argv = [sys.executable, str(engine), "--output-dir", str(out),
                "--panel-path", ctx["panel_path"], "--checkpoint", checkpoint,
                "--records-cache", str(cache), "--shard-index", str(shard_index),
                "--shard-count", str(shard_count), "--device", f"cuda:{assignment[shard_index]}",
                "--batch-size", str(batch_size), "--seed", str(seed)]
        procs.append((shard_index, subprocess.Popen(argv)))
        print(json.dumps({"stage": "shard_launched", "shard": shard_index, "gpu": int(assignment[shard_index]),
                          "pid": procs[-1][1].pid}, ensure_ascii=False), flush=True)
    statuses = []
    for shard_index, proc in procs:
        rc = proc.wait()
        statuses.append({"shard": shard_index, "gpu": int(assignment[shard_index]), "returncode": rc})
        print(json.dumps({"stage": "shard_finished", "shard": shard_index, "returncode": rc}, ensure_ascii=False), flush=True)
    failed = [s for s in statuses if s["returncode"] != 0]
    if failed:
        # Rescue pass: a shard usually dies because a co-tenant grabbed the
        # card between selection and load. Re-run the failed slices once on
        # freshly selected GPUs before giving up.
        retry_idx = sorted(s["shard"] for s in failed)
        print(json.dumps({"stage": "shard_retry", "shards": retry_idx}, ensure_ascii=False), flush=True)
        time.sleep(20)
        try:
            rescue_gpus = _auto_devices(max_gpus, gpu_wait_seconds)
        except RuntimeError:
            rescue_gpus = gpus
        rescue_assignment = [g for g in rescue_gpus for _ in range(workers_per_gpu)]
        procs2: list[tuple[int, subprocess.Popen]] = []
        for offset, shard_index in enumerate(retry_idx):
            dev = rescue_assignment[offset % len(rescue_assignment)]
            argv = [sys.executable, str(engine), "--output-dir", str(out),
                    "--panel-path", ctx["panel_path"], "--checkpoint", checkpoint,
                    "--records-cache", str(cache), "--shard-index", str(shard_index),
                    "--shard-count", str(shard_count), "--device", f"cuda:{dev}",
                    "--batch-size", str(batch_size), "--seed", str(seed)]
            procs2.append((shard_index, subprocess.Popen(argv)))
            print(json.dumps({"stage": "shard_rescue_launched", "shard": shard_index, "gpu": int(dev),
                              "pid": procs2[-1][1].pid}, ensure_ascii=False), flush=True)
        for shard_index, proc in procs2:
            rc = proc.wait()
            match = next((s for s in statuses if s["shard"] == shard_index), None)
            if match is not None:
                match["returncode"] = rc
                match["rescued_on"] = int(rescue_assignment[retry_idx.index(shard_index) % len(rescue_assignment)])
            print(json.dumps({"stage": "shard_rescue_finished", "shard": shard_index, "returncode": rc}, ensure_ascii=False), flush=True)
        failed = [s for s in statuses if s["returncode"] != 0]
    if failed:
        # No shard output is trusted unless every shard succeeded; final files
        # from a previous good run stay untouched.
        raise RuntimeError(f"多卡分片推理有 {len(failed)}/{len(statuses)} 个分片失败：{failed}；未发布不完整结果")
    rows: list[dict[str, Any]] = []
    shard_manifests = []
    for shard_index in range(shard_count):
        shard_frame = pd.read_csv(out / f"shard_predictions_{shard_index}.csv")
        rows.extend(shard_frame.to_dict("records"))
        shard_manifests.append(json.loads((out / f"shard_manifest_{shard_index}.json").read_text(encoding="utf-8")))
    multi = {
        "gpu_selection": "multi_gpu_top_free_memory",
        "gpus": sorted({int(g) for g in assignment[:shard_count]}),
        "max_gpus_requested": int(max_gpus),
        "workers_per_gpu": workers_per_gpu,
        "shard_statuses": statuses,
    }
    manifest = _finalize(ctx, rows, out, DEFAULT_STOCK_BASIC, shard_manifests, lookback, predict,
                         checkpoint, batch_size, started_at, multi=multi)
    # Shard CSVs stay for audit; drop the bulky context cache and live previews.
    cache.unlink(missing_ok=True)
    for stale in out.glob("shard_live_*.csv"):
        stale.unlink(missing_ok=True)
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Kronos #29 next-session inference")
    p.add_argument("--output-dir", required=True); p.add_argument("--panel-path", default=DEFAULT_PANEL)
    p.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT); p.add_argument("--stock-basic-path", default=DEFAULT_STOCK_BASIC); p.add_argument("--signal-timestamp")
    p.add_argument("--calendar-dir", default=DEFAULT_CALENDAR_DIR)
    p.add_argument("--device", default="auto"); p.add_argument("--batch-size", type=int, default=1024)
    p.add_argument("--gpu-wait-seconds", type=int, default=0)
    p.add_argument("--max-gpus", type=int, default=1, help=">1 时按空闲显存选前 N 张卡并行分片推理")
    p.add_argument("--workers-per-gpu", type=int, default=1, help="每张卡并行的分片进程数（显存富余时可提速）")
    p.add_argument("--limit-stocks", type=int, default=0, help="只推理前 N 只候选（冒烟测试用）")
    p.add_argument("--records-cache", default="", help="内部：父进程构建的上下文 pickle")
    p.add_argument("--shard-index", type=int, default=0); p.add_argument("--shard-count", type=int, default=1)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    if args.shard_count > 1:
        # Worker mode: load the parent's context instead of re-reading the panel.
        ctx = pickle.loads(Path(args.records_cache).read_bytes())
        records = ctx["records"][args.shard_index::args.shard_count]
        shard_manifest, _rows = _run_shard(ctx, records, Path(args.output_dir), args.checkpoint,
                                           160, 16, args.batch_size, 512, 5.0, 1, 0.6, 0, 0.9, True,
                                           args.device, args.gpu_wait_seconds, args.seed,
                                           shard_index=args.shard_index, shard_count=args.shard_count)
        print(json.dumps({"stage": "shard_done", "shard": args.shard_index, "stocks": shard_manifest["stocks"]}, ensure_ascii=False), flush=True)
        return
    manifest = run_intraday_prediction(args.output_dir, args.panel_path, args.checkpoint,
                                       args.signal_timestamp, args.stock_basic_path, batch_size=args.batch_size,
                                       device=args.device, calendar_dir=args.calendar_dir,
                                       gpu_wait_seconds=args.gpu_wait_seconds,
                                       max_gpus=args.max_gpus, limit_stocks=args.limit_stocks, seed=args.seed,
                                       workers_per_gpu=args.workers_per_gpu)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__=='__main__':
    main()
