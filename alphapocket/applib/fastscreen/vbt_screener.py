"""M7 T-F8：vectorbt 快筛通道（分支 A，探测档案见 sandbox/artifacts/m7_gaps/pip_probe.md）。

职责（计划 §10.2「vectorbt 参数网格快筛 → 精验引擎复核」）：
- 信号侧：与自研同口径的再平衡日截面合成（equal → composite_scores；
  ic_12m/ic_ir/lasso → composite_multi_meta，与 run_factor_backtest 逐参数一致）；
- 撮合侧：vectorbt Portfolio.from_orders（targetpercent + cash_sharing）无约束近似
  —— 无 T+1 / 无涨跌停 / 无整手，成本 = fees（双边比率）+ slippage（价格乘子）；
- 网格：top_n × method × slippage（CLI `--grid top_n=3,5,8`，slippage 缺省 0/5/10bps）；
- 对照：快筛（close 无约束）vs 自研精验（run_factor_backtest 全约束 T+1 VWAP），
  产物 = 排序一致性（Spearman ρ，手写实现零新依赖）+ 逐组合 cagr 差异表，如实披露。

诚实口径：
- 快筛用途是「参数网格排序筛选」，不是精验数值（close 时点 vs T+1 VWAP 天然有撮合差）；
- vectorbt fees 参数不能区分买卖方向 → 印花税（卖出单向）不可表达，差额逐项进差异表。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import vectorbt as vbt  # 新依赖：只允许 fastscreen 内 import（T-F8 红线，静态测试守护）

from applib import backtest as BT
from applib.ctx import Ctx
from applib.factor_backtest import FactorBacktestError, composite_scores, load_section
from applib.strategy import load_strategy

GRID_SLIPPAGES = (0.0, 0.0005, 0.0010)   # 0 / 5bps / 10bps（对齐主链滑点 5bps 缺省）
INIT_CASH = 1e8
METHODS_ALL = ("equal", "ic_12m", "ic_ir", "lasso")
# T-F7 v2 归档锚区间（m6_bt_*-v2：2018-05-02 → 2026-10-13，n_days=2050）
DEFAULT_START, DEFAULT_END = "2018-05-02", "2026-10-13"


# ── 信号侧：与自研同口径的截面合成面板 ──────────────────
def _resolve_versions(con, fids: list[str], pin: str | None) -> dict[str, str]:
    """版本解析（与 run_factor_backtest 同口径）：pin 全局钉版；缺省各因子最新版。"""
    from factorlib.materialize import latest_version
    versions: dict[str, str] = {}
    missing = []
    for fid in fids:
        v = pin or latest_version(con, fid)
        lid = None
        if v is not None:
            row = con.execute(
                "SELECT max(lineage_id) FROM fl_factor_value_daily "
                "WHERE factor_id = ? AND factor_version = ?", [fid, v]).fetchone()
            lid = row[0] if row else None
        if v is None or lid is None:
            missing.append(fid)
            continue
        versions[fid] = v
    if missing:
        raise FactorBacktestError(
            f"因子未物化（或 pin 版本无行）：{missing}（先 factorlib.materialize）")
    return versions


def build_scores(db_path: Path, repo: Path, spec: dict,
                 methods: list[str] | None = None,
                 start: str | None = None, end: str | None = None
                 ) -> tuple[dict[str, pd.DataFrame], dict]:
    """逐再平衡日截面 → {method: score DataFrame(index=交易日轴, columns=secid)}。

    equal 走 composite_scores（主链无 methods 分支）；ic_12m/ic_ir/lasso 走
    composite_multi_meta（与 run_factor_backtest T-C2 四法并列同参同口径）。
    非再平衡日行 = NaN（下游 from_orders NaN = 不下单，保持持仓）。
    start/end：区间过滤（与 run_factor_backtest 同序——过滤后再推再平衡日）；
    缺省取 T-F7 v2 归档锚区间（2018-05-02..2026-10-13）。
    """
    factors = spec.get("factors") or []
    if not factors:
        raise FactorBacktestError("spec.factors 不能为空")
    fids = [f["id"] for f in factors]
    directions = {f["id"]: f.get("direction", "desc") for f in factors}
    ms = [str(m) for m in (methods or ["equal"])]
    bad = [m for m in ms if m not in METHODS_ALL]
    if bad:
        raise FactorBacktestError(f"method 非法：{bad}（可选 {METHODS_ALL}）")

    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    universe = sorted(bars)
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in universe]))
    start = start or DEFAULT_START
    end = end or DEFAULT_END
    dates = [d for d in dates if start <= d <= end]
    if not dates:
        raise FactorBacktestError(f"区间内无交易日：{start}..{end}")
    rebal_dates = sorted(BT._rebalance_dates(dates, str(spec.get("rebalance") or "monthly")))
    closes = {s: {r["dt"]: float(r["close"]) for r in rows} for s, rows in bars.items()}
    pin = spec.get("factor_version") or None

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        versions = _resolve_versions(con, fids, pin)
        panels = {m: pd.DataFrame(np.nan, index=dates, columns=universe, dtype=float)
                  for m in ms}
        for d in rebal_dates:
            sections = {fid: load_section(con, fid, d, version=versions[fid])
                        for fid in fids}
            if any(not s for s in sections.values()):
                raise FactorBacktestError(
                    f"{d}: 因子截面缺失（{[k for k, v in sections.items() if not v]}）")
            need_multi = any(m != "equal" for m in ms)
            by_m: dict[str, dict] = {}
            if need_multi:
                from applib.composite import (
                    build_lasso_data,
                    composite_multi_meta,
                    load_ic_history,
                )
                history = load_ic_history(con, fids, directions, closes, as_of=d,
                                          versions=versions)
                lasso_data = build_lasso_data(con, fids, directions, closes, as_of=d,
                                              versions=versions)
                scores_by_m, _meta = composite_multi_meta(
                    sections, factors, history, as_of=d, lasso_data=lasso_data)
                by_m = {m: scores_by_m.get(m) or {} for m in ms}
            else:
                by_m = {"equal": composite_scores(sections, factors)}
            for m, scores in by_m.items():
                for s, v in scores.items():
                    panels[m].at[d, s] = float(v)
    finally:
        con.close()
    meta = {"dates": dates, "rebal_dates": rebal_dates, "versions": versions,
            "universe": universe, "methods": ms}
    return panels, meta


# ── 撮合侧：vectorbt 无约束近似 + 参数网格 ───────────────
def _close_panel(bars: dict, universe: list[str], dates: list[str]) -> pd.DataFrame:
    px = pd.DataFrame(np.nan, index=dates, columns=universe, dtype=float)
    for s in universe:
        for r in bars[s]:
            px.at[r["dt"], s] = float(r["close"])
    return px


def _size_panel(score: pd.DataFrame, top_n: int) -> pd.DataFrame:
    """再平衡日行 = Top-N 等权目标；其余行 NaN（from_orders NaN = 不下单）。"""
    size = pd.DataFrame(np.nan, index=score.index, columns=score.columns, dtype=float)
    for d, row in score.iterrows():
        vals = row.dropna()
        if vals.empty:
            continue
        sel = sorted(vals.index, key=lambda s: (-vals[s], s))[:max(1, int(top_n))]
        for s in sel:
            size.at[d, s] = 1.0 / len(sel)
    return size


def run_vbt_grid(px: pd.DataFrame, panels: dict[str, pd.DataFrame], *,
                 top_ns: list[int], slippages: list[float], fees: float) -> list[dict]:
    """top_n × method × slippage 网格 → 每组合 {total_return, cagr, n_orders, nav_end}。

    cagr 与主链同基数：(nav_end/nav_0)^(252/n_days)-1（n_days = 日收益条数 = 交易日数）。
    call_seq='auto'：同日换仓先卖后买（cash_sharing 语义下必须，行为确定性）。
    """
    rows = []
    for method, score in panels.items():
        for n in top_ns:
            size = _size_panel(score, n)
            for slip in slippages:
                pf = vbt.Portfolio.from_orders(
                    px, size, size_type="targetpercent", group_by=True,
                    cash_sharing=True, init_cash=INIT_CASH,
                    fees=fees, slippage=slip, call_seq="auto", freq="D")
                nav = pf.value()
                nav = nav.dropna()
                if len(nav) < 2 or float(nav.iloc[0]) <= 0:
                    raise FactorBacktestError(f"vbt 网格组合空转：{method}/top_n={n}")
                rets = nav.pct_change().dropna()
                total = float(nav.iloc[-1] / nav.iloc[0]) - 1.0
                nd = len(rets)
                cagr = (float(nav.iloc[-1] / nav.iloc[0])) ** (252.0 / nd) - 1.0 if nd else 0.0
                rows.append({"method": method, "top_n": int(n), "slippage": float(slip),
                             "total_return": round(total, 6),
                             "cagr": round(cagr, 6),
                             "n_orders": len(pf.orders.records_readable),
                             "n_days": nd})
    return rows


# ── 对照：快筛排序 vs 自研精验排序 ──────────────────────
def _spearman(a: list[float], b: list[float]) -> float:
    """手写 Spearman ρ（并列取平均秩），零新依赖。"""
    n = len(a)
    if n < 2 or len(b) != n:
        return float("nan")

    def _ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((x - mb) ** 2 for x in rb))
    return num / (da * db) if da and db else float("nan")


def crosscheck_self(db_path: Path, repo: Path, spec: dict, *,
                    top_ns: list[int], start: str, end: str,
                    want_gross: bool = False) -> list[dict]:
    """自研精验锚点：同区间 run_factor_backtest（全约束 T+1 VWAP，equal 主选股法）。

    返回 [{top_n, self_total_return, self_cagr}]；want_gross=True 附加零成本轨
    （strategy_metrics_gross，next_vwap 路径双轨）供交叉分解（T-F8 偏差审查）。
    """
    from applib.factor_backtest import run_factor_backtest
    out = []
    for n in top_ns:
        s = dict(spec)
        s["top_n"] = int(n)
        bt = run_factor_backtest(db_path, repo, s, start=start, end=end)
        row = {"top_n": int(n),
               "self_total_return": round(float(bt["strategy_metrics"]["total_return"]), 6),
               "self_cagr": round(float(bt["strategy_metrics"]["cagr"]), 6)}
        if want_gross and bt.get("strategy_metrics_gross"):
            row["self_cagr_gross"] = round(float(bt["strategy_metrics_gross"]["cagr"]), 6)
        out.append(row)
    return out


def align_rows(grid: list[dict], self_rows: list[dict]) -> dict:
    """equal 法网格行 × 自研锚点 → 对照表 + Spearman + 差异逐项。"""
    self_map = {r["top_n"]: r for r in self_rows}
    vbt_map = {r["top_n"]: r for r in grid if r["method"] == "equal"
               and abs(r["slippage"] - GRID_SLIPPAGES[1]) < 1e-12}
    tops = sorted(set(self_map) & set(vbt_map))
    diffs = [{"top_n": n,
              "vbt_cagr": vbt_map[n]["cagr"], "self_cagr": self_map[n]["self_cagr"],
              "cagr_diff": round(vbt_map[n]["cagr"] - self_map[n]["self_cagr"], 6)}
             for n in tops]
    rho = _spearman([vbt_map[n]["cagr"] for n in tops],
                    [self_map[n]["self_cagr"] for n in tops]) if len(tops) >= 2 else float("nan")
    return {"pairs": diffs, "spearman_equal": (None if math.isnan(rho) else round(rho, 4)),
            "n_pairs": len(tops)}


# ── 归档与 CLI ──────────────────────────────────────────
def render_md(sid: str, grid: list[dict], align: dict, slippages: list[float]) -> str:
    L = [f"# vectorbt 快筛网格 × 自研精验对照 —— {sid}", "",
         (f"- 快筛口径：`Portfolio.from_orders(targetpercent)` 无约束近似"
         f"（无 T+1 / 无涨跌停 / 无整手）；fees={0.00026:g}（双边佣金+过户费）"),
         "- 对照口径：自研 run_factor_backtest（全约束 T+1 VWAP，equal 主选股法，同区间）",
         "- 红线：快筛是排序工具，网格最优须回自研精验复核后采纳；对照不宣称「一致」", ""]
    L.append("| method | top_n | slippage | total_return | cagr | n_orders |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in grid:
        L.append(f"| {r['method']} | {r['top_n']} | {r['slippage']:.4f} "
                 f"| {r['total_return']:.4%} | {r['cagr']:.4%} | {r['n_orders']} |")
    L += ["", "## equal 法对照（vbt 5bps ≈ 主链滑点缺省）", "",
          "| top_n | vbt cagr | 自研精验 cagr | 差 |", "| --- | --- | --- | --- |"]
    for p in align["pairs"]:
        L.append(f"| {p['top_n']} | {p['vbt_cagr']:.4%} | {p['self_cagr']:.4%} "
                 f"| {p['cagr_diff']:+.4%} |")
    L += ["", (f"- 排序一致性（equal，Spearman ρ）：**{align['spearman_equal']}**"
              f"（n={align['n_pairs']}）"),
          ("- 差异来源（如实披露，禁止宣称一致）：① 撮合时点 close vs T+1 VWAP；"
          "② 约束集（T+1/涨跌停/整手 defer/skip）；③ 印花税单向不可表达"
          "（vbt fees 双边，卖出端少计 0.05%）；④ 份额/现金机制差（targetpercent vs 份额制）")]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m applib.fastscreen.vbt_screener")
    ap.add_argument("--strategy", required=True, help="策略 YAML 路径")
    ap.add_argument("--grid", default="top_n=3,5,8", help="网格，如 top_n=3,5,8")
    ap.add_argument("--slippages", default=None,
                    help="逗号分隔小数，缺省 0,0.0005,0.001（0/5bps/10bps）")
    ap.add_argument("--methods", default="equal",
                    help="快筛 method 维（equal 或 equal,ic_12m,ic_ir,lasso）")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    args = ap.parse_args(argv)

    ctx = Ctx()
    db_path = ctx.sandbox_dir / "data" / "fl.duckdb"
    spec = load_strategy(Path(args.strategy), ctx.local_repo, db_path)
    sid = spec["id"]
    top_ns = [int(x) for x in args.grid.split("=", 1)[1].split(",") if x.strip()]
    slips = ([float(x) for x in args.slippages.split(",")]
             if args.slippages else list(GRID_SLIPPAGES))
    methods = [m for m in args.methods.split(",") if m.strip()]

    panels, meta = build_scores(db_path, ctx.local_repo, spec, methods=methods,
                                start=args.start, end=args.end)
    bars_all = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    bars = {s: [r for r in rows if args.start <= r["dt"] <= args.end]
            for s, rows in bars_all.items()}
    px = _close_panel(bars, meta["universe"], meta["dates"])
    # vbt fees = 双边佣金 + 过户费（印花税卖出单向不可表达 → 差异表披露）
    fees = BT.DEFAULT_COST_TABLE["commission_rate"] + BT.DEFAULT_COST_TABLE["transfer_fee_rate"]
    grid = run_vbt_grid(px, panels, top_ns=top_ns, slippages=slips, fees=fees)
    dates = meta["dates"]
    self_rows = crosscheck_self(db_path, ctx.local_repo, spec, top_ns=top_ns,
                                start=dates[0], end=dates[-1])
    align = align_rows(grid, self_rows)

    adir = ctx.artifacts_dir / "m7_cross"
    adir.mkdir(parents=True, exist_ok=True)
    payload = {"strategy": sid, "grid": grid, "align": align,
               "rebal_dates": meta["rebal_dates"], "versions": meta["versions"],
               "disclosure": {
                   "constraints": "无 T+1/无涨跌停/无整手（近似撮合）",
                   "fees_mapping": "fees=佣金0.025%+过户费0.001%（双边）；印花税0.05%卖出单向不可表达",
                   "cost_bps_base": BT.DEFAULT_COST_BPS}}
    (adir / f"vbt_grid_{sid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (adir / f"vbt_grid_{sid}.md").write_text(
        render_md(sid, grid, align, slips), encoding="utf-8")
    print(f"[vbt_screener] {sid}: 网格 {len(grid)} 组合 → {adir / f'vbt_grid_{sid}.md'}")
    print(f"  equal 对照 Spearman ρ={align['spearman_equal']}（n={align['n_pairs']}）")
    for p in align["pairs"]:
        print(f"  top_n={p['top_n']}: vbt={p['vbt_cagr']:+.4%} "
              f"self={p['self_cagr']:+.4%} diff={p['cagr_diff']:+.4%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
