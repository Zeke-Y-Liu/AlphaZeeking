"""M3：单因子检验流水线（月度截面协议，消费 fl_* 因子表）。

协议（《中长期因子挖掘与回测-实施计划》§6，中长期口径）：
- 截面频率：每月末最后一个交易日（walkforward._month_end_dates 同口径）
- 前瞻收益：未来 1/3/6 个月（月末序列 index+h 的 close 链收益）
- 核心指标：Spearman Rank IC 时序 → IC 均值 / IC_IR / Newey-West 调整 t 值
- 分层检验：等宽分组（L1=低值 … Ln=高值），多头组超额（vs 基准 close 链）+
  定向多空价差 + 单调性（层序 vs 层均收益的 Spearman）
- 辅助指标：覆盖度、多头组月度换手（相邻期重合率）

PIT 纪律（与 M2 同源）：
- 因子值严格取 dt = 截面日（fl_factor_value_daily，物化层已保证 used_announce ≤ dt）
- 前瞻收益是「被解释变量」，允许使用截面日之后的行情——不构成未来函数
- 收益链任一端缺失（停牌/未上市/退市）→ 该标的该期剔除并透明计数

自研兜底：Spearman/分层/IC 全部不依赖 alphalens/scipy（库版本无关）。
报告落库 fl_factor_eval（对应计划 meta_factor_eval，按 M1 契约 fl_* 命名空间），
挂 factor_id + version + lineage_id + as_of + code_version 可复现四元组。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from applib.walkforward import _month_end_dates, _spearman

_HORIZONS = (1, 3, 6)


class FactorEvalError(Exception):
    """检验请求非法 / 因子未物化 / 区间无有效截面。"""


# ── 统计量 ──────────────────────────────────────────────
def _nw_lag(n: int) -> int:
    """Newey-West (1994) 自动带宽：lag = ⌊4·(T/100)^(2/9)⌋，至少 1。"""
    return max(1, int(4 * (n / 100) ** (2 / 9)))


def nw_t_stat(series: list[float]) -> float | None:
    """IC 均值的 Newey-West 调整 t 值（HAC：Bartlett 核修正自相关）。"""
    n = len(series)
    if n < 2:
        return None
    mu = sum(series) / n
    x = [v - mu for v in series]
    s0 = sum(v * v for v in x) / n
    lag = _nw_lag(n)
    for l in range(1, lag + 1):
        w = 1.0 - l / (lag + 1)
        s_l = sum(x[i] * x[i - l] for i in range(l, n)) / n
        s0 += 2.0 * w * s_l
    var_mean = s0 / n
    if var_mean <= 0:
        return None
    return mu / (var_mean ** 0.5)


def _summary(ics: list[tuple[str, float]], direction: str) -> dict:
    """IC 时序 → 均值 / IC_IR / 普通 t / Newey-West t / 胜率 / 定向均值。"""
    vals = [v for _, v in ics]
    n = len(vals)
    if not n:
        return {"n": 0, "series": []}
    mu = sum(vals) / n
    sd = (sum((v - mu) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t_plain = mu / (sd / n ** 0.5) if sd > 0 else None
    nw = nw_t_stat(vals) if n > 1 else None      # 退化序列（零方差 IC）→ None（边界防御）
    sgn = 1.0 if direction == "desc" else -1.0   # asc：低值优 → 负 IC 才是「正向」
    return {
        "n": n, "series": [(d, round(v, 6)) for d, v in ics],
        "mean": round(mu, 6), "std": round(sd, 6),
        "ic_ir": round(mu / sd, 4) if sd > 0 else None,
        "t_stat": round(t_plain, 4) if t_plain is not None else None,
        "nw_t": round(nw, 4) if nw is not None else None,
        "win_rate": round(sum(1 for v in vals if v > 0) / n, 4),
        "directed_mean": round(sgn * mu, 6),       # 按方向翻转：>0 = 符合预期方向
        "direction": direction,
    }


# ── 数据装载 ────────────────────────────────────────────
def _closes(repo: Path) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    closes = {s: {r["dt"]: r["close"] for r in arr if r.get("close") is not None}
              for s, arr in bars.items()}
    try:
        dims = json.loads((repo / "dim" / "security.json").read_text(encoding="utf-8"))
        names = {d["secid"]: d.get("name", d["secid"]) for d in dims}
    except (OSError, ValueError):
        names = {}
    return closes, names


def _load_factor_meta(con, factor_id: str, version: str | None = None) -> dict:
    """因子版本元数据：version=None 取最新版（三段式数值序）；显式版本须有物化行。"""
    from factorlib.materialize import latest_version
    v = version or latest_version(con, factor_id)
    if v is None:
        raise FactorEvalError(f"因子未物化：{factor_id}（先执行 factorlib.materialize）")
    row = con.execute("SELECT max(lineage_id) FROM fl_factor_value_daily "
                      "WHERE factor_id = ? AND factor_version = ?",
                      [factor_id, v]).fetchone()
    if not row or not row[0]:
        raise FactorEvalError(f"因子 {factor_id} 版本 {v} 未物化"
                              "（显式 pin 版本须先 materialize，不许对空版本出报告）")
    return {"version": v, "lineage_id": row[0]}


# ── 主入口 ──────────────────────────────────────────────
def run_factor_eval(db_path: Path, repo: Path, factor_id: str, direction: str,
                    horizons: tuple[int, ...] = _HORIZONS, n_groups: int = 10,
                    start: str | None = None, end: str | None = None,
                    benchmark: str = "510300.SH", as_of: str | None = None,
                    code_version: str | None = None,
                    version: str | None = None) -> dict:
    """月度截面检验：因子 IC 时序 + 分层单调性 + 多头超额，返回可序列化报告 dict。

    version（M5 T-L3）：None=最新版；显式钉版走该版本物化行（多版本并存不混合取行）。
    """
    if direction not in ("asc", "desc"):
        raise FactorEvalError(f"direction 非法：{direction!r}（asc=低值优 | desc=高值优）")
    for h in horizons:
        if h not in _HORIZONS:
            raise FactorEvalError(f"horizon 非法：{h}（可选 {_HORIZONS}）")
    if n_groups < 2:
        raise FactorEvalError("n_groups 至少 2")

    closes, names = _closes(repo)
    universe = sorted(closes)
    dates = sorted(set().union(*[set(c) for c in closes.values()]))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    if len(dates) < 3:
        raise FactorEvalError("区间交易日不足")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        meta = _load_factor_meta(con, factor_id, version)
        month_ends = _month_end_dates(dates)
        sections = {d: dict(con.execute(
            "SELECT secid, value FROM fl_factor_value_daily "
            "WHERE factor_id = ? AND factor_version = ? AND dt = ?",
            [factor_id, meta["version"], d]).fetchall())
            for d in month_ends}
    finally:
        con.close()

    # ── 截面 × 前瞻收益矩阵（逐 horizon）────────────────
    reports: dict[int, dict] = {}
    for h in horizons:
        ics: list[tuple[str, float]] = []
        layer_rets: dict[int, list[float]] = {}     # layer → 各期收益
        top_sets: list[set[str]] = []               # 多头组逐期名单（换手用）
        top_rets: list[float] = []                  # 多头组逐期收益（超额用）
        bench_rets: list[float] = []                # 基准同期收益
        n_dropped_gap = n_dropped_degenerate = 0
        cover: list[dict] = []
        n_eff = None
        for i, d in enumerate(month_ends):
            j = i + h
            if j >= len(month_ends):                # 前瞻窗口超出区间
                continue
            d2 = month_ends[j]
            sec = {s: v for s, v in sections.get(d, {}).items() if v is not None}
            if not sec:
                n_dropped_degenerate += 1
                continue
            rets = {}
            for s in sec:
                c0, c1 = closes.get(s, {}).get(d), closes.get(s, {}).get(d2)
                if c0 is None or c1 is None or c0 == 0:
                    n_dropped_gap += 1              # 收益链断裂（停牌/退市）
                    continue
                rets[s] = c1 / c0 - 1.0
            cover.append({"dt": d, "n_factor": len(sec), "n_ret": len(rets),
                          "coverage": round(len(sec) / len(universe), 4)})
            if len(rets) < 3:
                n_dropped_degenerate += 1
                continue
            ic = _spearman([sec[s] for s in rets], list(rets.values()))
            if ic is None:                          # 因子或收益零方差截面
                n_dropped_degenerate += 1
                continue
            ics.append((d, ic))
            b0, b1 = closes.get(benchmark, {}).get(d), closes.get(benchmark, {}).get(d2)
            if b0 and b1:
                bench_rets.append(b1 / b0 - 1.0)    # 与多头收益同期对齐

            # ── 分层（样本决定实际层数，全局一致）────────
            k = min(n_groups, len(rets))
            n_eff = k if n_eff is None else min(n_eff, k)
            order = sorted(rets, key=lambda s: (sec[s], s))     # 升序；并列按 secid
            for L in range(1, k + 1):
                grp = order[(L - 1) * len(rets) // k: L * len(rets) // k]
                if grp:
                    layer_rets.setdefault(L, []).append(
                        sum(rets[s] for s in grp) / len(grp))
            top_L = k if direction == "desc" else 1
            grp_top = order[(top_L - 1) * len(rets) // k: top_L * len(rets) // k]
            if grp_top:
                top_sets.append(set(grp_top))
                top_rets.append(sum(rets[s] for s in grp_top) / len(grp_top))

        s = _summary(ics, direction)
        layer_mean = {L: round(sum(v) / len(v), 6) for L, v in sorted(layer_rets.items())}
        mono = _spearman(list(layer_mean), list(layer_mean.values())) \
            if len(layer_mean) >= 3 else None
        bottom_L = 1 if direction == "desc" else max(layer_mean, default=1)
        top_L_dir = max(layer_mean, default=1) if direction == "desc" else 1
        spread = layer_mean.get(top_L_dir) - layer_mean.get(bottom_L) \
            if layer_mean else None
        n_over = [len(top_sets[i] & top_sets[i - 1]) / len(top_sets[i])
                  for i in range(1, len(top_sets))] if top_sets else []
        top_excess = (sum(top_rets) - sum(bench_rets)) / len(top_rets) \
            if top_rets and len(bench_rets) == len(top_rets) else None
        reports[h] = {
            "ic": s, "n_periods": s["n"],
            "n_dropped_gap": n_dropped_gap, "n_dropped_degenerate": n_dropped_degenerate,
            "layer_mean": layer_mean, "n_groups_eff": n_eff,
            "monotonicity": round(mono, 4) if mono is not None else None,
            "directed_spread": round(spread, 6) if spread is not None else None,
            "top_mean": round(sum(top_rets) / len(top_rets), 6) if top_rets else None,
            "top_excess_per_period": round(top_excess, 6) if top_excess is not None else None,
            "top_turnover": round(1 - sum(n_over) / len(n_over), 4) if n_over else None,
            "coverage_mean": (round(sum(c["coverage"] for c in cover) / len(cover), 4)
                              if cover else None),
        }

    return {
        "factor_id": factor_id, "direction": direction,
        "factor_version": meta["version"], "lineage_id": meta["lineage_id"],
        "as_of": as_of or dates[-1], "code_version": code_version,
        "protocol": {"freq": "monthly", "horizons": list(horizons),
                     "n_groups": n_groups, "window": [dates[0], dates[-1]],
                     "benchmark": benchmark, "universe_n": len(universe),
                     "factor_source": "fl_factor_value_daily（严格 dt=截面日）"},
        "names": names, "coverage_last": None,   # 占位：逐期覆盖度在 reports[*].coverage
        "reports": reports,
    }


# ── 报告落库（计划 meta_factor_eval → fl_factor_eval）───
def _ensure_eval_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS fl_factor_eval (
        eval_id TEXT, factor_id TEXT, factor_version TEXT, lineage_id TEXT,
        as_of TEXT, code_version TEXT, horizon INTEGER, n_periods INTEGER,
        ic_mean DOUBLE, ic_ir DOUBLE, nw_t DOUBLE, directed_mean DOUBLE,
        top_excess DOUBLE, monotonicity DOUBLE, top_turnover DOUBLE,
        report_json TEXT, created_at TEXT)""")


