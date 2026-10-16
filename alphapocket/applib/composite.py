"""M6 T-C2：合成打分四法并列 + 双闸 + fl_composite_snapshot 落库（计划 §9.1）。

依赖决策（红线「零新外部依赖」的落地记录）：
- core/.venv 实测无 sklearn（ModuleNotFoundError, 2026-10 核对）→ Lasso 用 numpy 自实现
  ISTA（迭代软阈值），迭代上限 1000、收敛容差 1e-4、λ=0.1 固定缺省（敏感性归 T-C4 网格）；
- ISTA 无随机成分（β₀=0 确定性起点）→ 契约「固定随机种子 42（若实现含随机）」不适用，如实登记；
- 本模块自足（不 import factor_backtest，避免与其新增的 composite 消费形成环），
  _rank_pct / select_top 语义与 factor_backtest 同口径复制，改动须两处同步。

PIT 红线：权重判据（IC 窗口 / lasso 训练窗）一律 ≤ 再平衡日——
- composite_scores_multi(as_of=...) 对 history / lasso_data 内部再截断（防线：调用方漏截断也安全）；
- load_ic_history / build_lasso_data 仅取 dt ≤ as_of 的因子截面与次月收益。

双闸语义：告警/冻结，不是拦截——gate1 触发仅告警（pairs 定位 + min_rho）；
gate2 触发沿用上期权重继续跑并全程 frozen_review 标记；degraded 法权重本就退化，不参与闸 2。

透明降级：IC 窗口不足 / 全负 / lasso 训练窗不足 / 系数全零 → 该法退化为等权并写 degraded 原因，
照常输出不抛异常、不静默。
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np

METHODS = ("equal", "ic_12m", "ic_ir", "lasso")
_IC_WINDOW = 12          # ic_12m / ic_ir 判据窗口（月）
_IC_MIN_MONTHS = 6       # 窗口下限：不足 → degraded
_LASSO_TRAIN_MONTHS = 24
_LASSO_MIN_MONTHS = 18
_LASSO_LAMBDA = 0.1
_ISTA_MAX_ITER = 1000
_ISTA_TOL = 1e-4
_DRIFT_TOL = 0.30
_RHO_MIN = 0.70


class CompositeError(Exception):
    """合成打分输入非法（history/lasso_data 结构错误）。"""


# ── 基础：截面 rank / 相关系数（与 factor_backtest 同口径）───────────────
def _rank_pct(vals: dict[str, float]) -> dict[str, float]:
    """截面百分位 (count_less + 0.5·count_eq) / n（并列取中位，确定性）。"""
    out = {}
    for s, v in vals.items():
        less = sum(1 for x in vals.values() if x < v)
        eq = sum(1 for x in vals.values() if x == v) - 1
        out[s] = (less + 0.5 * eq) / len(vals)
    return out


def _directed_ranks(sections: dict, factors: list[dict]) -> dict[str, dict[str, float]]:
    """各因子截面 rank 定向化：desc = rank_pct，asc = 1 − rank_pct（小值为优）。"""
    out = {}
    for f in factors:
        vals = {s: v for s, v in (sections.get(f["id"]) or {}).items() if v is not None}
        if not vals:
            continue
        r = _rank_pct(vals)
        if f.get("direction", "desc") == "asc":
            r = {s: 1.0 - p for s, p in r.items()}
        out[f["id"]] = r
    return out


def _avg_ranks(vs: list[float]) -> list[float]:
    """1-based 平均秩（并列取平均）——Spearman 的秩基底。"""
    order = sorted(range(len(vs)), key=lambda i: vs[i])
    ranks = [0.0] * len(vs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman = 秩 Pearson；样本 <3 视为最大分歧（rho=0 → 触发闸1，诚实告警）。"""
    if len(xs) < 3:
        return 0.0
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def _top_picks(scores: dict[str, float], n: int) -> list[str]:
    """Top-N（分数降序；并列按 secid 升序 → 可复现，与 factor_backtest.select_top 同口径）。"""
    return sorted(scores, key=lambda s: (-scores[s], s))[:max(1, int(n))]


