#!/usr/bin/env python3
"""大盘浪型因子的样本外评估：相位准确性与门控价值。

拆分：2005-01-01 ~ 2017-12-31 拟合（标准化/逻辑回归），2018-01-01 起全部样本外。
评估一（择时准确性）：相位(推动/调整)对指数 fwd1/fwd5/fwd20 的条件均值差、命中率、秩IC。
评估二（门控价值，指数层）：推动日持有指数、调整日空仓，对比买入持有（累计/最大回撤/敞口）。
评估三（门控价值，策略层）：2026 年因子实验组合盯市序列 × 门控（前一日相位）对比不门控。
只读评估，不改模型与策略。
"""
from __future__ import annotations
import glob, json, math, os, sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_wave_factor as mwf

ROOT = mwf.ROOT
SPLIT = "20180101"
ALPHA = mwf.ALPHA


def build_features(idx: pd.DataFrame, amt: pd.Series | None) -> pd.DataFrame:
    close = idx["close"].astype(float)
    ret = close.pct_change()
    sigma20 = ret.rolling(20).std()
    ma20, ma120 = close.rolling(20).mean(), close.rolling(120).mean()
    f = pd.DataFrame(index=idx.index)
    f["f_trendpos"] = close / ma120 - 1.0
    f["f_maslope"] = ma20 / ma20.shift(10) - 1.0
    f["f_mom20"] = close.pct_change(20)
    f["f_vol20"] = sigma20 * math.sqrt(244)
    f["f_volratio"] = sigma20.rolling(20).mean() / sigma20.rolling(60).mean()
    if amt is not None:
        f["f_amtratio"] = amt.reindex(idx["trade_date"].tolist()).to_numpy()
    return f


def max_drawdown(cum: np.ndarray) -> float:
    peak = np.maximum.accumulate(cum)
    return float(((cum / peak) - 1.0).min()) if len(cum) else float("nan")


