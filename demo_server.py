#!/usr/bin/env python3
"""QuantWeave demo server.

Serves the real dashboard (``templates/`` + ``static/``) against a frozen,
redistributable snapshot in ``demo/snapshots/``.  No market data, no model
weights, no private paths: every number below is a recorded demo sample.

    python demo_server.py --port 7072

The demo intentionally keeps the same JSON contract as the production API
implemented in ``app.py`` so the frontend cannot tell them apart.  Only a
handful of endpoints (jobs, paper account) are simulated in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
SNAPSHOTS = ROOT / "demo" / "snapshots"
ARTIFACTS = ROOT / "demo" / "artifacts"
TZ = ZoneInfo("Asia/Shanghai")

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config["JSON_AS_ASCII"] = False

_cache: dict[str, object] = {}
_lock = threading.Lock()

STAGES: dict[str, list[tuple[str, int]]] = {
    "data_update": [
        ("校验交易日历与本地面板", 10),
        ("增量下载日线行情", 38),
        ("补齐 30 分钟 bar 与宽度", 66),
        ("质量审计：缺口 / 重复 / 未收盘", 90),
    ],
    "factor_evaluate": [
        ("加载面板与因子缓存", 18),
        ("逐日因果回测（含交易成本）", 55),
        ("计算 IC / 分组 / 换手", 82),
        ("生成日度曲线与报告", 95),
    ],
    "composite_factor": [
        ("对齐日模型与分时分数", 30),
        ("横截面标准化", 60),
        ("合成排名与稳定性过滤", 88),
    ],
    "predict": [
        ("加载冠军模型工作簿", 30),
        ("生成下一交易日目标组合", 65),
        ("风控闸门与暴露检查", 90),
    ],
    "intraday_predict": [
        ("拉取分时快照", 25),
        ("行业相对强度打分", 58),
        ("生成日内候选与大盘曲线", 88),
    ],
    "chan_multi": [
        ("拼装 5F / 30F / 日线结构", 30),
        ("递归中枢与背驰标注", 62),
        ("walk-forward 验证", 88),
    ],
    "trend_refresh": [
        ("刷新申万行业与宽基指数", 20),
        ("计算结构分与七票 P", 55),
        ("排序、写快照并校验", 85),
    ],
}

DONE_TAIL = {
    "data_update": "DONE 面板 29 期 · 最新 2026-10-22 · 缺口 0",
    "factor_evaluate": "DONE 因子评价完成 · 30 个交易日 · 引擎 causal_cash_book_v2",
    "composite_factor": "DONE 合成排名完成 · 5,488 只 · 稳定性过滤已启用",
    "predict": "DONE 生成下一交易日目标 · 风控闸门 CLOSED（0 暴露）",
    "intraday_predict": "DONE 分时候选完成 · 5,525 只 · 行业覆盖 31/31",
    "chan_multi": "DONE 多级别结构完成 · 三组合已缓存",
    "trend_refresh": "DONE 趋势刷新完成 · 31 个申万一级 + 3 个宽基",
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load(name: str, default=None):
    with _lock:
        if name in _cache:
            return _cache[name]
    path = SNAPSHOTS / name
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    with _lock:
        _cache[name] = value
    return value


def load_runs() -> list[dict]:
    return load("runs.json", []) or []


def load_account() -> dict:
    path = ROOT / "demo" / "account.json"
    with _lock:
        if "account" not in _cache:
            _cache["account"] = json.loads(path.read_text(encoding="utf-8"))
        return _cache["account"]


def save_account() -> None:
    path = ROOT / "demo" / "account.json"
    path.write_text(json.dumps(load_account(), ensure_ascii=False, indent=2), encoding="utf-8")


def recalc(account: dict) -> dict:
    account["market_value"] = round(sum(p["market_value"] for p in account["positions"]), 2)
    account["total_asset"] = round(account["cash"] + account["market_value"], 2)
    return account


def run_tail(job: dict) -> list[str]:
    return job.setdefault("tail", [])


def sleep_stages(job: dict) -> None:
    """Advance a simulated job through its stage list."""
    kind = job["kind"]
    for label, pct in STAGES.get(kind, [("执行中", 50)]):
        time.sleep(0.85 if kind != "data_update" else 1.0)
        with _lock:
            job["stage"] = label
            job["progress"] = pct
            job["updated_at"] = now_iso()
            run_tail(job).append(f"[{datetime.now(TZ):%H:%M:%S}] [stage {pct}%] {label} …")
    time.sleep(0.6)
    with _lock:
        job.update(
            status="completed",
            progress=100,
            returncode=0,
            stage=DONE_TAIL.get(kind, "DONE"),
            finished_at=now_iso(),
            updated_at=now_iso(),
        )
        run_tail(job).append(f"[{datetime.now(TZ):%H:%M:%S}] {DONE_TAIL.get(kind, 'DONE')}")


def start_job(kind: str, params: dict) -> dict:
    job = {
        "id": secrets.token_hex(6),
        "kind": kind,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "finished_at": None,
        "progress": 0,
        "returncode": None,
        "stage": "已排队，等待执行",
        "pid": 4200 + secrets.randbelow(500),
        "params": params or {},
        "metadata": {"demo": True, "runner": "demo_server"},
        "log": f"/opt/quantweave/runs/{kind}.log",
        "tail": [f"[{datetime.now(TZ):%H:%M:%S}] job {kind} queued (demo mode)"],
    }
    with _lock:
        _cache.setdefault("jobs", {})[job["id"]] = job
    threading.Thread(target=sleep_stages, args=(job,), daemon=True).start()
    return job


def all_runs() -> list[dict]:
    jobs = list((_cache.get("jobs") or {}).values())
    merged = jobs + load_runs()
    return sorted(merged, key=lambda r: r.get("created_at") or "", reverse=True)


# --------------------------------------------------------------------------- #
# pages & static
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(str(ROOT / "templates"), "index.html")


@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# core API
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status():
    data = dict(load("status.json", {}))
    data["time"] = now_iso()
    data["demo"] = True
    return jsonify(data)


@app.get("/api/config")
def api_get_config():
    return jsonify(load("config.json", {}))


@app.put("/api/config")
def api_put_config():
    body = request.get_json(silent=True) or {}
    cfg = load("config.json", {})

    def merge(dst: dict, src: dict) -> dict:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    merge(cfg, body)
    return jsonify(cfg)


@app.get("/api/review")
def api_get_review():
    day = request.args.get("date") or "2026-10-23"
    data = load("review.json", {})
    return jsonify({"date": day, "content": data.get("content", ""), "path": f"/opt/quantweave/reviews/{day}.md"})


@app.put("/api/review")
def api_put_review():
    body = request.get_json(silent=True) or {}
    day = body.get("date") or "2026-10-23"
    content = str(body.get("content", ""))
    with _lock:
        _cache["review"] = {"date": day, "content": content}
    return jsonify({"date": day, "path": f"/opt/quantweave/reviews/{day}.md", "bytes": len(content.encode())})


@app.post("/api/jobs")
def api_create_job():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in STAGES:
        return jsonify({"error": f"未知任务类型：{kind}"}), 400
    if kind in {"data_update", "chan_multi", "trend_refresh"}:
        with _lock:
            for job in (_cache.get("jobs") or {}).values():
                if job["kind"] == kind and job["status"] == "running":
                    return jsonify(job), 202
    job = start_job(kind, body.get("params") or {})
    return jsonify(job), 202


@app.get("/api/runs")
def api_runs():
    return jsonify(all_runs())


@app.get("/api/runs/<run_id>")
def api_run(run_id: str):
    for job in all_runs():
        if job.get("id") == run_id:
            return jsonify(job)
    return jsonify({"error": "run not found"}), 404


@app.get("/api/artifacts")
def api_artifacts():
    return jsonify(load("artifacts.json", []))


@app.get("/api/artifacts/<path:name>")
def api_artifact(name: str):
    return send_from_directory(str(ARTIFACTS), Path(name).name, as_attachment=True)


@app.get("/api/results/latest")
def api_results():
    return jsonify(load("results_latest.json", {"available": False}))


@app.get("/api/factors/latest")
def api_factors():
    return jsonify(load("factors_latest.json", {"available": False}))


@app.get("/api/composite/latest")
def api_composite():
    return jsonify(load("composite_latest.json", {"available": False}))


@app.get("/api/intraday/latest")
def api_intraday():
    return jsonify(load("intraday_latest.json", {"available": False}))


@app.get("/api/intraday/analytics")
def api_intraday_analytics():
    return jsonify(load("intraday_analytics.json", {"available": False}))


@app.get("/api/intraday/industry-ranking")
def api_intraday_ranking():
    return jsonify(load("intraday_industry_ranking.json", {"available": False}))


@app.get("/api/intraday/market-curve")
def api_intraday_curve():
    return jsonify(load("intraday_market_curve.json", {"available": False}))


@app.get("/api/intraday/ic-trend")
def api_ic_trend():
    return jsonify(load("intraday_ic_trend.json", {"available": False}))


@app.get("/api/market/volume-history")
def api_volume_history():
    return jsonify(load("market_volume_history.json", {"available": False}))


@app.get("/api/market_wave")
def api_market_wave():
    return jsonify(load("market_wave.json", {"available": False}))


@app.get("/api/chan_phase")
def api_chan_phase():
    return jsonify(load("chan_phase.json", {"available": False}))


@app.get("/api/chan_multi")
def api_chan_multi():
    combo = request.args.get("combo") or "5F-30F-D"
    data = load(f"chan_multi_{combo}.json")
    if data is None:
        data = load("chan_multi_5F-30F-D.json", {"available": False})
    return jsonify(data)


@app.get("/api/chan_multi/eval")
def api_chan_multi_eval():
    combo = request.args.get("combo") or "5F-30F-D"
    data = load(f"chan_multi_eval_{combo}.json")
    if data is None:
        data = load("chan_multi_eval_5F-30F-D.json", {"available": False})
    return jsonify(data)


@app.get("/api/champion_workbook")
def api_champion():
    return jsonify(load("champion_workbook.json", {"available": False}))


@app.get("/api/trends")
def api_trends():
    return jsonify(load("trends.json", {"available": False}))


@app.get("/api/trends/<code>")
def api_trend_detail(code: str):
    level = (request.args.get("level") or "D").upper()
    if level not in {"D", "W", "M"}:
        level = "D"
    path = SNAPSHOTS / "trends_detail" / f"{code}_{level}.json"
    if not path.exists():
        path = SNAPSHOTS / "trends_detail" / f"{code}_D.json"
    if not path.exists():
        return jsonify({"available": False, "code": code, "reason": "demo 快照未内置该标的逐 K 线数据"}), 404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/chan/classic-cases")
def api_classic_cases():
    return jsonify(load("chan_classic_cases.json", {"available": False}))


# --------------------------------------------------------------------------- #
# paper account (in-memory)
# --------------------------------------------------------------------------- #
@app.get("/api/intraday/account")
def api_account():
    return jsonify(load_account())


@app.put("/api/intraday/account")
def api_account_put():
    body = request.get_json(silent=True) or {}
    acc = load_account()["account"]
    if body.get("reset"):
        capital = float(body.get("capital") or acc.get("capital") or 1_000_000)
        with _lock:
            acc.update(capital=capital, cash=capital, positions=[], trades=[])
        recalc(acc)
        save_account()
        return jsonify({"account": acc, "message": "账户已重置"})
    capital = body.get("capital")
    if capital is not None:
        delta = float(capital) - float(acc["capital"])
        acc["capital"] = float(capital)
        acc["cash"] = round(float(acc["cash"]) + delta, 2)
        recalc(acc)
        save_account()
    return jsonify({"account": acc, "message": "本金已更新"})


@app.post("/api/intraday/positions")
def api_position_add():
    body = request.get_json(silent=True) or {}
    acc = load_account()["account"]
    try:
        code = str(body["ts_code"]).strip().upper()
        shares = int(body.get("shares") or 0)
        price = float(body.get("price") or body.get("entry_price") or 0)
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "参数不完整"}), 400
    if shares <= 0 or price <= 0:
        return jsonify({"error": "股数与价格需为正数"}), 400
    cost = round(shares * price, 2)
    if cost > float(acc["cash"]):
        return jsonify({"error": "现金不足"}), 400
    acc["cash"] = round(float(acc["cash"]) - cost, 2)
    name = body.get("name")
    if not name:
        snap = load("intraday_latest.json", {})
        for row in ((snap.get("result") or {}).get("predictions") or []):
            if str(row.get("ts_code")) == code:
                name = row.get("name")
                break
    pos = {
        "id": secrets.token_hex(6),
        "ts_code": code,
        "name": name or code,
        "shares": shares,
        "entry_px": price,
        "last_price": price,
        "entry_day": str(body.get("entry_date") or body.get("date") or datetime.now(TZ).date().isoformat())[:10],
        "created_at": now_iso(),
        "market_value": cost,
        "float_pnl": 0.0,
        "return": 0.0,
        "action": "HOLD",
    }
    acc["positions"].append(pos)
    recalc(acc)
    save_account()
    return jsonify({"account": acc, "message": f"已录入 {code} {shares} 股"})


@app.delete("/api/intraday/positions/<pos_id>")
def api_position_cancel(pos_id: str):
    acc = load_account()["account"]
    keep = []
    refund = 0.0
    for p in acc["positions"]:
        if p["id"] == pos_id:
            refund += float(p["entry_px"]) * int(p["shares"])
        else:
            keep.append(p)
    acc["positions"] = keep
    acc["cash"] = round(float(acc["cash"]) + refund, 2)
    recalc(acc)
    save_account()
    return jsonify({"account": acc, "message": "已取消该笔录入"})


@app.post("/api/intraday/positions/<pos_id>/sell")
def api_position_sell(pos_id: str):
    body = request.get_json(silent=True) or {}
    acc = load_account()["account"]
    target = next((p for p in acc["positions"] if p["id"] == pos_id), None)
    if not target:
        return jsonify({"error": "持仓不存在"}), 404
    price = float(body.get("price") or target["last_price"])
    shares = int(body.get("shares") or target["shares"])
    shares = min(shares, int(target["shares"]))
    pnl = round((price - float(target["entry_px"])) * shares, 2)
    acc["cash"] = round(float(acc["cash"]) + price * shares, 2)
    if shares >= target["shares"]:
        acc["positions"].remove(target)
    else:
        target["shares"] -= shares
        target["market_value"] = round(target["shares"] * target["last_price"], 2)
    ret = price / float(target["entry_px"]) - 1
    acc.setdefault("trades", []).insert(0, {
        "exit_day": datetime.now(TZ).date().isoformat(),
        "ts_code": target["ts_code"],
        "name": target.get("name"),
        "shares": shares,
        "entry_px": target["entry_px"],
        "exit_px": price,
        "pnl": pnl,
        "ret": round(ret, 6),
        "reason": "手动平仓（demo）",
    })
    stats = acc.setdefault("stats", {"total_pnl": 0, "n_trades": 0, "n_wins": 0, "n_losses": 0})
    stats["total_pnl"] = round(float(stats.get("total_pnl", 0)) + pnl, 2)
    stats["n_trades"] = int(stats.get("n_trades", 0)) + 1
    stats["n_wins"] = int(stats.get("n_wins", 0)) + (1 if pnl >= 0 else 0)
    stats["n_losses"] = int(stats.get("n_losses", 0)) + (0 if pnl >= 0 else 1)
    stats["win_rate"] = round(stats["n_wins"] / max(1, stats["n_trades"]), 4)
    recalc(acc)
    save_account()
    return jsonify({"account": acc, "message": f"已平仓 {shares} 股 · 盈亏 {pnl:+,.0f} 元"})


@app.post("/api/intraday/timing")
def api_timing():
    return jsonify({
        "available": True,
        "result": {
            "kind": "intraday_timing",
            "signal_date": "2026-10-23",
            "status": "DEMO",
            "message": "Demo 模式：择时参数已记录，不产生交易指令。",
        },
    })


# --------------------------------------------------------------------------- #
# synthetic stock snapshot so chart buttons stay interactive
# --------------------------------------------------------------------------- #
@app.get("/api/market_snapshot")
def api_market_snapshot():
    mode = request.args.get("mode") or "stock"
    symbol = request.args.get("symbol") or "000300.SH"
    limit = min(int(request.args.get("limit") or 90), 250)
    if mode != "stock":
        return jsonify({"available": True, "rows": [], "industries": [], "warning": "demo"})
    rng = random.Random(int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16))
    days = []
    d = datetime(2026, 10, 23)
    while len(days) < limit:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    price = 8 + rng.random() * 40
    opens, highs, lows, closes, vols = [], [], [], [], []
    for _ in days:
        drift = rng.gauss(0.0008, 0.018)
        o = price
        c = max(0.5, o * (1 + drift))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        opens.append(round(o, 2)); highs.append(round(h, 2))
        lows.append(round(lo, 2)); closes.append(round(c, 2))
        vols.append(int(2e6 + rng.random() * 8e6))
        price = c
    return jsonify({
        "available": True,
        "mode": "stock",
        "symbol": symbol,
        "name": symbol,
        "ohlc": [{
            "timestamps": [x.strftime("%Y-%m-%d") for x in days],
            "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols,
        }],
        "horizon": [{"raw_mean": round(rng.gauss(0.004, 0.02), 5)} for _ in range(5)],
        "warnings": ["demo: 合成 K 线，仅用于界面演示"],
    })


@app.get("/api/visuals/<path:_name>")
def api_visuals(_name: str):
    return jsonify({"available": False, "error": "not part of the open demo"}), 503


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "demo": True, "time": now_iso()})


def main() -> None:
    ap = argparse.ArgumentParser(description="QuantWeave demo server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7072)
    args = ap.parse_args()
    print(f"\n  QuantWeave demo  →  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