# ── 四法权重 ─────────────────────────────────────────────
def _fixed_priors(factors: list[dict]) -> dict[str, float]:
    """数值权重作先验乘子（缺省 1）——equal 法全 1 时即纯等权 1/n。"""
    out = {}
    for f in factors:
        w = f.get("weight")
        out[f["id"]] = float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) else 1.0
    return out


def _w_equal(factors: list[dict]) -> dict[str, float]:
    priors = _fixed_priors(factors)
    tot = sum(priors.values())
    if tot <= 0:
        return {fid: 1.0 / len(priors) for fid in priors}
    return {fid: p / tot for fid, p in priors.items()}


def _trunc(history: dict, fid: str, as_of: str | None, window: int) -> list[tuple[str, float]]:
    """月频序列按月升序 + PIT 截断（month ≤ as_of）+ 近 window 月。"""
    rows = history.get(fid) or []
    try:
        rows = sorted((str(m), float(ic)) for m, ic in rows)
    except (TypeError, ValueError) as e:
        raise CompositeError(f"history[{fid!r}] 结构非法（应为 [(month, ic), ...]）：{e}") from e
    if as_of:
        rows = [(m, ic) for m, ic in rows if m <= str(as_of)]
    return rows[-window:]


def _w_ic12m(history: dict, factors: list[dict], as_of: str | None) -> tuple[dict, str | None, dict]:
    """ic_12m：近 12 月 directed IC 均值 → w_i = max(μ_i,0)/Σmax(μ_j,0)。

    任一因子窗口 <6 月 → degraded（保守：混窗不可比）；全负 → degraded；
    退化路径一律等权（透明降级）。
    """
    mus: dict[str, float | None] = {}
    min_n = None
    for f in factors:
        rows = _trunc(history, f["id"], as_of, _IC_WINDOW)
        min_n = len(rows) if min_n is None else min(min_n, len(rows))
        mus[f["id"]] = sum(ic for _, ic in rows) / len(rows) if rows else None
    judged = {fid: (round(m, 6) if m is not None else None) for fid, m in mus.items()}
    if min_n is None or min_n < _IC_MIN_MONTHS:
        return _w_equal(factors), f"IC 窗口不足（<{_IC_MIN_MONTHS} 月）", judged
    if all(m is None or m <= 0 for m in mus.values()):
        return _w_equal(factors), "近 12 月 directed IC 全负", judged
    tot = sum(max(m, 0.0) for m in mus.values() if m is not None)
    w = {fid: (max(m, 0.0) / tot if (m is not None and m > 0) else 0.0) for fid, m in mus.items()}
    return w, None, judged


def _w_ic_ir(history: dict, factors: list[dict], as_of: str | None) -> tuple[dict, str | None, dict]:
    """ic_ir：同窗口 IR_i = μ_i/σ_i（样本方差）；σ_i=0 → 该因子剔除；全部无效 → degraded。"""
    irs: dict[str, float | None] = {}
    min_n = None
    for f in factors:
        rows = _trunc(history, f["id"], as_of, _IC_WINDOW)
        min_n = len(rows) if min_n is None else min(min_n, len(rows))
        if len(rows) < 2:
            irs[f["id"]] = None
            continue
        mu = sum(ic for _, ic in rows) / len(rows)
        var = sum((ic - mu) ** 2 for _, ic in rows) / (len(rows) - 1)
        sigma = var ** 0.5
        # σ<1e-9 视为常数序列（浮点残差防抖：常数均值的求和误差会造出 1e-17 级假 σ）
        irs[f["id"]] = None if sigma < 1e-9 else mu / sigma
    judged = {fid: (round(v, 6) if v is not None else None) for fid, v in irs.items()}
    if min_n is None or min_n < _IC_MIN_MONTHS:
        return _w_equal(factors), f"IC 窗口不足（<{_IC_MIN_MONTHS} 月）", judged
    valid = {fid: v for fid, v in irs.items() if v is not None}
    if not valid or all(v <= 0 for v in valid.values()):
        return _w_equal(factors), "ic_ir 全部无效（σ=0 或 IR≤0）", judged
    tot = sum(max(v, 0.0) for v in valid.values())
    w = {fid: (max(v, 0.0) / tot if (v is not None and v > 0) else 0.0) for fid, v in irs.items()}
    return w, None, judged


