"""M4：稳健性三关 + 增量价值 + 多重检验（《实施计划》§7 ④⑤⑥）。

- ④ 时间稳健：walk-forward 扩展窗口滚动验证（train/valid/step 可配），数据末端
  lockbox 封存（滚动不含、单独一次性验证记录，不参与判定）；门槛：样本外 IC 衰减
  < 50%、无单一年度贡献 > 50%（均为 directed 口径）
- ④ 参数稳健：核心参数 ±30% 网格——变体经「定义文本替换 / transform 覆盖」生成
  （lineage 随定义文本变化，可复现性保持），物化进临时 db（主库零污染）；
  门槛：方向不反转、IC_IR 变化 < 40%
- ④ 环境稳健：牛熊分段（基准 vs 250 日均线）directed IC + 失效环境标签
  （大小盘/成长价值分段留待风格指数入库后启用，接口就绪）
- ⑤ 增量价值：与既有因子共同截面日的因子值 Spearman 均值（|ρ| < 0.6 可注册，
  否则转替代版本对比通道）
- ⑥ 多重检验：Bonferroni（NW-t 双侧 p × N）+ Deflated Sharpe Ratio
  （Bailey & López de Prado 2014）；候选 < 20 时仅演练流程

统计函数零第三方依赖（正态 CDF/分位数用标准库 statistics.NormalDist）。
关卡结论落库 fl_factor_gate（四元组 + 逐 gate JSON）。
"""
from __future__ import annotations

import json
import math
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

import duckdb

_ND = NormalDist()
_EULER_GAMMA = 0.5772156649015329


class GateError(Exception):
    """治理关卡请求非法。"""


# ── ④.1 时间稳健：walk-forward ─────────────────────────
def _months(dts: list[str]) -> list[str]:
    """月频键（YYYY-MM），保持时序去重。"""
    out = []
    for d in dts:
        m = d[:7]
        if not out or out[-1] != m:
            out.append(m)
    return out