def main() -> None:
    idx = pd.read_parquet(mwf.INDEX)
    idx["trade_date"] = idx["trade_date"].astype(str).str.replace("-", "", regex=False)
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    close = idx["close"].astype(float)
    ret = close.pct_change()
    sigma20 = ret.rolling(20).std()
    thr = np.maximum(mwf.ZIGZAG_BASE, (mwf.VOL_MULT * sigma20).fillna(mwf.ZIGZAG_BASE).to_numpy())
    label = mwf.zigzag_labels(close.to_numpy(), thr)
    feats = build_features(idx, mwf.load_amount_ratio())

    valid = feats.notna().all(axis=1).to_numpy() & ~np.isnan(label)
    valid[:mwf.WARMUP] = False
    dates = idx["trade_date"].tolist()
    test_mask = (np.array(dates) >= SPLIT) & valid

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="直接使用规则引擎输出的相位（无拟合）")
    args, _ = ap.parse_known_args()
    if args.json:
        wd = json.loads(Path(args.json).read_text(encoding="utf-8"))["daily"]
        pmap = {str(r["date"]): float(r["p_impulse"]) for r in wd}
        p = np.array([pmap.get(dt, np.nan) for dt in dates])
        auc_tr = auc_te = None
    else:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        X = feats.to_numpy(float)
        tr = valid & ~test_mask
        mu, sd = X[tr].mean(axis=0), X[tr].std(axis=0) + 1e-12
        Z = (X - mu) / sd
        model = LogisticRegression(C=1.0, max_iter=2000).fit(Z[tr], label[tr].astype(int))
        p = np.full(len(dates), np.nan)
        p[tr] = model.predict_proba(Z[tr])[:, 1]
        p[test_mask] = model.predict_proba(Z[test_mask])[:, 1]
        auc_tr = roc_auc_score(label[tr], p[tr])
        auc_te = roc_auc_score(label[test_mask], p[test_mask])

    pe = p.copy()
    idxs = np.where(~np.isnan(pe))[0]
    prev = None
    for i in idxs:
        prev = pe[i] if prev is None else ALPHA * prev + (1 - ALPHA) * pe[i]
        pe[i] = prev
    phase = np.where(pe >= 0.5, 1, 0).astype(float)
    phase[~np.isfinite(pe)] = np.nan

    out = {"split": SPLIT, "auc_train": (round(auc_tr, 4) if auc_tr else None), "auc_test_label": (round(auc_te, 4) if auc_te else None), "test_days": int(test_mask.sum())}
    ti = np.where(test_mask)[0]
    fwd_stats = {}
    for k in (1, 5, 20):
        fwd = (close.shift(-k) / close - 1).to_numpy()
        m_imp = np.nanmean(fwd[ti][phase[ti] == 1])
        m_cor = np.nanmean(fwd[ti][phase[ti] == 0])
        both = ~np.isnan(fwd[ti])
        spread = m_imp - m_cor
        sd = np.nanstd(fwd[ti])
        tstat = spread / (sd * math.sqrt(2 / len(ti))) if sd > 0 else float("nan")
        hit = float(np.mean((fwd[ti] > 0)[phase[ti] == 1])) if (phase[ti] == 1).any() else float("nan")
        ic = float(pd.Series(pe[ti]).corr(pd.Series(fwd[ti]), method="spearman"))
        fwd_stats[f"fwd{k}"] = {"推动后": round(float(m_imp) * 100, 3), "调整后": round(float(m_cor) * 100, 3),
                                "差(bps/日)": round(spread * 1e4, 1), "t≈": round(float(tstat), 2),
                                "推动日指数上涨率": round(hit * 100, 1), "秩IC": round(ic, 4)}
    out["择时准确性_样本外"] = fwd_stats

    # 门控（指数层，OOS）：相位在 t 收盘已知 → 决定 t→t+1 是否持仓
    fwd1 = (close.shift(-1) / close - 1).to_numpy()
    gate = np.where(np.isnan(phase), 0.0, phase)
    strat = gate * np.nan_to_num(fwd1, nan=0.0)
    m = test_mask.copy()
    bh = np.cumprod(1 + np.nan_to_num(fwd1[m], nan=0.0))
    gv = np.cumprod(1 + strat[m])
    expo = float(np.mean(gate[m]))
    out["门控_指数层_样本外"] = {
        "买入持有": round(float((bh[-1] - 1) * 100), 1),
        "门控切换": round(float((gv[-1] - 1) * 100), 1),
        "最大回撤: 持有/门控": [round(max_drawdown(bh) * 100, 1), round(max_drawdown(gv) * 100, 1)],
        "持仓敞口%": round(expo * 100, 1),
        "相位翻转次数": int(np.sum(np.diff(gate[m]) != 0)),
    }

    # 门控（策略层，2026 窗口，小样本仅供参考）
    snaps = sorted(glob.glob(str(ROOT / "runs" / "factor_*/factor_daily.csv")), key=os.path.getmtime)
    src = None
    for c in reversed(snaps):
        snap = Path(c).parent / "factor_snapshot.json"
        if snap.exists() and json.loads(snap.read_text()).get("kronos_source") == "daily":
            src = c
            break
    if src:
        d = pd.read_csv(src)
        wave = {r["date"]: (0 if (r.get("phase") is not None and r["phase"] < 0.5) else 1) for r in
                (json.loads((mwf.HERE / "data" / "market_wave.json").read_text())["daily"])}
                # 因子行日期为信号日 t；门控用 t-1 相位（因果）
        pmap = {str(r["date"]): (1 if r["p_impulse"] >= 0.5 else 0) for r in json.loads((mwf.HERE / "data" / "market_wave.json").read_text())["daily"]}
        order = d["signal_date"].tolist()
        gate_prev = []
        for i, dt in enumerate(order):
            gate_prev.append(pmap.get(str(order[i - 1])) if i > 0 else None)
        d["gate_prev"] = gate_prev
        sub = d.dropna(subset=["port_ret"]).copy()
        sub["g_ret"] = sub.apply(lambda r: r.port_ret if r.gate_prev == 1 else 0.0, axis=1)
        out["门控_策略层_2026窗口"] = {
            "样本信号日": int(len(sub)),
            "不门控累计%": round(float(((1 + sub.port_ret).prod() - 1) * 100), 2),
            "门控后累计%": round(float(((1 + sub.g_ret).prod() - 1) * 100), 2),
            "基准同期%": round(float(((1 + sub.bench_ret.fillna(0)).prod() - 1) * 100), 2),
            "空仓日占比%": round(float((sub.gate_prev == 0).mean() * 100), 1),
            "提示": "仅 40 个信号日且 96% 持仓为强平，样本太小，只看方向不下结论",
        }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