def _ista(X: np.ndarray, y: np.ndarray, lam: float = _LASSO_LAMBDA) -> np.ndarray:
    """Lasso（L1-regularized least squares）ISTA 求解：β ← soft(β − ∇/L, λ·step)。

    确定性：β₀=0、步长 = 1/Lipschitz（XᵀX/n 最大特征值）、无随机项；
    收敛判据 max|Δβ| < 1e-4，上限 1000 轮。
    """
    n, k = X.shape
    L = float(np.linalg.eigvalsh(X.T @ X / n).max())
    step = 1.0 / max(L, 1e-12)
    beta = np.zeros(k)
    for _ in range(_ISTA_MAX_ITER):
        grad = X.T @ (X @ beta - y) / n
        z = beta - step * grad
        new = np.sign(z) * np.maximum(np.abs(z) - step * lam, 0.0)
        if float(np.max(np.abs(new - beta))) < _ISTA_TOL:
            beta = new
            break
        beta = new
    return beta


def _w_lasso(lasso_data: dict | None, factors: list[dict],
             as_of: str | None) -> tuple[dict, str | None, dict]:
    """lasso：训练窗 24 月（<18 → degraded+等权）；X=各月 directed rank，y=次月收益。

    zscore 标准化 → ISTA(λ=0.1) → β 负系数截零 → 正 β 归一（w_i = β_i/Σβ⁺）。
    """
    fids = [f["id"] for f in factors]
    if not lasso_data:
        return _w_equal(factors), "lasso 训练数据缺失", {fid: None for fid in fids}
    months_all = sorted(str(m) for m in lasso_data.get("months") or [])
    if as_of:
        months_all = [m for m in months_all if m <= str(as_of)]
    window = months_all[-_LASSO_TRAIN_MONTHS:]
    if len(window) < _LASSO_MIN_MONTHS:
        return (_w_equal(factors),
                f"lasso 训练窗不足（{len(window)} < {_LASSO_MIN_MONTHS} 月）",
                {fid: None for fid in fids})
    rank = lasso_data.get("rank") or {}
    fwd = lasso_data.get("fwd") or {}
    Xr: list[list[float]] = []
    yr: list[float] = []
    for m in window:
        fr = fwd.get(m) or {}
        for s in sorted(fr):
            row = [(rank.get(fid) or {}).get(m, {}).get(s) for fid in fids]
            if any(v is None for v in row):
                continue
            Xr.append([float(v) for v in row])
            yr.append(float(fr[s]))
    if len(Xr) < len(fids) + 1:
        return _w_equal(factors), f"lasso 训练样本不足（N={len(Xr)}）", {fid: None for fid in fids}
    X = np.asarray(Xr, dtype=float)
    y = np.asarray(yr, dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    beta = _ista((X - mu) / sd, y - y.mean())
    raw = {fid: float(beta[i]) for i, fid in enumerate(fids)}
    pos = {fid: b for fid, b in raw.items() if b > 0}
    if not pos or sum(pos.values()) <= 0:
        return _w_equal(factors), "lasso 系数全零/全负", raw
    tot = sum(pos.values())
    return {fid: pos.get(fid, 0.0) / tot for fid in fids}, None, raw


# ── 主入口：四法并列合成 ──────────────────────────────────
def composite_multi_meta(sections: dict, factors: list[dict], history: dict,
                         as_of: str | None = None,
                         lasso_data: dict | None = None) -> tuple[dict, dict]:
    """四法并列 → ({method: {secid: score∈[0,1]}}, {method: {weights, judged, degraded}})。

    - 各法 raw = Σ w_i·rank_i(s) / Σ_{可用} w_i（单标的缺失因子不计入分母，对齐现状语义）；
    - raw 再走截面 rank 归一到 [0,1]（并列方法间可比的前提）；零权重因子不参与分子分母；
    - equal 法：w_i ∝ 数值权重先验（缺省全 1 = 纯等权 1/n）；
    - degraded 法照常输出（等权 + 原因），不静默、不抛异常。
    """
    ranks = _directed_ranks(sections, factors)
    weights = {"equal": _w_equal(factors)}
    weights["ic_12m"], deg12, judged12 = _w_ic12m(history, factors, as_of)
    weights["ic_ir"], degir, judgedir = _w_ic_ir(history, factors, as_of)
    weights["lasso"], degla, judgedla = _w_lasso(lasso_data, factors, as_of)
    degraded = {"equal": None, "ic_12m": deg12, "ic_ir": degir, "lasso": degla}
    judged = {"equal": {f["id"]: None for f in factors}, "ic_12m": judged12,
              "ic_ir": judgedir, "lasso": judgedla}
    scores: dict[str, dict[str, float]] = {}
    for m in METHODS:
        w = weights[m]
        raw: dict[str, float] = {}
        num: dict[str, float] = {}
        den: dict[str, float] = {}
        for fid, r in ranks.items():
            wf = float(w.get(fid, 0.0))
            if wf <= 0:
                continue
            for s, p in r.items():
                num[s] = num.get(s, 0.0) + wf * p
                den[s] = den.get(s, 0.0) + wf
        raw = {s: num[s] / den[s] for s in num if den[s] > 0}
        scores[m] = _rank_pct(raw) if raw else {}
    meta = {m: {"weights": weights[m], "judged": judged[m], "degraded": degraded[m]}
            for m in METHODS}
    return scores, meta


def composite_scores_multi(sections: dict, factors: list[dict], history: dict,
                           as_of: str | None = None,
                           lasso_data: dict | None = None) -> dict[str, dict[str, float]]:
    """契约签名：四法并列截面分（equal / ic_12m / ic_ir / lasso）→ {method: {secid: score}}。"""
    scores, _ = composite_multi_meta(sections, factors, history, as_of=as_of,
                                     lasso_data=lasso_data)
    return scores


# ── 双闸 ─────────────────────────────────────────────────
def gate_consensus(method_scores: dict[str, dict[str, float]], top_n: int) -> dict:
    """闸1：各法 Top-N 并集上的两两 Spearman（用全截面打分，非仅选股交集）。

    任一对 rho < 0.7 → gate1_overfit（告警，不阻断）；样本 <3 视为 rho=0（最大分歧，诚实告警）。
    返回 {"pairs": [...], "min_rho": ...[, "gate1_overfit": {"pairs", "min_rho"}]}。
    """
    picks = {m: _top_picks(sc, top_n) for m, sc in method_scores.items()}
    ms = list(method_scores)
    pairs = []
    for i in range(len(ms)):
        for j in range(i + 1, len(ms)):
            a, b = ms[i], ms[j]
            union = sorted(set(picks[a]) | set(picks[b]))
            common = [s for s in union if s in method_scores[a] and s in method_scores[b]]
            rho = _spearman([method_scores[a][s] for s in common],
                            [method_scores[b][s] for s in common])
            pairs.append({"a": a, "b": b, "rho": round(rho, 6), "n": len(common)})
    if not pairs:
        return {"pairs": [], "min_rho": None}
    out = {"pairs": pairs, "min_rho": min(p["rho"] for p in pairs)}
    bad = [p for p in pairs if p["rho"] < _RHO_MIN]
    if bad:
        out["gate1_overfit"] = {"pairs": bad, "min_rho": min(p["rho"] for p in bad)}
    return out


def gate_weight_drift(prev_weights: dict | None, weights: dict,
                      tol: float = _DRIFT_TOL) -> dict:
    """闸2：L1 漂移 = Σ|Δw|/2（质量守恒下最大 1.0）≥ tol → 冻结沿用上期 + frozen_review。

    首期无 prev → 直接过（drift=None）；degraded 法由调用方跳过本闸（权重本就退化）。
    """
    weights = dict(weights or {})
    if not prev_weights:
        return {"drift": None, "frozen": False, "frozen_review": False, "weights": weights}
    keys = set(prev_weights) | set(weights)
    drift = 0.5 * sum(abs(float(weights.get(k, 0.0)) - float(prev_weights.get(k, 0.0)))
                      for k in keys)
    if drift >= tol:
        return {"drift": round(drift, 6), "frozen": True, "frozen_review": True,
                "weights": dict(prev_weights)}
    return {"drift": round(drift, 6), "frozen": False, "frozen_review": False, "weights": weights}


# ── 快照落库 ─────────────────────────────────────────────
_SNAPSHOT_DDL = """CREATE TABLE IF NOT EXISTS fl_composite_snapshot (
  strategy_id VARCHAR, as_of DATE, method VARCHAR, factor_id VARCHAR,
  weight DOUBLE, directed_ic_12m DOUBLE,
  gate1_min_rho DOUBLE, gate2_drift DOUBLE, frozen BOOLEAN,
  degraded VARCHAR, lineage_id VARCHAR, code_version VARCHAR,
  PRIMARY KEY (strategy_id, as_of, method, factor_id))"""


def _num(v) -> float | None:
    return None if v is None else float(v)


def save_snapshot(db_path: Path, strategy_id: str, as_of: str, meta: dict, gates: dict,
                  dry_run: bool = False, lineage_id: str | None = None,
                  code_version: str | None = None) -> dict:
    """合成快照落库（可复现四元组：strategy_id + as_of + lineage_id + code_version）。

    幂等：同 (strategy_id, as_of) 先 DELETE 后 INSERT（收敛到主键）；
    dry_run（演练态）不落库。gates 摊平到每行：gate1_min_rho / gate2_drift / frozen。
    """
    from .ctx import CODE_VERSION
    cv = code_version or CODE_VERSION
    con = duckdb.connect(str(db_path))
    try:
        con.execute(_SNAPSHOT_DDL)
        if dry_run:
            return {"written": 0, "dry_run": True}
        con.execute("DELETE FROM fl_composite_snapshot WHERE strategy_id = ? AND as_of = ?",
                    [strategy_id, as_of])
        n = 0
        rho, dr, fz = gates.get("min_rho"), gates.get("drift"), bool(gates.get("frozen"))
        for m, mm in (meta or {}).items():
            for fid, w in (mm.get("weights") or {}).items():
                con.execute(
                    "INSERT INTO fl_composite_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [strategy_id, as_of, m, fid, float(w),
                     _num((mm.get("judged") or {}).get(fid)), _num(rho), _num(dr), fz,
                     mm.get("degraded"), lineage_id, cv])
                n += 1
        return {"written": n, "dry_run": False}
    finally:
        con.close()