def walk_forward(ic_series: list[tuple[str, float]], direction: str,
                 train_months: int = 24, valid_months: int = 6, step_months: int = 6,
                 lockbox_months: int = 12) -> dict:
    """扩展窗口滚动：train=[0,t)（含全历史），valid=[t,t+valid)。

    折窗口按「IC 序列覆盖的月序号」推进；末端 lockbox 月剔除滚动，仅一次性验证。
    all IC 均以 directed 口径（desc 不变，asc 取负）参与衰减与年度贡献。
    """
    if direction not in ("asc", "desc"):
        raise GateError(f"direction 非法：{direction!r}")
    if train_months < 3 or valid_months < 1 or step_months < 1:
        raise GateError("train/valid/step 月数非法")
    sgn = 1.0 if direction == "desc" else -1.0
    ser = [(d, sgn * v) for d, v in ic_series]          # directed IC 时序
    months = _months([d for d, _ in ser])
    idx = {m: i for i, m in enumerate(months)}
    n_lock = min(lockbox_months, max(0, len(months) // 4))   # lockbox 上限 1/4 样本
    lock_set = set(months[len(months) - n_lock:]) if n_lock else set()
    core = [(d, v) for d, v in ser if d[:7] not in lock_set]
    cmonths = _months([d for d, _ in core])

    folds = []
    t = train_months
    while t + valid_months <= len(cmonths):
        tr = [v for d, v in core if idx[d[:7]] < t]
        va = [v for d, v in core if t <= idx[d[:7]] < t + valid_months]
        if tr and va:
            tm, vm = sum(tr) / len(tr), sum(va) / len(va)
            folds.append({"train_end_month": cmonths[t - 1] if t <= len(cmonths) else None,
                          "valid_months_n": len(va),
                          "train_mean": round(tm, 6), "valid_mean": round(vm, 6),
                          "decay": round(vm / tm, 4) if tm > 1e-12 else None})
        t += step_months
    is_vals = [v for d, v in core if idx[d[:7]] < train_months]
    is_mean = sum(is_vals) / len(is_vals) if is_vals else None
    oos = [v for d, v in core if idx[d[:7]] >= train_months]
    oos_mean = sum(oos) / len(oos) if oos else None
    lock_vals = [v for d, v in ser if d[:7] in lock_set]
    years: dict[str, list[float]] = {}
    for d, v in core:
        years.setdefault(d[:4], []).append(v)
    year_sum = {y: round(sum(v), 6) for y, v in sorted(years.items())}
    total = sum(year_sum.values())
    top_year = max(year_sum, key=lambda y: year_sum[y]) if year_sum else None
    return {
        "n_months": len(months), "n_lockbox_months": n_lock,
        "folds": folds, "n_folds": len(folds),
        "is_mean": round(is_mean, 6) if is_mean is not None else None,
        "oos_mean": round(oos_mean, 6) if oos_mean is not None else None,
        "decay": round(oos_mean / is_mean, 4) if is_mean and oos_mean is not None and is_mean > 1e-12 else None,
        "year_sum": year_sum, "total_directed_ic": round(total, 6),
        "top_year": top_year,
        "top_year_share": round(year_sum[top_year] / total, 4) if top_year and total > 1e-12 else None,
        "lockbox": {"months": sorted(lock_set),
                    "directed_mean": round(sum(lock_vals) / len(lock_vals), 6) if lock_vals else None,
                    "n": len(lock_vals),
                    "note": "lockbox 仅登记不判定（协议：样本外只验证一次）"},
    }


def gate_time_robustness(wf: dict, decay_threshold: float = 0.5,
                         year_conc: float = 0.5) -> dict:
    """④.1 判定：样本外衰减 < 阈值 且 无单一年度贡献 > 阈值。"""
    reasons = []
    if wf["decay"] is None:
        reasons.append("样本不足，衰减不可估")
    elif wf["decay"] < decay_threshold:
        reasons.append(f"样本外衰减 {wf['decay']:.2} < {decay_threshold}（IC 衰减过半）")
    if wf["total_directed_ic"] <= 0:
        reasons.append(f"全期定向 IC ≤ 0（{wf['total_directed_ic']}）")
    elif wf["top_year_share"] is not None and wf["top_year_share"] > year_conc:
        reasons.append(f"单一年度贡献集中：{wf['top_year']} 占 {wf['top_year_share']:.0%}")
    return {"gate": "time_robustness", "pass": not reasons,
            "metrics": {k: wf[k] for k in ("is_mean", "oos_mean", "decay",
                                           "top_year_share", "n_folds")},
            "lockbox": wf["lockbox"], "reasons": reasons}


# ── ④.2 参数稳健：±30% 网格 ────────────────────────────
def _apply_repl(text: str, pairs: list[list[str]], label: str) -> str:
    for old, new in pairs:
        if old not in text:
            raise GateError(f"变体 {label!r}：定义文本未命中 {old!r}（网格无效）")
        text = text.replace(old, new)
    return text


def param_grid_eval(db_path: Path, repo: Path, meta, variants: list[dict],
                    horizons: tuple[int, ...] = (1, 3, 6), n_groups: int = 10,
                    start: str | None = None, end: str | None = None) -> dict:
    """参数网格：变体物化进临时 db（主库只读），逐变体重跑检验。

    variants: [{label, replace?: [[old,new],...], transform?: [...]}]
      replace 逐对替换 meta.expr/sql/udf 文本（未命中即报错——防静默空网格）；
      transform 直接覆盖变换链（如 winsorize 分位 / rank 开关）。
    """
    from applib.factor_eval_fl import run_factor_eval
    if not variants:
        raise GateError("变体网格为空")
    direction = "asc" if meta.direction == "low" else "desc"
    base = run_factor_eval(db_path, repo, meta.id, direction, horizons=horizons,
                           n_groups=n_groups, start=start, end=end)
    rows = []
    tmp_db = Path(db_path).with_name(f"_grid_{meta.id}.duckdb")
    try:
        shutil.copy(db_path, tmp_db)
        for i, v in enumerate(variants):
            label = v.get("label") or f"variant_{i}"
            expr = meta.expr and _apply_repl(meta.expr, v.get("replace", []), label)
            sql = meta.sql and _apply_repl(meta.sql, v.get("replace", []), label)
            udf = meta.udf and _apply_repl(meta.udf, v.get("replace", []), label)
            vm = replace(meta, id=f"{meta.id}__grid{i}", expr=expr, sql=sql, udf=udf,
                         transform=v.get("transform", meta.transform),
                         status="active", version=f"{meta.version}+grid")
            from factorlib.materialize import materialize
            materialize(vm, tmp_db, mode="full")   # 自管连接；外层再连会触发双配置冲突
            rep = run_factor_eval(tmp_db, repo, vm.id, direction, horizons=horizons,
                                  n_groups=n_groups, start=start, end=end)
            for h, r in rep["reports"].items():
                rows.append({"variant": label, "horizon": h,
                             "directed_mean": r["ic"]["directed_mean"],
                             "ic_ir": r["ic"]["ic_ir"], "n_periods": r["n_periods"]})
    finally:
        tmp_db.unlink(missing_ok=True)
    return {"base_id": meta.id, "direction": direction, "base": base,
            "rows": rows, "labels": [v.get("label") for v in variants]}


def gate_param_robustness(grid: dict, ir_tolerance: float = 0.4) -> dict:
    """④.2 判定：各变体与基线同 horizon 对比——方向不反转 + IC_IR 相对变化 < 40%。"""
    from itertools import groupby
    base = {r["horizon"]: r for r in
            [{"horizon": h, "directed_mean": rep["ic"]["directed_mean"],
              "ic_ir": rep["ic"]["ic_ir"]}
             for h, rep in grid["base"]["reports"].items()]}
    reasons, detail = [], []
    by_variant = groupby(sorted([r for r in grid["rows"]], key=lambda r: r["variant"]),
                         key=lambda r: r["variant"])
    for label, grp in by_variant:
        for r in grp:
            b = base.get(r["horizon"])
            if b is None or b["directed_mean"] is None or r["directed_mean"] is None:
                continue
            flip = b["directed_mean"] * r["directed_mean"] < 0
            ir_delta = (abs(r["ic_ir"] - b["ic_ir"]) / abs(b["ic_ir"])
                        if b["ic_ir"] and r["ic_ir"] is not None else None)
            if flip:
                reasons.append(f"{label}@{r['horizon']}M 方向反转："
                               f"{r['directed_mean']:.4f} vs 基线 {b['directed_mean']:.4f}")
            if ir_delta is not None and ir_delta > ir_tolerance:
                reasons.append(f"{label}@{r['horizon']}M IC_IR 漂移 {ir_delta:.0%} > {ir_tolerance:.0%}")
            detail.append({"variant": label, "horizon": r["horizon"], "flip": flip,
                           "ic_ir_delta": round(ir_delta, 4) if ir_delta is not None else None})
    return {"gate": "param_robustness", "pass": not reasons,
            "n_variants": len(grid["labels"]), "detail": detail, "reasons": reasons}


# ── ④.3 环境稳健：牛熊分段 ────────────────────────────
def regime_segments(source: Path | dict, benchmark: str,
                    ic_series: list[tuple[str, float]],
                    direction: str, ma_days: int = 250) -> dict:
    """牛 = 基准收盘 > MA{ma_days}（满窗判定，预热期不计分段）；分段 directed IC 与失效环境标签。

    source 可传 repo 路径（读 market/daily_bars.json）或基准收盘序列 {dt: close}（测试注入）。
    """
    bench = source if isinstance(source, dict) else _load_closes(source)[0].get(benchmark)
    if not bench:
        raise GateError(f"基准 {benchmark} 无行情")
    dts = sorted(bench)
    ma = {d: sum(bench[x] for x in dts[i - ma_days + 1: i + 1]) / ma_days
          for i, d in enumerate(dts) if i + 1 >= ma_days}
    sgn = 1.0 if direction == "desc" else -1.0
    segs: dict[str, list[float]] = {"bull": [], "bear": []}
    n_unknown = 0
    for d, v in ic_series:
        if d not in ma:
            n_unknown += 1
            continue
        segs["bull" if bench[d] > ma[d] else "bear"].append(sgn * v)
    out = {}
    for k, vals in segs.items():
        out[k] = {"n": len(vals), "directed_mean": round(sum(vals) / len(vals), 6)} \
            if vals else {"n": 0, "directed_mean": None}
    failed = [k for k, s in out.items() if s["n"] >= 3 and s["directed_mean"] is not None
              and s["directed_mean"] < 0]
    return {"segmentation": f"基准 {benchmark} vs MA{ma_days}", "segments": out,
            "n_unknown_regime": n_unknown, "failed_environments": failed,
            "note": "大小盘/成长价值分段待风格指数入库后启用（接口就绪）"}


def _load_closes(repo: Path) -> tuple[dict[str, dict[str, float]], dict]:
    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    closes = {s: {r["dt"]: r["close"] for r in arr if r.get("close") is not None}
              for s, arr in bars.items()}
    return closes, {}


# ── ⑤ 增量价值：因子值截面相关矩阵 ────────────────────
def cross_factor_corr(con, factor_ids: list[str], start: str | None = None,
                      end: str | None = None) -> dict:
    """两因子共同截面日因子值 Spearman 的均值矩阵（对角线恒 1）。"""
    from applib.walkforward import _spearman
    if len(factor_ids) < 2:
        raise GateError("至少两个因子才能算增量相关")
    ph = ",".join("?" * len(factor_ids))
    q = f"""SELECT dt, factor_id, secid, value FROM fl_factor_value_daily
            WHERE factor_id IN ({ph}) AND value IS NOT NULL"""
    args = list(factor_ids)
    if start:
        q += " AND dt >= ?"; args.append(start)
    if end:
        q += " AND dt <= ?"; args.append(end)
    rows = con.execute(q + " ORDER BY dt", args).fetchall()
    by_fd: dict[str, dict[str, dict[str, float]]] = {}
    for dt, fid, sec, v in rows:
        by_fd.setdefault(fid, {}).setdefault(dt, {})[sec] = v
    n = len(factor_ids)
    mat = [[1.0] * n for _ in range(n)]
    pair_days: dict[str, int] = {}
    for i in range(n):
        for j in range(i + 1, n):
            fi, fj = factor_ids[i], factor_ids[j]
            common = sorted(set(by_fd.get(fi, {})) & set(by_fd.get(fj, {})))
            rhos = []
            for dt in common:
                si, sj = by_fd[fi][dt], by_fd[fj][dt]
                both = sorted(set(si) & set(sj))
                if len(both) >= 3:
                    r = _spearman([si[s] for s in both], [sj[s] for s in both])
                    if r is not None:
                        rhos.append(r)
            m = sum(rhos) / len(rhos) if rhos else None
            mat[i][j] = mat[j][i] = round(m, 4) if m is not None else None
            pair_days[f"{fi}|{fj}"] = len(rhos)   # 字符串键：报告整体可 JSON 序列化
    return {"factor_ids": factor_ids, "matrix": mat, "pair_days": pair_days,
            "note": "ρ = 共同截面日因子值横截面 Spearman 均值"}


def gate_incremental(corr: dict, factor_id: str, threshold: float = 0.6) -> dict:
    """⑤ 判定：与任一既有因子 |ρ| ≥ 阈值 → 拒绝注册，转替代版本对比通道。"""
    ids = corr["factor_ids"]
    reasons = []
    for j, other in enumerate(ids):
        if other == factor_id:
            continue
        rho = corr["matrix"][ids.index(factor_id)][j]
        if rho is not None and abs(rho) >= threshold:
            reasons.append(f"与 {other} |ρ|={abs(rho):.2f} ≥ {threshold} → 转替代版本对比通道")
    return {"gate": "incremental_value", "pass": not reasons,
            "threshold": threshold, "reasons": reasons}


# ── ⑥ 多重检验 ─────────────────────────────────────────
def bonferroni(nw_t: float, n_trials: int) -> dict:
    """NW-t 双侧 p 值 × N（族错误率校正，cap 1）。"""
    if n_trials < 1:
        raise GateError("n_trials ≥ 1")
    p = 2.0 * (1.0 - _ND.cdf(abs(nw_t)))
    return {"p": round(p, 6), "n_trials": n_trials,
            "p_adj": round(min(1.0, p * n_trials), 6)}


def deflated_sharpe(ic_ir: float, n: int, n_trials: int,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """Deflated Sharpe Ratio（把 IC 序列视作周期收益，SR=IC_IR）。

    SR₀ = √V·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]，V = (1−γ₃SR+((γ₄−1)/4)SR²)/(T−1)；
    DSR = Φ((SR−SR₀)·√(T−1)/√(1−γ₃SR+((γ₄−1)/4)SR²))。N=1 时退化为 PSR(0)。
    """
    if n_trials < 1 or n < 2:
        raise GateError("n_trials ≥ 1 且样本 ≥ 2")
    v = 1.0 - skew * ic_ir + (kurt - 1) / 4.0 * ic_ir ** 2      # 逐期方差因子
    var_sr = v / (n - 1)                                        # SR 估计量方差
    sr0 = 0.0 if n_trials == 1 else math.sqrt(max(var_sr, 1e-18)) * (
        (1 - _EULER_GAMMA) * _ND.inv_cdf(1 - 1.0 / n_trials)
        + _EULER_GAMMA * _ND.inv_cdf(1 - 1.0 / (n_trials * math.e)))
    dsr = _ND.cdf((ic_ir - sr0) * math.sqrt(n - 1) / math.sqrt(max(v, 1e-18)))
    return {"sr": round(ic_ir, 6), "sr0": round(sr0, 6), "n_trials": n_trials,
            "dsr": round(dsr, 6), "pass": dsr >= 0.95}


# ── 汇总：全关卡 + 落库 ────────────────────────────────
def run_full_gauntlet(db_path: Path, repo: Path, meta, horizons=(1, 3, 6),
                      n_groups: int = 10, start: str | None = None,
                      end: str | None = None, variants: list[dict] | None = None,
                      existing: list[str] | None = None,
                      as_of: str | None = None, code_version: str | None = None) -> dict:
    """④（时间/参数/环境）+ ⑤ + ⑥ 一次跑完，返回逐 gate 结论 dict。"""
    from applib.factor_eval_fl import run_factor_eval
    direction = "asc" if meta.direction == "low" else "desc"
    rep = run_factor_eval(db_path, repo, meta.id, direction, horizons=horizons,
                          n_groups=n_groups, start=start, end=end)
    h1 = rep["reports"].get(1) or next(iter(rep["reports"].values()))
    ic_series = [(d, v) for d, v in h1["ic"]["series"]]

    wf = walk_forward(ic_series, direction)
    g_time = gate_time_robustness(wf)
    regimes = regime_segments(repo, rep["protocol"]["benchmark"], ic_series, direction)
    g_grid = {"gate": "param_robustness", "pass": None, "reasons": ["未提供变体网格"]}
    if variants:
        grid = param_grid_eval(db_path, repo, meta, variants, horizons=horizons,
                               n_groups=n_groups, start=start, end=end)
        g_grid = gate_param_robustness(grid)

    corr = None
    g_incr = {"gate": "incremental_value", "pass": None, "reasons": ["无既有因子可比"]}
    if existing:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            corr = cross_factor_corr(con, [meta.id] + [f for f in existing if f != meta.id],
                                     start=start, end=end)
        finally:
            con.close()
        g_incr = gate_incremental(corr, meta.id)

    nw = h1["ic"].get("nw_t")
    multi = None
    if nw is not None:
        n_trials = max(1, (len(existing or [])) + len(variants or []))
        sgn = 1.0 if direction == "desc" else -1.0
        multi = {"bonferroni": bonferroni(nw, n_trials),
                 "deflated_sharpe": deflated_sharpe(sgn * h1["ic"]["ic_ir"],
                                                    h1["ic"]["n"], n_trials)
                 if h1["ic"].get("ic_ir") else None,
                 "note": "DSR 以 directed 口径（方向声明后）计算；候选 < 20 时仅演练流程（协议 §7.5）"}

    return {
        "factor_id": meta.id, "direction": direction,
        "factor_version": rep["factor_version"], "lineage_id": rep["lineage_id"],
        "as_of": as_of or rep["as_of"], "code_version": code_version,
        "ic_h1": {k: h1["ic"][k] for k in ("mean", "directed_mean", "ic_ir", "nw_t", "n")},
        "gates": {"time_robustness": g_time, "param_robustness": g_grid,
                  "environment": regimes, "incremental_value": g_incr,
                  "multiplicity": multi},
        "all_pass": all(g.get("pass") for g in (g_time, g_grid, g_incr)),
        "walk_forward": wf, "corr": corr,
    }


def save_gate_report(res: dict, db_path: Path) -> list[str]:
    """关卡结论落库 fl_factor_gate（逐 gate 一行，同因子重跑幂等覆盖）。"""
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS fl_factor_gate (
            gate_id TEXT, factor_id TEXT, factor_version TEXT, lineage_id TEXT,
            as_of TEXT, code_version TEXT, gate TEXT, pass BOOLEAN,
            detail_json TEXT, created_at TEXT)""")
        created = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        saved = []
        for name, g in res["gates"].items():
            gid = f"GATE-{res['factor_id']}-{name}-{res['as_of']}"
            p = g.get("pass")
            con.execute("DELETE FROM fl_factor_gate WHERE gate_id = ?", [gid])
            con.execute("INSERT INTO fl_factor_gate VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [gid, res["factor_id"], res["factor_version"], res["lineage_id"],
                         res["as_of"], res["code_version"], name, p,
                         json.dumps(g, ensure_ascii=False, default=str), created])
            saved.append(gid)
    finally:
        con.close()
    return saved
