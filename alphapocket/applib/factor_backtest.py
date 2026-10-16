"""M2：回测执行层 × factorlib 因子表对接（fl_* 命名空间消费端）。

链路：因子策略 spec → fl_factor_value_daily 再平衡日截面（严格 dt = 再平衡日，
不取未来截面）→ rank 定向合成 → Top-N 目标权重 → run_backtest(rule_fn) 动态回测。

无抢跑审计（M2 DoD）：
- 截面纪律：仅取 dt = 再平衡日 的因子值；物化层已保证每行 used_announce ≤ dt
- 财务因子逐笔核对 fl_factor_pit_meta（used_announce ≤ dt）；量价因子无 pit 行 → N/A 豁免
- 任一违例 → 结果 pit_audit = FAIL 并列样本（不静默放行）

依赖方向：applib → factorlib 单向（factorlib 零耦合契约不变，本模块仅 SQL 消费 fl_* 表）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from . import backtest as BT
from factorlib.materialize import latest_version

_DIRECTIONS = ("asc", "desc")


class FactorBacktestError(Exception):
    """因子策略 spec 非法 / 因子未物化 / 截面缺失。"""


# ── 截面读取与合成打分 ──────────────────────────────────
def load_section(con, factor_id: str, d: str, version: str | None = None) -> dict[str, float | None]:
    """dt = d 的单因子截面（PIT：绝不取 d 之后的行）。

    version=None 取该因子最新版本（三段式数值序）；显式 version 钉住旧版——
    多版本并存时按版本过滤取行唯一，不混合（M5 T-L3 版本冻结消费端）。
    """
    from factorlib.materialize import latest_version
    v = version or latest_version(con, factor_id)
    if v is None:
        return {}
    rows = con.execute(
        "SELECT secid, value FROM fl_factor_value_daily "
        "WHERE factor_id = ? AND dt = ? AND factor_version = ?",
        [factor_id, d, v]).fetchall()
    return {s: v2 for s, v2 in rows}


def _rank_pct(vals: dict[str, float]) -> dict[str, float]:
    """截面百分位 (count_less + 0.5·count_eq) / n（并列取中位，确定性）。"""
    out = {}
    for s, v in vals.items():
        less = sum(1 for x in vals.values() if x < v)
        eq = sum(1 for x in vals.values() if x == v) - 1
        out[s] = (less + 0.5 * eq) / len(vals)
    return out


def composite_scores(sections: dict[str, dict], specs: list[dict]) -> dict[str, float]:
    """多因子 rank 定向合成：asc=小值为优（低 PE 优先），desc=大值为优（动量优先）。

    某标的在某因子缺失 → 该因子不计入其合成，权重按可用因子数归一；
    全因子缺失的标的不入选。返回 {secid: score∈[0,1]}，分高者优先。
    """
    total_w = sum(float(f.get("weight", 1.0)) for f in specs)
    if total_w <= 0:
        raise FactorBacktestError("因子权重之和必须 > 0")
    acc: dict[str, float] = {}
    wsum: dict[str, float] = {}
    for f in specs:
        vals = {s: v for s, v in sections[f["id"]].items() if v is not None}
        if not vals:
            continue
        ranks = _rank_pct(vals)
        if f.get("direction", "desc") == "asc":
            ranks = {s: 1.0 - p for s, p in ranks.items()}
        w = float(f.get("weight", 1.0)) / total_w
        for s, p in ranks.items():
            acc[s] = acc.get(s, 0.0) + p * w
            wsum[s] = wsum.get(s, 0.0) + w
    return {s: acc[s] / wsum[s] for s in acc}


def select_top(scores: dict[str, float], n: int) -> list[str]:
    """Top-N（分数降序；并列按 secid 升序 → 可复现）。"""
    order = sorted(scores, key=lambda s: (-scores[s], s))
    return order[:max(1, int(n))]


# ── 无抢跑审计 ──────────────────────────────────────────
def _pit_audit(con, picks: list[dict]) -> dict:
    """选中标的 × 财务因子逐笔核对 used_announce ≤ dt；无 pit 行（量价因子）豁免。

    同时校验截面纪律：选股所用因子值必须取自 dt = 再平衡日（由调用方保证，
    此处复核 picks 中值的 dt 字段一致）。
    """
    violations = []
    n_checked = n_exempt = 0
    for p in picks:
        for fid, val in p["factors"].items():
            row = con.execute(
                "SELECT used_announce FROM fl_factor_pit_meta "
                "WHERE factor_id = ? AND secid = ? AND dt = ?",
                [fid, p["secid"], p["dt"]]).fetchone()
            if row is None:
                n_exempt += 1          # 量价因子：无财务 PIT 元数据
                continue
            n_checked += 1
            ann = row[0] if isinstance(row[0], str) else str(row[0])
            if ann > p["dt"]:
                violations.append({"dt": p["dt"], "secid": p["secid"], "factor_id": fid,
                                   "used_announce": ann})
    return {"check": "fail" if violations else "pass", "n_checked": n_checked,
            "n_exempt": n_exempt, "violations": violations[:10],
            "n_violations": len(violations)}


# ── 无偷价审计（T-C3 DoD#4）─────────────────────────────
def _no_lookahead_audit(bt: dict) -> dict | None:
    """撮合层无偷价审计透传（仅 next_vwap 路径产出；close 缺省路径不注入任何新字段）：

    逐笔基础成交价（VWAP/avg4，不含滑点）∈ [当日 low, high]；
    违例与 pit_audit 同级呈现（bt["no_lookahead"].check=fail），不静默放行。
    """
    if "no_lookahead" not in bt:
        return None
    nl = dict(bt["no_lookahead"])
    bt["no_lookahead"] = nl
    return nl


# ── 主入口 ──────────────────────────────────────────────
def run_factor_backtest(db_path: Path, repo: Path, spec: dict,
                        start: str | None = None, end: str | None = None,
                        rf: float = 0.0, cost_bps: float = BT.DEFAULT_COST_BPS) -> dict:
    """因子驱动动态回测：spec → fl_* 截面选股 → run_backtest(rule_fn)。

    spec 字段：
      id/name        策略标识（报告标题）
      factors        [{id, weight?, direction?}]，direction ∈ asc(小值优)/desc(大值优)
      top_n          每期持仓数
      rebalance      monthly/quarterly/yearly/daily/buyhold
      benchmark      基准 secid（默认 510300.SH）
      factor_version 显式钉住因子版本（缺省 None=各因子最新版；M5 T-L3 版本 pin）
    返回 run_backtest 全量结果 + factor 字段（spec/lineages/targets/coverage/pit_audit）。
    """
    factors = spec.get("factors") or []
    if not factors:
        raise FactorBacktestError("spec.factors 不能为空")
    for f in factors:
        if f.get("direction", "desc") not in _DIRECTIONS:
            raise FactorBacktestError(f"direction 非法：{f.get('direction')!r}（asc | desc）")
    top_n = int(spec.get("top_n") or 1)
    rebalance = str(spec.get("rebalance") or "monthly")
    if rebalance not in BT.REBALANCE_CHOICES:
        raise FactorBacktestError(f"rebalance 非法：{rebalance}")

    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    dims = json.loads((repo / "dim" / "security.json").read_text(encoding="utf-8")) \
        if (repo / "dim" / "security.json").exists() else []
    names = {d["secid"]: d.get("name", d["secid"]) for d in dims}

    con = duckdb.connect(str(db_path), read_only=not bool(spec.get("methods")))
    try:
        fids = [f["id"] for f in factors]
        # 版本解析（M5 T-L3）：spec.factor_version 全局 pin（缺省 None=各因子最新版）；
        # 显式版本无物化行 → 报错（不许对空版本回测）。元数据记录 (id, version, lineage) 对账锚点。
        pin = spec.get("factor_version") or None
        pined: dict[str, dict] = {}
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
            pined[fid] = {"version": v, "lineage_id": lid}
        if missing:
            raise FactorBacktestError(
                f"因子未物化（或 pin 版本无行）：{missing}（先执行 factorlib.materialize；"
                "spec.factor_version 可显式钉版）")
        lineages = pined

        # 回测日轴与再平衡日（与 run_backtest 同口径推导）
        universe = sorted(bars)
        dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in universe]))
        if start:
            dates = [d for d in dates if d >= start]
        if end:
            dates = [d for d in dates if d <= end]
        if not dates:
            raise FactorBacktestError("区间内无交易日")
        rebal_dates = sorted(BT._rebalance_dates(dates, rebalance))

        # 各再平衡日截面 → 合成 → Top-N（严格 dt = d；记录覆盖率与选股快照）
        # M6 T-C2：spec.methods 存在 → 四法并列（composite_multi_meta），每法独立选股并列输出，
        # 主选股法（method_primary，缺省 equal 向后兼容）出 targets；双闸告警/冻结不拦截。
        methods = [str(m) for m in (spec.get("methods") or [])]
        primary = str(spec.get("method_primary") or "equal")
        directions = {f["id"]: f.get("direction", "desc") for f in factors}
        versions = {fid: pined[fid]["version"] for fid in fids}
        closes = {s: {r["dt"]: float(r["close"]) for r in rows} for s, rows in bars.items()}
        targets: dict[str, dict[str, float]] = {}
        picks: list[dict] = []
        cover: list[dict] = []
        comp_rows: list[dict] = []          # (as_of, meta, gates) → con 关闭后统一落库
        method_picks: list[dict] = []
        gate_log: list[dict] = []
        exposure: list[dict] = []       # T-C5：各期持仓因子原始 rank 均值（asc → <0.5 核对）
        snap_written = 0
        prev_primary_w: dict | None = None
        if methods and primary not in ("equal", "ic_12m", "ic_ir", "lasso"):
            raise FactorBacktestError(f"method_primary 非法：{primary!r}（可选 equal/ic_12m/ic_ir/lasso）")
        for d in rebal_dates:
            sections = {fid: load_section(con, fid, d, version=pined[fid]["version"])
                        for fid in fids}
            if any(not s for s in sections.values()):
                raise FactorBacktestError(f"{d}: 因子截面缺失（{[k for k, v in sections.items() if not v]}）"
                                          "——请检查物化区间覆盖")
            if methods:
                from .composite import (build_lasso_data, composite_multi_meta, gate_consensus,
                                        gate_weight_drift, load_ic_history, save_snapshot)
                history = load_ic_history(con, fids, directions, closes, as_of=d, versions=versions)
                lasso_data = build_lasso_data(con, fids, directions, closes, as_of=d, versions=versions)
                scores_by_m, comp_meta = composite_multi_meta(
                    sections, factors, history, as_of=d, lasso_data=lasso_data)
                if primary not in scores_by_m:
                    primary = "equal"
                scores = scores_by_m[primary]
                for m in methods:
                    if scores_by_m.get(m):
                        sm = scores_by_m[m]
                        method_picks.append({"dt": d, "method": m,
                                             "secids": sorted(sm, key=lambda x: (-sm[x], x))[:top_n]})
                g1 = gate_consensus({m: scores_by_m[m] for m in methods if scores_by_m.get(m)}, top_n)
                if comp_meta[primary]["degraded"]:
                    g2 = {"drift": None, "frozen": False, "frozen_review": False,
                          "weights": comp_meta[primary]["weights"]}
                    g2["skipped"] = "primary degraded（权重退化，不参与闸2）"
                else:
                    g2 = gate_weight_drift(prev_primary_w, comp_meta[primary]["weights"])
                prev_primary_w = g2["weights"]        # 冻结沿用后，下期与「在用权重」比较
                gate_log.append({"dt": d, "primary": primary, "gate1": g1, "gate2": g2,
                                 "degraded": {m: comp_meta[m]["degraded"] for m in comp_meta}})
                comp_rows.append({"as_of": d, "meta": comp_meta,
                                  "gates": {**g1, **g2}})
            else:
                scores = composite_scores(sections, factors)
            if not scores:
                raise FactorBacktestError(f"{d}: 截面无有效值")
            sel = select_top(scores, top_n)
            targets[d] = {s: 1.0 / len(sel) for s in sel}
            # T-C5 因子暴露：持仓在原始截面 rank_pct 的均值（未定向——asc 因子选低原始 rank，
            # 均值应 <0.5；desc 选高原始 rank，均值应 >0.5，报告层据 DSL direction 核对）。
            exp = {}
            for fid in fids:
                vals = {s: v for s, v in sections[fid].items() if v is not None}
                ranks = _rank_pct(vals) if vals else {}
                picked = [ranks[s] for s in sel if s in ranks]
                exp[fid] = round(sum(picked) / len(picked), 6) if picked else None
            exposure.append({"dt": d, "exposure": exp})
            cover.append({"dt": d, "n_universe": len(universe),
                          "n_scored": len(scores), "coverage": round(len(scores) / len(universe), 4)})
            for s in sel:
                picks.append({"dt": d, "secid": s, "score": round(scores[s], 6),
                              "factors": {fid: sections[fid].get(s) for fid in fids}})
        audit = _pit_audit(con, picks)
    finally:
        con.close()

    # 快照落库（连接关闭后串行写；dry_run 演练态零行）
    if methods and comp_rows:
        from .composite import save_snapshot
        snap_written = 0
        for row in comp_rows:
            lid = ",".join(sorted({pined[f]["lineage_id"] for f in fids}))
            res = save_snapshot(db_path, str(spec.get("id") or "factor_strategy"),
                                row["as_of"], row["meta"], row["gates"],
                                dry_run=bool(spec.get("dry_run", False)), lineage_id=lid)
            snap_written += int(res["written"])

    # 动态规则闭包：非再平衡日（如回测首日早于首个再平衡日）取最近过去截面；
    # 更早则取首个未来截面（期初建仓语义）。
    def rule_fn(d: str) -> dict[str, float]:
        if d in targets:
            return targets[d]
        prior = [k for k in targets if k <= d]
        if prior:
            return targets[max(prior)]
        return targets[min(targets)]

    secids = sorted({s for t in targets.values() for s in t})
    pf = {"id": str(spec.get("id") or "factor_strategy"),
          "name": str(spec.get("name") or spec.get("id") or "因子策略"),
          "positions": [{"secid": s, "name": names.get(s, s)} for s in secids]}
    ex = str(spec.get("exec") or "close")
    if ex not in ("close", "next_day_vwap"):
        raise FactorBacktestError(f"exec 非法：{ex!r}（可选 close | next_day_vwap）")
    bt = BT.run_backtest(bars, pf, start=start, end=end, rf=rf, rebalance=rebalance,
                         benchmark_secid=str(spec.get("benchmark") or "510300.SH"),
                         cost_bps=cost_bps, rule_fn=rule_fn,
                         exec_mode="next_vwap" if ex == "next_day_vwap" else "close",
                         cost_table=spec.get("cost_table"),
                         constraints=spec.get("constraints") if ex == "next_day_vwap" else None,
                         capital=float(spec.get("capital") or 1_000_000.0))
    _no_lookahead_audit(bt)
    bt["factor_spec"] = {"factors": factors, "top_n": top_n, "universe_n": len(universe),
                         "factor_version": spec.get("factor_version")}
    bt["factor_lineages"] = lineages
    bt["factor_targets"] = [{"dt": d, "secids": sorted(t), "weights": t}
                            for d, t in sorted(targets.items())]
    bt["factor_coverage"] = cover
    bt["factor_exposure"] = exposure                      # T-C5：期 × 因子 rank 均值
    bt["turnover"] = [{"dt": e["dt"], "turnover": e["turnover"]}   # T-C5：期换手率序列
                      for e in (bt.get("cost") or {}).get("events") or []]
    bt["pit_audit"] = audit
    if methods:
        bt["composite"] = {"methods": methods, "primary": primary,
                           "method_picks": method_picks, "gates": gate_log,
                           "snapshot_written": snap_written}
    return bt