def save_eval_report(rep: dict, db_path: Path) -> dict:
    """报告（逐 horizon 一行）写入 fl_factor_eval；同 eval_id 重跑幂等覆盖。"""
    con = duckdb.connect(str(db_path))
    try:
        _ensure_eval_table(con)
        created = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        saved = []
        for h, r in rep["reports"].items():
            eid = f"EVAL-{rep['factor_id']}-h{h}-{rep['as_of']}"
            ic = r["ic"]
            con.execute("DELETE FROM fl_factor_eval WHERE eval_id = ?", [eid])
            con.execute("""INSERT INTO fl_factor_eval VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [eid, rep["factor_id"], rep["factor_version"], rep["lineage_id"],
                         rep["as_of"], rep["code_version"], h, r["n_periods"],
                         ic.get("mean"), ic.get("ic_ir"), ic.get("nw_t"),
                         ic.get("directed_mean"), r.get("top_excess_per_period"),
                         r.get("monotonicity"), r.get("top_turnover"),
                         json.dumps({**rep, "reports": {h: r}}, ensure_ascii=False),
                         created])
            saved.append(eid)
    finally:
        con.close()
    return {"saved": saved}


def run_market_eval(db_path: Path, repo: Path, factor_id: str, markets: list[str]) -> dict:
    """T-F9 一键重检验触发（多市场空跑口径，计划 10.3 HK 空跑演练锚）。

    - CN 段：照常 M3 重检验（run_factor_eval + save_eval_report 落 fl_factor_eval）；
    - 扩展市场段（HK/US）：就绪性披露（factorlib.account_map.market_readiness）——
      科目映射/行情准入/财务源缺口逐条列示，**不抛异常不静默**（「结果可另议」的诚实实现：
      预检披露 ≠ 重检验，M8+ 分市场重过 §6.4 才产出结论）。
    """
    from factorlib.account_map import market_readiness
    from factorlib.registry import load_factor
    meta = load_factor(repo, factor_id)
    direction = {"high": "desc", "low": "asc", "both": "asc"}.get(meta.direction, "asc")
    out: dict = {"factor_id": factor_id, "factor_version": meta.version,
                 "requested_markets": [str(m).upper() for m in markets], "segments": {}}
    if "CN" in out["requested_markets"]:
        rep = run_factor_eval(db_path, repo, factor_id, direction)
        save_eval_report(rep, db_path)
        h1 = rep["reports"].get(1) or next(iter(rep["reports"].values()))
        out["segments"]["CN"] = {"ok": True, "rechecked": True, "direction": direction,
                                 "h1": {"directed_mean": h1["ic"].get("directed_mean"),
                                        "ic_ir": h1["ic"].get("ic_ir"),
                                        "n_periods": h1["ic"].get("n"),
                                        "coverage": h1["ic"].get("coverage_mean")
                                        or h1.get("coverage_mean")}}
    ext = [m for m in out["requested_markets"] if m != "CN"]
    if ext:
        rd = market_readiness(repo, [{"id": meta.id, "inputs": list(meta.inputs)}],
                              ext, db_path)
        out["segments"].update(rd["markets"])
        out["readiness_note"] = rd["note"]
    return out


def render_md(rep: dict) -> str:
    """检验报告 Markdown（头部可复现四元组 + 逐 horizon 指标表 + 分层表）。"""
    L = [f"# 单因子检验报告 · {rep['factor_id']}\n",
         (f"`factor_id={rep['factor_id']}` · `version={rep['factor_version']}` · "
         f"`lineage={rep['lineage_id'][:12]}…` · `as_of={rep['as_of']}` · "
         f"`code={rep['code_version'] or '—'}`\n"),
         (f"- 方向声明：`{rep['direction']}`（{'低值为优' if rep['direction'] == 'asc' else '高值为优'}）"
         f" · 协议：{rep['protocol']['freq']} 截面 · 窗口 {rep['protocol']['window'][0]} ~ "
         f"{rep['protocol']['window'][1]} · universe {rep['protocol']['universe_n']} 标的 · "
         f"基准 {rep['protocol']['benchmark']}\n"),
         "| horizon | 有效期 | IC均值 | 定向IC | IC_IR | NW-t | 胜率 | 多头超额/期 | 定向多空 | 单调性 | 换手 | 覆盖度 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for h, r in rep["reports"].items():
        ic = r["ic"]
        L.append(f"| {h}M | {r['n_periods']} | {ic.get('mean')} | {ic.get('directed_mean')} | "
                 f"{ic.get('ic_ir')} | {ic.get('nw_t')} | {ic.get('win_rate')} | "
                 f"{r.get('top_excess_per_period')} | {r.get('directed_spread')} | "
                 f"{r.get('monotonicity')} | {r.get('top_turnover')} | {r.get('coverage_mean')} |")
    L.append("")
    for h, r in rep["reports"].items():
        if r["layer_mean"]:
            L.append(f"## 分层平均收益（{h}M，L1=低值 … {r['n_groups_eff']}=高值）\n")
            L.append("| 层 | " + " | ".join(f"L{k}" for k in r["layer_mean"]) + " |")
            L.append("| --- | " + " | ".join("---" for _ in r["layer_mean"]) + " |")
            L.append("| 均值 | " + " | ".join(f"{v:.4%}" for v in r["layer_mean"].values()) + " |")
            L.append("")
    L.append("> 剔除口径：收益链断裂（停牌/退市）与零方差截面逐期透明计数（见 JSON "
             "`n_dropped_gap / n_dropped_degenerate`）；IC 为 Spearman 秩相关，"
             "NW-t 为 Newey-West HAC 调整 t 值。\n")
    return "\n".join(L)