# ── 月频数据源（IC 历史 / lasso 面板）──────────────────────
def month_ends(dates: list[str]) -> list[str]:
    """月末交易日序列（YYYY-MM → 该月最大 dt）。"""
    by_month: dict[str, list[str]] = {}
    for d in dates:
        by_month.setdefault(d[:7], []).append(d)
    return sorted(max(v) for v in by_month.values())


def load_ic_history(con, fids: list[str], directions: dict[str, str],
                    closes: dict[str, dict[str, float]], as_of: str | None = None,
                    versions: dict[str, str] | None = None, window: int = _IC_WINDOW) -> dict:
    """月频 directed IC 历史：月末截面 directed rank × 次月收益 Spearman（严格 ≤ as_of）。

    directed rank：desc = rank_pct；asc = 1 − rank_pct（与合成打分同口径）。
    收益缺值 / 截面 <3 样本的月份跳过（不造数）。
    """
    all_dates = sorted({d for rows in closes.values() for d in rows})
    mends = [m for m in month_ends(all_dates) if as_of is None or m <= str(as_of)]
    hist: dict[str, list[tuple[str, float]]] = {fid: [] for fid in fids}
    for i in range(len(mends) - 1):
        m0, m1 = mends[i], mends[i + 1]
        fwd = {s: closes[s][m1] / closes[s][m0] - 1.0
               for s in closes if closes[s].get(m0) and closes[s].get(m1)}
        if len(fwd) < 3:
            continue
        for fid in fids:
            sql = ("SELECT secid, value FROM fl_factor_value_daily "
                   "WHERE factor_id = ? AND dt = ?")
            args: list = [fid, m0]
            if versions and versions.get(fid):
                sql += " AND factor_version = ?"
                args.append(versions[fid])
            vals = {s: v for s, v in con.execute(sql, args).fetchall() if v is not None}
            if len(vals) < 3:
                continue
            r = _rank_pct(vals)
            if directions.get(fid, "desc") == "asc":
                r = {s: 1.0 - p for s, p in r.items()}
            common = sorted(set(r) & set(fwd))
            if len(common) < 3:
                continue
            ic = _pearson(_avg_ranks([r[s] for s in common]),
                          _avg_ranks([fwd[s] for s in common]))
            hist[fid].append((m0, round(ic, 6)))
    return {fid: rows[-window:] for fid, rows in hist.items()}


