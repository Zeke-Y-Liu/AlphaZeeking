"""M5 生命周期（计划 §8）：激活门禁（8.1）+ 版本冻结/被引用关系（8.3，T-L3）+ 巡检/池（T-L4/T-L5 续建）。

依赖方向红线：本模块属 factorlib，禁止 import applib——
- fl_* 治理表（fl_factor_value_daily / fl_factor_eval / fl_factor_gate）duckdb 只读直连；
- 假设登记表 repo/hypothesis/registry.json 直读（仅复用其 JSON schema，R3 产物）。

激活门禁（计划 8.1 / R3 §5.2）：draft→active 唯一入口，七查——
③ 检验记录（h1 directed_mean>0 且 n_periods≥12）
④ 时间稳健（硬）/ 参数稳健（硬且非 None）/ 环境稳健（软：失效标签列示不阻断）
⑤ 增量价值（|ρ|<0.6；无既有因子可比 pass=None = 首因子冷启动通过）
⑥ 多重检验（候选 <20 演练通道；≥20 需 Bonferroni p_adj<0.05）
+ 血缘（hypothesis_id 非空且在登记表内，不悬空）
「同 lineage」守卫：以物化表当前 version 的 lineage 为准，旧版检验/治理报告不放行新版定义。

被引用关系（计划 8.3，T-L3）：refs 列出因子（版本）被哪些 eval/gate/物化行/回测 artifact
引用——降级/退役前的强制影响面评估（T-L5 消费）；impact_report 渲染 MD + artifact 落盘。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

_HYP_REGISTRY = "hypothesis/registry.json"
_MIN_PERIODS = 12          # 三年月频下限：样本过少不许激活（计划 8.1）


def _current_lineage(con, factor_id: str) -> tuple[str | None, str | None]:
    """物化表当前 version 的 (version, lineage_id)（三段式数值序取最新）。

    物化表不存在（fl_factor_value_daily 未建）= 未物化 → (None, None)，
    走激活门禁「未物化」拒因（T-F9 personal_clone 演练首次触达该路径）。
    """
    from factorlib.materialize import latest_version
    try:
        ver = latest_version(con, factor_id)
    except duckdb.CatalogException:
        return (None, None)
    if ver is None:
        return (None, None)
    lid = con.execute("SELECT max(lineage_id) FROM fl_factor_value_daily "
                      "WHERE factor_id = ? AND factor_version = ?",
                      [factor_id, ver]).fetchone()[0]
    return (ver, lid)


def _latest_eval(con, factor_id: str, lineage_id: str) -> dict | None:
    has = con.execute("SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name = 'fl_factor_eval'").fetchone()[0]
    if not has:
        return None                                    # 检验报告从未落库（透明拒因）
    row = con.execute("""SELECT eval_id, directed_mean, n_periods, as_of
                         FROM fl_factor_eval
                         WHERE factor_id = ? AND lineage_id = ? AND horizon = 1
                         ORDER BY as_of DESC LIMIT 1""",
                      [factor_id, lineage_id]).fetchone()
    if not row:
        return None
    return {"eval_id": row[0], "directed_mean": row[1], "n_periods": row[2], "as_of": row[3]}


def _latest_gates(con, factor_id: str, lineage_id: str) -> dict[str, dict]:
    """逐 gate 取同 lineage 最新行：{gate: {pass, detail(dict), as_of}}。"""
    rows = con.execute("""SELECT gate, pass, detail_json, as_of FROM fl_factor_gate
                          WHERE factor_id = ? AND lineage_id = ? ORDER BY as_of""",
                       [factor_id, lineage_id]).fetchall()
    out: dict[str, dict] = {}
    for name, p, dj, as_of in rows:            # 时序遍历 → 同名后者覆盖 = 最新
        detail = {}
        if dj:
            try:
                detail = json.loads(dj)
            except ValueError:
                detail = {}
        out[name] = {"pass": p, "detail": detail, "as_of": as_of}
    return out


def _hyp_check(repo: Path, meta) -> tuple[bool, str, str | None]:
    """血缘检查：hypothesis_id 非空且在假设登记表内（R3「无来源不激活」）。
    第三返回值 = 假设 source.type（T-F9 personal_clone 不豁免提示用）。"""
    hyp_id = meta.hypothesis_id
    if not hyp_id:
        return False, "未绑定 hypothesis_id（无来源不激活）", None
    p = repo / _HYP_REGISTRY
    if not p.exists():
        return False, f"{hyp_id} 无法核验：假设登记表不存在（{_HYP_REGISTRY}）", None
    reg = json.loads(p.read_text(encoding="utf-8"))
    hit = next((h for h in reg.get("hypotheses", []) if h.get("id") == hyp_id), None)
    if hit is None:
        return False, f"{hyp_id} 不在假设登记表（悬空血缘）", None
    return True, f"{hyp_id}《{hit.get('title', '')}》", (hit.get("source") or {}).get("type")


def _gate_row_check(name: str, row: dict | None, *, soft: bool = False) -> tuple[bool, str]:
    """gate 行判定（hard 除非 soft）：无行 = 拒（不允许「没跑过治理就激活」）。"""
    if row is None:
        return (False, "未跑" if not soft else "未跑（软检查，不阻断）")
    p = row["pass"]
    if soft:
        return (p is not False, "pass=NULL（环境关未显式判定，软通过）" if p is None
                else ("通过" if p else "存在失效环境（软检查，不阻断）"))
    if p is None:
        return (False, "pass=NULL（未完成判定）")
    return (bool(p), "通过" if p else "未通过")


def _multiplicity_check(row: dict | None) -> tuple[bool, str]:
    """⑥ 多重检验：候选 <20 演练通道有记录即过；≥20 需 Bonferroni p_adj < 0.05。"""
    if row is None:
        return False, "未跑多重检验"
    d = row["detail"]
    bon = (d or {}).get("bonferroni")
    if not bon:
        return False, "多重检验记录缺 Bonferroni 结论"
    n_trials = bon.get("n_trials", 0)
    p_adj = bon.get("p_adj")
    if n_trials < 20:
        return True, f"候选 {n_trials}<20 演练通道（p_adj={p_adj} 已记录）"
    if p_adj is None or p_adj >= 0.05:
        return False, f"候选 {n_trials}≥20 且 Bonferroni p_adj={p_adj} ≥ 0.05"
    return True, f"候选 {n_trials}≥20，Bonferroni p_adj={p_adj} < 0.05"


def activation_gate(db_path: Path, repo: Path, factor_id: str) -> dict:
    """七查激活门禁。返回 {factor_id, ok, checks, reasons}——只读治理结论，不重算。"""
    from factorlib.registry import load_factor
    meta = load_factor(repo, factor_id)
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        ver, lin = _current_lineage(con, factor_id)
        if lin is None:
            lin_tag = "未物化"
            ev = None
            gates: dict[str, dict] = {}
        else:
            lin_tag = f"{ver}/{lin[:12]}…"
            ev = _latest_eval(con, factor_id, lin)
            gates = _latest_gates(con, factor_id, lin)
    finally:
        con.close()

    # ── 血缘（硬）─────────────────────────────────────
    ok, detail, hyp_src_type = _hyp_check(repo, meta)
    checks["lineage_hyp"] = {"ok": ok, "detail": detail}
    if not ok:
        reasons.append(f"lineage_hyp: {detail}")

    # ── ③ 检验记录（硬）───────────────────────────────
    if lin is None:
        ok, detail = False, "因子未物化（fl_factor_value_daily 无行）"
    elif ev is None:
        ok, detail = False, f"无同 lineage（{lin_tag}）h1 检验记录"
    else:
        dm, n = ev["directed_mean"], ev["n_periods"]
        ok = (dm is not None and dm > 0) and (n is not None and n >= _MIN_PERIODS)
        detail = (f"h1 directed_mean={dm} / n={n}（下限 {_MIN_PERIODS}）"
                  f" / as_of={ev['as_of']} / lineage={lin_tag}")
    checks["eval"] = {"ok": ok, "detail": detail}
    if not ok:
        reasons.append(f"eval: {detail}")

    # ── ④ 时间 / 参数（硬）+ 环境（软）─────────────────
    ok, detail = _gate_row_check("time_robustness", gates.get("time_robustness"))
    checks["time"] = {"ok": ok, "detail": f"时间稳健 {detail}（as_of="
                                        f"{gates.get('time_robustness', {}).get('as_of')}）"}
    if not ok:
        reasons.append(f"time: 样本外时间关未过（{detail}）")

    ok, detail = _gate_row_check("param_robustness", gates.get("param_robustness"))
    if gates.get("param_robustness") and gates["param_robustness"]["pass"] is None:
        detail = "pass=None（未跑变体网格 → 激活前必须过参数关）"
    checks["param"] = {"ok": ok, "detail": f"参数稳健 {detail}"}
    if not ok:
        reasons.append(f"param: 参数关未过（{detail}）")

    env = gates.get("environment")
    ok, detail = _gate_row_check("environment", env, soft=True)
    if env and (env["detail"] or {}).get("failed_environments"):
        detail += f"·失效环境 {env['detail']['failed_environments']}（人工确认）"
    checks["environment"] = {"ok": ok, "soft": True, "detail": f"环境稳健 {detail}"}

    # ── ⑤ 增量价值（硬；pass=None = 首因子冷启动）───────
    inc = gates.get("incremental_value")
    if inc is None:
        ok, detail = False, "未跑增量价值关"
    elif inc["pass"] is None:
        ok, detail = True, "无既有因子可比（首因子冷启动，pass=NULL 视为通过）"
    else:
        ok, detail = bool(inc["pass"]), ("通过（|ρ|<0.6）" if inc["pass"]
                                         else "与既有因子高相关（转替代版本对比通道）")
    checks["incremental"] = {"ok": ok, "detail": f"增量价值 {detail}"}
    if not ok:
        reasons.append(f"incremental: 增量关未过（{detail}）")

    # ── ⑥ 多重检验（硬）───────────────────────────────
    ok, detail = _multiplicity_check(gates.get("multiplicity"))
    checks["multiplicity"] = {"ok": ok, "detail": detail}
    if not ok:
        reasons.append(f"multiplicity: {detail}")

    # ── T-F9 personal_clone 不豁免提示（计划 10.4 原文「不豁免任何关卡」）──
    if hyp_src_type == "personal_clone" and reasons:
        reasons.append("个人化来源不豁免检验：personal_clone 候选走同一 ①–⑦ 流水线"
                       "（计划 10.4），缺检验记录/门禁未过照拒，无个人化旁路")

    return {"factor_id": factor_id, "ok": not reasons, "checks": checks, "reasons": reasons}


def activate_factor(db_path: Path, repo: Path, factor_id: str, *, who: str = "system",
                    why: str = "") -> dict:
    """激活编排：门禁 → set_status(gate=…)。未过返回 activated=False（含拒因）。"""
    from factorlib.registry import load_factor, set_status
    meta = load_factor(repo, factor_id)            # 存在性前置
    g = activation_gate(db_path, repo, factor_id)
    if meta.status != "draft":
        return {"activated": False, "factor_id": factor_id, "gate": g,
                "reason": f"仅 draft 可激活（当前 {meta.status}）"}
    if not g["ok"]:
        return {"activated": False, "factor_id": factor_id, "gate": g,
                "reason": "激活门禁未过（一次报全，详见 reasons）"}
    set_status(repo, factor_id, "active", who=who, why=why, gate=g)
    return {"activated": True, "factor_id": factor_id, "gate": g,
            "reason": why or "门禁七查全过"}


# ── 被引用关系 / 降级影响面（计划 8.3，T-L3；T-L5 降级动作消费）──────
def _refs_backtests(repo: Path, factor_id: str, version: str | None) -> list[dict]:
    """best-effort 扫 artifacts 下回测产物中引用该因子的策略（扫不到不报错）。

    只读 artifact JSON 的 factor_spec.factors[].id 与 factor_lineages 字段（M2 产物形态），
    坏文件/缺字段透明跳过——影响面清单宁缺勿错。
    """
    adir = Path(repo).parent / "artifacts"
    if not adir.exists():
        return []
    hits: list[dict] = []
    for p in sorted(adir.rglob("*.json")):
        if "backtest" not in p.name:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        fids = [f.get("id") for f in ((data.get("factor_spec") or {}).get("factors") or [])]
        if factor_id not in fids:
            continue
        lin = (data.get("factor_lineages") or {}).get(factor_id)
        fv = lin.get("version") if isinstance(lin, dict) else None   # 旧产物无 version → None
        if version is not None and fv not in (None, version):
            continue
        hits.append({"artifact": str(p.relative_to(adir.parent)),
                     "strategy": data.get("id") or data.get("name") or p.stem,
                     "factor_version": fv})
    return hits


def refs(db_path: Path, repo: Path, factor_id: str, version: str | None = None) -> dict:
    """被引用关系聚合（降级/退役前的强制影响面查询）。

    version=None 全版本聚合；显式 version 等值过滤（evals/gates/values 同步收窄）。
    返回 {factor_id, version, evals, gates, values, backtests}——version 取显式值，
    缺省为物化表最新版本（聚合口径的展示锚点）。
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        from factorlib.materialize import latest_version
        cur = version or latest_version(con, factor_id)
        vw = "AND factor_version = ?" if version else ""
        args = [factor_id] + ([version] if version else [])
        has_eval = con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = 'fl_factor_eval'").fetchone()[0]
        evals = []
        if has_eval:
            evals = [dict(zip(("eval_id", "version", "as_of", "horizon",
                               "ic_mean", "directed_mean"), r))
                     for r in con.execute(f"""SELECT eval_id, factor_version, as_of,
                           horizon, ic_mean, directed_mean FROM fl_factor_eval
                           WHERE factor_id = ? {vw} ORDER BY as_of, horizon""",
                                          args).fetchall()]
        has_gate = con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = 'fl_factor_gate'").fetchone()[0]
        gates = []
        if has_gate:
            gates = [dict(zip(("gate_id", "gate", "pass", "as_of", "version"), r))
                     for r in con.execute(f"""SELECT gate_id, gate, pass, as_of, factor_version
                           FROM fl_factor_gate WHERE factor_id = ? {vw}
                           ORDER BY as_of, gate""", args).fetchall()]
        values = [{"version": v, "rows": n, "dt_range": [str(lo), str(hi)]}
                  for v, n, lo, hi in con.execute(f"""SELECT factor_version, count(*),
                          CAST(min(dt) AS VARCHAR), CAST(max(dt) AS VARCHAR)
                          FROM fl_factor_value_daily WHERE factor_id = ? {vw}
                          GROUP BY factor_version""", args).fetchall()]
    finally:
        con.close()
    return {"factor_id": factor_id, "version": cur,
            "evals": evals, "gates": gates, "values": values,
            "backtests": _refs_backtests(repo, factor_id, version)}


