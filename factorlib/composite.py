"""M6 合成打分（实施计划 §9.1，模式②主力）+ Strategy DSL 解析（§9.4）。

四法并列（§9.1）：equal 等权 / ic_12m 滚动 12M IC 加权 / ic_ir IC_IR 最大化 / lasso 稀疏回归
（自研坐标下降，纯标准库）；另含 rank_product 双排序（PE-ROE 策略语义）与 orthogonal 正交组合
（模式③同步实现；模式① = 单因子 strategies 自然退化；模式④因子轮动留 P2）。

防过拟合双闸（§9.1）：
- 闸 1 一致性：法间两两月度截面 rank 相关均值 < 0.7 → 过拟合告警（须 ≥2 法）；
- 闸 2 权重稳定：相邻期权重 L1 漂移 ≥ 30% → freeze_review（冻结复核）。

方向与预处理纪律：因子值按 meta.direction 对齐后逐月截面 zscore（与 M3/M4 检验口径一致）；
滚动权重只用 t−1 及更早信息（防未来函数：score_t 的权重窗口截至 t−1）。
依赖纪律：纯标准库 + duckdb（无 numpy/sklearn，lasso 自研兜底为唯一路径）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from .evaluate import close_map, cross_sections, month_ends
from .governance import spearman
from .registry import load_definitions

GATE_CONSISTENCY_MIN = 0.70     # 闸 1：法间 rank 相关均值下限
GATE_WEIGHT_DRIFT_MAX = 0.30    # 闸 2：相邻期权重 L1 漂移上限
ROLLING_MONTHS = 12             # 滚动权重窗口
LASSO_LAMBDA = 0.05             # α 阶段冻结（生产走 ±30% 网格）
LASSO_N_ITER = 60
EQUAL_W = "equal_fallback"      # 滚动信息不足时的回退标注


class CompositeError(ValueError):
    """合成/DSL 违例（失败即拒，无静默降级）。"""


# ── 9.4 Strategy DSL ──
@dataclass
class StrategyDSL:
    id: str
    mode: str                        # single | composite | orthogonal
    factors: list                    # [{"id", "weight"}]
    top_n: int                       # 合成打分取前 N
    methods: list                    # 并列输出法（≥1；>1 时触发闸 1）
    freq: str = "monthly"
    exec_: str = "next_day_vwap"
    universe: str = "CN"
    notes: str = ""
    source: str = ""


def load_strategies(root: str | Path) -> dict[str, StrategyDSL]:
    """strategies/ 下一个策略一个 YAML → {id: StrategyDSL}（按 id 排序）。"""
    out: dict[str, StrategyDSL] = {}
    for p in sorted(Path(root).glob("strategies/*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        missing = [k for k in ("id", "factors", "top_n", "methods") if not doc.get(k)]
        if missing:
            raise CompositeError(f"{p.name}: 缺少必填字段 {missing}")
        fids = [f["id"] for f in doc["factors"]]
        if len(set(fids)) != len(fids):
            raise CompositeError(f"{p.name}: factors 重复")
        known = {"equal", "ic_12m", "ic_ir", "lasso", "rank_product", "orthogonal"}
        bad = [m for m in doc["methods"] if m not in known]
        if bad:
            raise CompositeError(f"{p.name}: 未知合成法 {bad}（可选 {sorted(known)}）")
        s = StrategyDSL(id=doc["id"], mode=doc.get("mode", "composite"),
                        factors=doc["factors"], top_n=int(doc["top_n"]),
                        methods=list(doc["methods"]),
                        freq=doc.get("rebalance_freq", "monthly"),
                        exec_=doc.get("exec", "next_day_vwap"),
                        universe=doc.get("universe", "CN"),
                        notes=doc.get("notes", ""), source=str(p.relative_to(root)))
        out[s.id] = s
    return out


# ── 截面原语（方向对齐 zscore） ──
def _zscore(vals: dict[str, float]) -> dict[str, float]:
    if len(vals) < 2:
        return {k: 0.0 for k in vals}
    xs = list(vals.values())
    mu = sum(xs) / len(xs)
    sd = (sum((v - mu) ** 2 for v in xs) / len(xs)) ** 0.5
    if sd < 1e-12:
        return {k: 0.0 for k in vals}
    return {k: (v - mu) / sd for k, v in vals.items()}


def aligned_z(con: duckdb.DuckDBPyConnection, metas: dict, factor_ids: list[str],
              start: str, end: str) -> dict[str, dict[str, dict[str, float]]]:
    """{fid: {dt: {secid: z}}}：direction 对齐（v × direction）后逐月截面 zscore。"""
    out = {}
    for fid in factor_ids:
        meta = metas[fid]
        xs = cross_sections(con, fid, start, end)
        out[fid] = {dt: _zscore({s: v * meta.direction for s, v in secvals.items()})
                    for dt, secvals in xs.items() if len(secvals) >= 2}
    return out


def forward_panels(con: duckdb.DuckDBPyConnection, start: str, end: str
                   ) -> tuple[list[str], dict[str, dict[str, float]]]:
    """月末序 mes + 次月收益 {t: {secid: r}}（t 月末 close → 下月末 close；缺样本不进面板）。"""
    closes = close_map(con, start, end)
    mes = month_ends(con, start, end)
    fwd: dict[str, dict[str, float]] = {}
    for i in range(len(mes) - 1):
        t0, t1 = mes[i], mes[i + 1]
        r = {}
        for s, cm in closes.items():
            c0, c1 = cm.get(t0), cm.get(t1)
            if c0 and c1:
                r[s] = c1 / c0 - 1
        fwd[t0] = r
    return mes, fwd


# ── Lasso（自研坐标下降，纯标准库） ──
def _soft(x: float, t: float) -> float:
    return (x - t) if x > t else ((x + t) if x < -t else 0.0)


def lasso_fit(X: list[list[float]], y: list[float], lam: float = LASSO_LAMBDA,
              n_iter: int = LASSO_N_ITER) -> list[float]:
    """Lasso 坐标下降（soft-thresholding；特征须已标准化，截距忽略）。"""
    n, p = len(X), len(X[0]) if X else 0
    if n < 2 or p < 1:
        return [0.0] * p
    w = [0.0] * p
    col_norm = [sum(X[i][j] ** 2 for i in range(n)) for j in range(p)]
    for _ in range(n_iter):
        max_delta = 0.0
        for j in range(p):
            if col_norm[j] < 1e-12:
                w[j] = 0.0
                continue
            rho = 0.0                      # Σ_i X_ij·(y_i − Σ_{k≠j} X_ik·w_k)
            for i in range(n):
                r = y[i] - sum(X[i][k] * w[k] for k in range(p) if k != j)
                rho += X[i][j] * r
            new = _soft(rho / n, lam) / (col_norm[j] / n)
            max_delta = max(max_delta, abs(new - w[j]))
            w[j] = new
        if max_delta < 1e-10:
            break
    return w


# ── 滚动权重（窗口截至 t−1，防未来函数） ──
def _mean_pos(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _ir_pos(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
    return mu / sd if sd > 1e-12 else None


def _normalize_pos(raw: dict, factor_ids: list[str]) -> tuple[dict[str, float], bool]:
    """正值归一（不赌反向：负 IC/IR → 权重 0）；全 0/None → equal 回退（诚实标注）。"""
    pos = {f: max(0.0, raw.get(f) or 0.0) for f in factor_ids}
    tot = sum(pos.values())
    if tot < 1e-12:
        return {f: 1.0 / len(factor_ids) for f in factor_ids}, True
    return {f: pos[f] / tot for f in factor_ids}, False


def _rolling_weights(method: str, mes: list[str], F: dict, fwd: dict,
                     factor_ids: list[str]) -> dict[str, dict[str, float]]:
    """{dt: {fid: w}}；窗口 = 严格早于 t 的近 ROLLING_MONTHS 月；不足 → equal 起步。"""
    out: dict[str, dict[str, float]] = {}
    eq = {f: 1.0 / len(factor_ids) for f in factor_ids}
    for i, t in enumerate(mes):
        if method == "equal":
            out[t] = dict(eq)
            continue
        win = [m for m in mes[:i][-ROLLING_MONTHS:]
               if fwd.get(m) and all(F[f].get(m) for f in factor_ids)]
        if not win:
            out[t] = dict(eq)
            continue
        if method in ("ic_12m", "ic_ir"):
            stats = {f: [] for f in factor_ids}
            for m in win:
                secids = sorted(set(F[factor_ids[0]][m]) & set(fwd[m]))
                if len(secids) < 3:
                    continue
                y = [fwd[m][s] for s in secids]
                for f in factor_ids:
                    x = [F[f][m].get(s, 0.0) for s in secids]
                    stats[f].append(spearman(x, y))
            raw = {f: (_mean_pos(stats[f]) if method == "ic_12m"
                       else _ir_pos(stats[f])) for f in factor_ids}
            out[t] = _normalize_pos(raw, factor_ids)[0]
        elif method == "lasso":
            rows, ys = [], []
            for m in win:
                secids = sorted(set(F[factor_ids[0]][m]) & set(fwd[m]))
                if len(secids) < 3:
                    continue
                rows.extend([F[f][m].get(s, 0.0) for f in factor_ids] for s in secids)
                ys.extend(fwd[m][s] for s in secids)
            # y 同步标准化（X 已为截面 z；否则 lam 阈值相对收益量级失真 → 全 0 回退）
            my = sum(ys) / len(ys)
            sy = (sum((v - my) ** 2 for v in ys) / len(ys)) ** 0.5
            ys = [(v - my) / sy for v in ys] if sy > 1e-12 else [0.0] * len(ys)
            w = lasso_fit(rows, ys)
            out[t] = _normalize_pos({f: max(0.0, w[j])
                                     for j, f in enumerate(factor_ids)}, factor_ids)[0]
        else:
            raise CompositeError(f"未知滚动权重法 {method}")
    return out


# ── 合成主流程 ──
def _combine(method: str, F: dict, fwd: dict, mes: list[str], factor_ids: list[str]
             ) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """单法合成 → (scores {dt: {secid: s}}, weights {dt: {fid: w}})。"""
    if method == "rank_product":           # 双排序语义（PE-ROE）：z 相乘
        scores = {}
        for t in mes:
            zs = [F[f].get(t) for f in factor_ids if F[f].get(t)]
            if len(zs) == len(factor_ids):
                secids = set(zs[0])
                for z in zs[1:]:
                    secids &= set(z)
                scores[t] = {s: _prod(zs, s) for s in secids}
        return scores, {}
    if method == "orthogonal":             # 模式③
        ics = {f: abs(_mean_pos([_month_ic(F, fwd, f, m) for m in mes])) or 0.0
               for f in factor_ids}
        order = sorted(factor_ids, key=lambda f: -ics[f])
        return _orthogonal_scores(F, mes, order), {}
    weights = _rolling_weights(method, mes, F, fwd, factor_ids)
    scores = {}
    for t in mes:
        zs = [F[f].get(t) for f in factor_ids if F[f].get(t)]
        if not zs:
            continue
        w = weights.get(t, {})
        secids = set(zs[0])
        for z in zs[1:]:
            secids &= set(z)
        scores[t] = {s: sum(w.get(f, 0.0) * F[f][t].get(s, 0.0)
                            for f in factor_ids if F[f].get(t)) for s in secids}
    return scores, weights


def _prod(zs: list, s: str) -> float:
    v = 1.0
    for z in zs:
        v *= z.get(s, 0.0)
    return v


def _month_ic(F: dict, fwd: dict, f: str, m: str) -> float | None:
    if not fwd.get(m) or not F[f].get(m):
        return None
    secids = sorted(set(F[f][m]) & set(fwd[m]))
    if len(secids) < 3:
        return None
    return spearman([F[f][m].get(s, 0.0) for s in secids],
                    [fwd[m][s] for s in secids])


def _orthogonal_scores(F: dict, mes: list[str], order: list[str]
                       ) -> dict[str, dict[str, float]]:
    """模式③：按 |IC| 降序，逐个对前序已选因子逐月截面回归取残差 → 残差 z 等权合成。"""
    scores: dict[str, dict[str, float]] = {}
    for t in mes:
        resid: dict[str, dict[str, float]] = {}
        for f in order:
            x = F[f].get(t, {})
            if not x:
                continue
            y = dict(x)
            for prev in resid:
                b = _beta([x.get(s, 0.0) for s in x],
                          [resid[prev].get(s, 0.0) for s in x])
                y = {s: y.get(s, 0.0) - b * resid[prev].get(s, 0.0) for s in x}
            resid[f] = _zscore(y)
        if resid:
            allsec = set().union(*[set(r) for r in resid.values()])
            scores[t] = _zscore({s: sum(r.get(s, 0.0) for r in resid.values()) / len(resid)
                                 for s in allsec})
    return scores


def _beta(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    var = sum((a - mx) ** 2 for a in x)
    if var < 1e-12:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / var


# ── 双闸（§9.1 防过拟合） ──
def consistency_gate(scores_by_method: dict[str, dict]) -> dict:
    """闸 1：法间两两月度截面 rank 相关均值 < 0.7 → 过拟合告警（<2 法时 not_triggered）。"""
    methods = sorted(scores_by_method)
    if len(methods) < 2:
        return {"triggered": False, "pass": True, "note": "法数 < 2，一致闸不适用"}
    pairs = {}
    for i, a in enumerate(methods):
        for b in methods[i + 1:]:
            rhos = []
            sa, sb = scores_by_method[a], scores_by_method[b]
            for t in sorted(set(sa) & set(sb)):
                secids = sorted(set(sa[t]) & set(sb[t]))
                if len(secids) >= 3:
                    rhos.append(spearman([sa[t][s] for s in secids],
                                         [sb[t][s] for s in secids]))
            rhos = [r for r in rhos if r is not None]
            pairs[f"{a}|{b}"] = {"mean_rho": sum(rhos) / len(rhos) if rhos else None,
                                 "n_months": len(rhos)}
    val = [p["mean_rho"] for p in pairs.values() if p["mean_rho"] is not None]
    mean = sum(val) / len(val) if val else None
    return {"triggered": True, "pass": mean is not None and mean >= GATE_CONSISTENCY_MIN,
            "mean_rho": mean, "pairs": pairs,
            "threshold": GATE_CONSISTENCY_MIN,
            "note": None if (mean is not None and mean >= GATE_CONSISTENCY_MIN)
            else f"法间一致性 {mean:.2f} < {GATE_CONSISTENCY_MIN} → 过拟合告警"}


def weight_stability_gate(weights_by_method: dict[str, dict]) -> dict:
    """闸 2：相邻期权重 L1 漂移 ≥ 30% → freeze_review（equal 权重恒定，天然过）。"""
    out = {"triggered": False, "pass": True, "drifts": {}, "review_months": []}
    for method, weights in sorted(weights_by_method.items()):
        dts = sorted(weights)
        mx = 0.0
        for a, b in zip(dts, dts[1:]):
            wa, wb = weights[a], weights[b]
            keys = set(wa) | set(wb)
            tot = sum(wa.get(k, 0.0) for k in keys) or 1.0
            d = sum(abs(wa.get(k, 0.0) - wb.get(k, 0.0)) for k in keys) / tot
            mx = max(mx, d)
            if d >= GATE_WEIGHT_DRIFT_MAX:
                out["review_months"].append(f"{method}:{b}")
        out["drifts"][method] = round(mx, 4)
    out["triggered"] = bool(out["review_months"])
    out["pass"] = not out["review_months"]
    if out["review_months"]:
        out["note"] = (f"相邻期权重漂移 ≥ {GATE_WEIGHT_DRIFT_MAX:.0%} → "
                       f"freeze_review（{len(out['review_months'])} 期）")
    return out


def composite_scores(con: duckdb.DuckDBPyConnection, strategy: StrategyDSL,
                     metas: dict, start: str, end: str) -> dict:
    """策略合成主入口：四法并列 scores + weights + 双闸 + 因子状态诚实标注。"""
    factor_ids = [f["id"] for f in strategy.factors]
    for fid in factor_ids:
        if fid not in metas:
            raise CompositeError(f"{strategy.id}: 因子 {fid} 未注册")
    mes, fwd = forward_panels(con, start, end)
    F = aligned_z(con, metas, factor_ids, start, end)
    if not any(F[f] for f in factor_ids):
        raise CompositeError(f"{strategy.id}: 窗口内无可用因子截面")
    out_methods, out_weights = {}, {}
    for method in strategy.methods:
        scores, weights = _combine(method, F, fwd, mes, factor_ids)
        out_methods[method] = scores
        out_weights[method] = weights
    reg_status = {fid: metas[fid].status for fid in factor_ids}
    return {
        "strategy_id": strategy.id, "mode": strategy.mode,
        "factors": factor_ids, "factor_status": reg_status,
        "window": {"start": start, "end": end, "n_months": len(mes)},
        "scores": out_methods, "weights": out_weights, "mes": mes,
        "gate_consistency": consistency_gate(out_methods),
        "gate_weight_stability": weight_stability_gate(out_weights),
        "notes": strategy.notes,
    }