def build_lasso_data(con, fids: list[str], directions: dict[str, str],
                     closes: dict[str, dict[str, float]], as_of: str | None = None,
                     versions: dict[str, str] | None = None,
                     window: int = _LASSO_TRAIN_MONTHS) -> dict:
    """lasso 训练面板：{months, rank: {fid: {month: {secid: directed_rank}}}, fwd}。

    仅收「全部因子截面 + 次月收益齐备」的月份（不造数）；严格 ≤ as_of。
    """
    all_dates = sorted({d for rows in closes.values() for d in rows})
    mends = [m for m in month_ends(all_dates) if as_of is None or m <= str(as_of)]
    rank: dict[str, dict[str, dict[str, float]]] = {fid: {} for fid in fids}
    fwd_all: dict[str, dict[str, float]] = {}
    months: list[str] = []
    for i in range(len(mends) - 1):
        m0, m1 = mends[i], mends[i + 1]
        fwd = {s: closes[s][m1] / closes[s][m0] - 1.0
               for s in closes if closes[s].get(m0) and closes[s].get(m1)}
        if len(fwd) < 3:
            continue
        month_rank: dict[str, dict[str, float]] = {}
        ok = True
        for fid in fids:
            sql = ("SELECT secid, value FROM fl_factor_value_daily "
                   "WHERE factor_id = ? AND dt = ?")
            args: list = [fid, m0]
            if versions and versions.get(fid):
                sql += " AND factor_version = ?"
                args.append(versions[fid])
            vals = {s: v for s, v in con.execute(sql, args).fetchall() if v is not None}
            if len(vals) < 3:
                ok = False
                break
            r = _rank_pct(vals)
            if directions.get(fid, "desc") == "asc":
                r = {s: 1.0 - p for s, p in r.items()}
            month_rank[fid] = r
        if not ok:
            continue
        for fid, r in month_rank.items():
            rank[fid][m0] = r
        fwd_all[m0] = fwd
        months.append(m0)
    keep = months[-window:]
    return {"months": keep,
            "rank": {fid: {m: rank[fid][m] for m in keep if m in rank[fid]} for fid in fids},
            "fwd": {m: fwd_all[m] for m in keep}}