def impact_report(db_path: Path, repo: Path, factor_id: str, version: str | None = None,
                  out_dir: Path | None = None) -> dict:
    """影响面报告：refs 聚合 + MD 渲染（头部 checklist：在用策略数/关卡结论/版本行数）。

    out_dir 提供时落盘 fl_refs_<id>_<runid>/（impact.md + refs.json）并返回 artifact_dir；
    缺省只返回不落盘（查询态）。
    """
    r = refs(db_path, repo, factor_id, version=version)
    n_rows = sum(v["rows"] for v in r["values"])
    n_strats = len(r["backtests"])
    latest_gate: dict[str, dict] = {}
    for g in r["gates"]:                       # 时序遍历 → 同名后者覆盖 = 最新结论
        latest_gate[g["gate"]] = g
    gate_lines = "\n".join(
        f"| {g['gate']} | {'✓ pass' if g['pass'] else ('✗ fail' if g['pass'] is not None else '— 未判定')} "
        f"| {g['as_of']} |" for g in latest_gate.values()) or "| （无治理记录） | — | — |"
    val_lines = "\n".join(
        f"| {v['version']} | {v['rows']:,} | {v['dt_range'][0]} ~ {v['dt_range'][1]} |"
        for v in r["values"]) or "| （未物化） | 0 | — |"
    bt_lines = "\n".join(
        f"| {b['strategy']} | {b['factor_version'] or '—'} | `{b['artifact']}` |"
        for b in r["backtests"]) or "| （无在用策略引用） | — | — |"
    md = "\n".join([
        f"# 降级/退役影响面评估 · {factor_id}\n",
        f"`version={r['version']}`（version=None 为全版本聚合口径）\n",
        "## Checklist\n",
        f"- 在用策略数：**{n_strats}**（best-effort 扫 artifacts 回测产物）",
        f"- 治理关卡最新结论：{len(latest_gate)} 项",
        f"- 物化行数：{n_rows:,}（{len(r['values'])} 个版本）",
        f"- 检验报告：{len(r['evals'])} 行（fl_factor_eval）\n",
        "## 治理关卡\n",
        "| gate | 结论 | as_of |", "| --- | --- | --- |", gate_lines, "",
        "## 物化版本\n",
        "| version | 行数 | dt 范围 |", "| --- | --- | --- |", val_lines, "",
        "## 在用策略（回测引用）\n",
        "| 策略 | factor_version | artifact |", "| --- | --- | --- |", bt_lines, "",
        "> 降级动作前必须过本影响面：在用策略 >0 时摘除合成池需评估组合缺口（T-L5 联动）。\n",
    ])
    out: dict = {"factor_id": factor_id, "version": r["version"], "md": md,
                 "n_strategies": n_strats, "n_rows": n_rows, "refs": r,
                 "artifact_dir": None}
    if out_dir is not None:
        sub = Path(out_dir) / f"fl_refs_{factor_id}_{time.strftime('%Y%m%d_%H%M%S')}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "impact.md").write_text(md, encoding="utf-8")
        (sub / "refs.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        out["artifact_dir"] = str(sub)
    return out


# ── 季度巡检：三指标三态判定（计划 8.2，T-L4）────────────────────────
# 指标复算（IC 序列/覆盖度/regime 分段）在 applib 编排层（依赖方向 applib → factorlib
# 单向，公式不搬家）；本模块只放与 DB 无关的纯判定 + 纯 SQL 落库。
DEFAULT_HEALTH_THRESHOLDS = {
    "ic_decay": 0.5,              # 近窗均值 ÷ 此前全史均值 < 0.5 → weakening
    "coverage": 0.8,              # 近3月覆盖 ÷ 历史覆盖 < 0.8 → weakening（仅此档，不判 invalid）
    "regime_min_months": 3,       # 失效环境段最短长度
    "ic_window_months": 24,       # IC 滚动窗口（月）
    "coverage_recent_months": 3,  # 覆盖度近窗（月）
}


def judge_health(m: dict, th: dict | None = None) -> dict:
    """三指标健康判定（纯函数零 IO，单测主战场）：就低合并 invalid > weakening > healthy。

    m = {"ic_decay": float|None, "ic_recent_mean": float|None, "coverage_ratio": float|None,
         "regime_fail": {"bull": bool|None, "bear": bool|None}}
    - 指标1（滚动 IC 衰减）：近窗 directed IC 均值 ≤ 0 → invalid；衰减比 < ic_decay → weakening
    - 指标2（覆盖度漂移）：比值 < coverage → weakening（数据断供是工程问题，不判 invalid）
    - 指标3（失效环境）：单段失效 → weakening（带出待答项）；全段失效 → invalid
    - 指标数据不足（None）不拉低整体，透明并入 reasons；全指标 None → unknown（透明降级红线）
    """
    t = {**DEFAULT_HEALTH_THRESHOLDS, **(th or {})}
    m = m or {}
    decay = m.get("ic_decay")
    recent = m.get("ic_recent_mean")
    cov = m.get("coverage_ratio")
    reg = m.get("regime_fail") or {}
    bull, bear = reg.get("bull"), reg.get("bear")

    reasons: list[str] = []
    unknowns: list[str] = []
    if recent is None and decay is None:
        unknowns.append("IC衰减: 无 directed IC 序列（全史不足）")
    else:
        if recent is not None and recent <= 0:
            reasons.append(f"invalid: 近{t['ic_window_months']}月 directed IC 均值 {recent} ≤ 0")
        elif decay is not None and decay < t["ic_decay"]:
            reasons.append(f"weakening: IC 衰减比 {decay} < {t['ic_decay']}（近窗均值 {recent}）")
        if decay is None:
            unknowns.append("IC衰减比: 此前全史不可比（不足窗口或基线非正，近窗均值仍可判）")
    if cov is None:
        unknowns.append("覆盖度: 无逐月覆盖序列")
    elif cov < t["coverage"]:
        reasons.append(f"weakening: 覆盖度比 {cov} < {t['coverage']}（数据断供预警）")
    if bull is None and bear is None:
        unknowns.append("失效环境: regime 分段样本不足")
    else:
        fails = [k for k, v in (("bull", bull), ("bear", bear)) if v is True]
        if len(fails) == 2:
            reasons.append("invalid: 全段失效（bull+bear directed IC 均 < 0）")
        elif fails:
            reasons.append(f"weakening: 失效环境段 {fails}（局部失效待答 → 假设复查）")

    all_unknown = (recent is None and decay is None) and cov is None \
        and bull is None and bear is None
    if all_unknown:
        health = "unknown"
    elif any(r.startswith("invalid:") for r in reasons):
        health = "invalid"
    elif reasons:
        health = "weakening"
    else:
        health = "healthy"
    if unknowns and health == "unknown":
        reasons.extend(unknowns)                    # unknown 态：reasons 必含数据缺口说明
    elif unknowns:
        reasons.append("unknown（数据不足，不拉低整体）: " + "；".join(unknowns))
    return {"health": health, "reasons": reasons, "metrics": m, "thresholds": t}


# ── 合成池（计划 8.4 / §11 #6，T-L5）：M6 合成器的正式消费契约 ────────
ACTIVE_CAP = 30            # active 因子软上限（超限仅随池披露，不阻断）


def active_pool(db_path: Path | None, repo: Path, as_of: str | None = None) -> dict:
    """合成池快照：lifecycle == active AND health != invalid（weakening 留池带警示）。

    - 判定只依赖状态文件（health 键，get_health）+ registry——**不查 fl_factor_health**
      （表是历史，状态文件是当前真源，T-L4 已保证同步）；db_path 预留（池判定零 DB）；
    - warnings 取 health_history 最新记录的 why（T-L4 set_health 落档）→ 策略层提示（8.4）；
    - invalid 进 excluded（自动摘除语义），但**不迁 lifecycle**——退役是人工决策（两维分离）；
    - draft / deprecated 不进 pool 也不进 excluded；
    - version 取注册中心当前版本（T-L3 语义：registry version 是唯一真源，物化是投影）。
    """
    from factorlib.registry import get_health, load_registry
    pool: list[dict] = []
    excluded: list[dict] = []
    n_active = 0
    for m in load_registry(repo):                      # id 序（load_registry 保证）
        if m.status != "active":
            continue
        n_active += 1
        health, hist = get_health(repo, m.id)
        why = (hist[-1].get("why") if hist else "") or ""
        if health == "invalid":
            excluded.append({"factor_id": m.id, "version": m.version, "health": health,
                             "reason": why or "自动摘除：健康态 invalid"})
            continue
        entry: dict = {"factor_id": m.id, "version": m.version, "health": health,
                       "warnings": ([why] if (health in ("weakening", "unknown") and why)
                                    else [])}   # healthy 无警示；unknown 透明披露数据缺口
        if m.hypothesis_id:
            entry["hypothesis_id"] = m.hypothesis_id
        pool.append(entry)
    return {"n_active": n_active, "cap": ACTIVE_CAP, "pool": pool, "excluded": excluded,
            "as_of": (as_of or date.today().isoformat())[:10]}


def save_health_row(db_path: Path, factor_id: str, factor_version: str | None,
                    lineage_id: str | None, as_of: str, judge: dict) -> dict:
    """巡检结果落 fl_factor_health（幂等：factor_id+as_of 覆盖；纯 SQL，零 applib 依赖）。"""
    con = duckdb.connect(str(db_path))
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS fl_factor_health (
            factor_id TEXT, factor_version TEXT, lineage_id TEXT, as_of TEXT,
            health TEXT, reasons_json TEXT, metrics_json TEXT, created_at TEXT)""")
        con.execute("DELETE FROM fl_factor_health WHERE factor_id = ? AND as_of = ?",
                    [factor_id, str(as_of)])
        con.execute("INSERT INTO fl_factor_health VALUES (?,?,?,?,?,?,?,?)",
                    [factor_id, factor_version, lineage_id, str(as_of), judge["health"],
                     json.dumps(judge.get("reasons", []), ensure_ascii=False),
                     json.dumps(judge.get("metrics", {}), ensure_ascii=False, default=str),
                     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")])
    finally:
        con.close()
    return {"factor_id": factor_id, "as_of": str(as_of), "health": judge["health"]}
