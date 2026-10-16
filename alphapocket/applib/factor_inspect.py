"""M5 T-L4 季度巡检编排（计划 8.2）：三指标复算 → factorlib 纯判定 → 落库/状态同步。

依赖方向红线：applib → factorlib 单向；公式不搬家——
- IC 序列：优先 fl_factor_eval.report_json（同 lineage 最新 as_of 的 h1 series），
  无记录现场重算（复用 factor_eval_fl.run_factor_eval，不复制 Spearman 公式）；
- regime 分段：复用 factor_governance.regime_segments（基准 vs MA250 牛熊，内部做
  directed 翻转——传 raw 序列 + direction，不二次翻转）；
- 判定/落库/状态：factorlib.lifecycle.judge_health（纯函数）+ save_health_row（纯 SQL）
  + factorlib.registry.set_health（可恢复健康迁移，与 lifecycle 两维分离，巡检不改状态机）。

drill 演练（DoD 载体）：inject_decay 名单因子的 IC 序列尾部 24 月 ×0.3 再判——只影响
本次判定输入，不落 fl_factor_health、不写状态文件（artifact 标注 drill: true）。
口径透明：覆盖度 universe = 该因子物化行全史出现过的 secid（sandbox 无全 A 池，登记于
_coverage_series；生产接入全 A 池后同函数换分母即可）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb

from applib.ctx import CODE_VERSION, Ctx, new_run_id
from factorlib.lifecycle import ACTIVE_CAP, DEFAULT_HEALTH_THRESHOLDS

_BENCHMARK = "510300.SH"  # 与 M3/M4 检验基准一致


# ── 指标复算 ────────────────────────────────────────────
def _directed(series: list[tuple[str, float]], direction: str) -> list[tuple[str, float]]:
    """raw IC → directed（asc=低值优：负 IC 才是正向；与 M3/M4 directed 口径一致）。"""
    sgn = -1.0 if direction == "asc" else 1.0
    return [(d, sgn * v) for d, v in series]


def _ic_series(con, db_path: Path, repo: Path, factor_id: str, version: str,
               lineage_id: str, horizon: int = 1) -> tuple[list[tuple[str, float]], str]:
    """IC 序列来源：优先 eval 表（同 lineage 最新 as_of h1 series）；无记录现场重算。"""
    has = con.execute("SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name = 'fl_factor_eval'").fetchone()[0]
    if has:
        row = con.execute("""SELECT report_json FROM fl_factor_eval
                             WHERE factor_id = ? AND lineage_id = ? AND horizon = ?
                             ORDER BY as_of DESC LIMIT 1""",
                          [factor_id, lineage_id, horizon]).fetchone()
        if row and row[0]:
            rep = json.loads(row[0])
            r = (rep.get("reports") or {}).get(str(horizon)) or {}
            s = [(str(d), float(v)) for d, v in (r.get("ic") or {}).get("series") or []]
            if s:
                return s, "fl_factor_eval.report_json"
    from applib.factor_eval_fl import run_factor_eval
    from factorlib.registry import load_factor
    meta = load_factor(repo, factor_id)
    direction = "asc" if meta.direction == "low" else "desc"
    rep = run_factor_eval(db_path, repo, factor_id, direction, horizons=(horizon,),
                          version=version)
    r = (rep.get("reports") or {}).get(horizon) or {}
    s = [(str(d), float(v)) for d, v in (r.get("ic") or {}).get("series") or []]
    return s, "recompute(factor_eval_fl)"


def _coverage_series(con, factor_id: str, version: str, as_of: str) -> list[tuple[str, float]]:
    """逐月覆盖度 = 当月有值 secid ÷ 全史出现过的 secid（口径透明，见模块 docstring）。"""
    uni = con.execute("""SELECT count(DISTINCT secid) FROM fl_factor_value_daily
                         WHERE factor_id = ? AND factor_version = ? AND dt <= ?""",
                      [factor_id, version, as_of]).fetchone()[0]
    if not uni:
        return []
    rows = con.execute("""SELECT strftime(dt, '%Y-%m'), count(DISTINCT secid)
                          FROM fl_factor_value_daily
                          WHERE factor_id = ? AND factor_version = ? AND dt <= ?
                          GROUP BY 1 ORDER BY 1""", [factor_id, version, as_of]).fetchall()
    return [(m, n / uni) for m, n in rows]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _metric_ic_decay(series: list[tuple[str, float]],
                     window: int) -> tuple[float | None, float | None]:
    """近 window 期 directed IC 均值 ÷ 此前全史均值。

    全史不足 window、此前无基线或**基线非正** → 比值 None（unknown，近窗均值仍可判
    invalid 线）：基线 ≤ 0 时比值无定义——负基线 × 正近窗 = 负比值，会被误读为衰减，
    实际是改善（真实库 mom_20d_fl 实测案例，2026-10-11 巡检）。
    """
    if not series:
        return None, None
    vals = [v for _, v in sorted(series)]
    recent_mean = _mean(vals[-window:])
    prior = vals[:-window]
    if len(vals) < window or not prior:
        return None, recent_mean
    prior_mean = _mean(prior)
    if prior_mean <= 0:
        return None, recent_mean
    return recent_mean / prior_mean, recent_mean


def _metric_coverage(cov: list[tuple[str, float]], recent_n: int) -> float | None:
    """近 recent_n 月覆盖均值 ÷ 历史覆盖均值（历史含近窗，口径见 00-总览 §3.3）。"""
    if not cov:
        return None
    vals = [v for _, v in cov]
    hist, rec = _mean(vals), _mean(vals[-recent_n:])
    return rec / hist if hist else None


def _metric_regime(repo: Path, benchmark: str, raw_recent: list[tuple[str, float]],
                   direction: str, min_months: int) -> dict:
    """复用 M4 regime_segments（基准 vs MA250）→ {bull: bool|None, bear: bool|None}。

    段长 < min_months 或段均 None → None（样本不足，不拉低整体，透明披露）。
    """
    from applib.factor_governance import regime_segments
    if not raw_recent:
        return {"bull": None, "bear": None}
    try:
        seg = regime_segments(repo, benchmark, raw_recent, direction)["segments"]
    except Exception:                                    # 基准缺失等 → 透明降级
        return {"bull": None, "bear": None}
    out = {}
    for k in ("bull", "bear"):
        s = seg.get(k) or {}
        n, dm = s.get("n", 0), s.get("directed_mean")
        out[k] = (dm < 0) if (n >= min_months and dm is not None) else None
    return out


def _inject_decay(series: list[tuple[str, float]], window: int = 24,
                  k: float = 0.3) -> list[tuple[str, float]]:
    """演练注入：尾部 window 期 IC × k（模拟衰减，只改本次判定输入，不落盘）。"""
    s = sorted(series)
    n = min(window, len(s))
    if not n:
        return s
    return s[:-n] + [(d, v * k) for d, v in s[len(s) - n:]]


# ── 编排入口 ────────────────────────────────────────────
def run_inspection(ctx: Ctx, factors: list[str] | None = None, as_of: str | None = None,
                   drill: bool = False, inject_decay: list[str] | None = None,
                   benchmark: str = _BENCHMARK, horizon: int = 1,
                   thresholds: dict | None = None) -> dict:
    """季度巡检：逐 active 因子三指标复算 → judge_health →（非演练）落库 + 状态同步。

    - factors=None 默认全部 lifecycle=active；单因子失败透明降级 unknown，不阻断批量；
    - 巡检不改 lifecycle（两维分离红线）；unknown 也写 health（透明，reasons 带数据缺口）；
    - drill / inject_decay 任一给出即演练态：不落 fl_factor_health、不写状态文件、
      不触发降级动作/通知（演练只是 what-if）；
    - set_health 非法迁移（invalid→healthy 直通）被拒时透明披露，不影响落库记录。
    """
    from factorlib.lifecycle import judge_health, save_health_row
    from factorlib.materialize import latest_version
    from factorlib.registry import load_factor, load_registry, set_health

    db_path = ctx.sandbox_dir / "data" / "fl.duckdb"
    repo = ctx.local_repo
    day = (as_of or date.today().isoformat())[:10]
    t = {**DEFAULT_HEALTH_THRESHOLDS, **(thresholds or {})}
    inj = set(inject_decay or [])
    drill = bool(drill or inj)

    metas = load_registry(repo)
    active = [m.id for m in metas if m.status == "active"]
    factors = factors if factors is not None else active

    # 阶段 A（只读连接）：指标复算；单因子异常以 err 标记，阶段 B 统一透明降级
    gathered: dict[str, dict] = {}
    con = duckdb.connect(str(db_path), read_only=True) if db_path.exists() else None
    try:
        for fid in factors:
            g: dict = {"factor_id": fid, "version": None, "lineage_id": None,
                       "metrics": None, "err": None}
            try:
                if con is None:
                    raise RuntimeError(f"fl 库不存在：{db_path}")
                meta = load_factor(repo, fid)
                ver = latest_version(con, fid)
                if ver is None:
                    raise RuntimeError("因子未物化（fl_factor_value_daily 无行）")
                lid = con.execute("SELECT max(lineage_id) FROM fl_factor_value_daily "
                                  "WHERE factor_id = ? AND factor_version = ?",
                                  [fid, ver]).fetchone()[0]
                raw, src = _ic_series(con, db_path, repo, fid, ver, lid, horizon)
                raw = [(d, v) for d, v in raw if d <= day]            # as_of 截断
                if fid in inj:
                    raw = _inject_decay(raw)
                direction = "asc" if meta.direction == "low" else "desc"
                decay, recent_mean = _metric_ic_decay(_directed(raw, direction),
                                                      t["ic_window_months"])
                cov = _coverage_series(con, fid, ver, day)
                reg = _metric_regime(repo, benchmark, raw[-12:], direction,
                                     t["regime_min_months"])
                g.update(version=ver, lineage_id=lid, metrics={
                    "ic_decay": decay, "ic_recent_mean": recent_mean,
                    "coverage_ratio": _metric_coverage(cov, t["coverage_recent_months"]),
                    "regime_fail": reg, "n_ic_months": len(raw),
                    "ic_series_source": src + ("（drill 注入 ×0.3）" if fid in inj else ""),
                })
            except Exception as e:                    # 透明降级红线：不阻断批量
                g["err"] = f"{type(e).__name__}: {e}"
            gathered[fid] = g
    finally:
        if con is not None:
            con.close()

    # 阶段 B：判定 →（非演练）落库 + 状态同步
    results = []
    for fid in factors:
        g = gathered[fid]
        if g["err"]:
            j = judge_health({}, t)
            j["reasons"].append(f"巡检异常（透明降级）: {g['err']}")
            metrics: dict = {}
        else:
            m = g["metrics"]
            j = judge_health({k: m[k] for k in ("ic_decay", "ic_recent_mean",
                                                "coverage_ratio", "regime_fail")}, t)
            metrics = dict(m)
        row = {"factor_id": fid, "health": j["health"], "reasons": j["reasons"],
               "metrics": metrics, "version": g["version"], "lineage_id": g["lineage_id"]}
        if not drill:
            try:
                save_health_row(db_path, fid, g["version"], g["lineage_id"], day, j)
            except Exception as e:
                row["reasons"].append(f"落库失败: {type(e).__name__}: {e}")
            try:
                rec = set_health(repo, fid, j["health"], by="inspect",
                                 why=("；".join(j["reasons"]) or "巡检通过")[:200], as_of=day)
                row["health_transition"] = rec    # 降级动作据此判定「本巡检发生迁移」
            except Exception as e:                    # 如 invalid→healthy 直通被拒
                row["reasons"].append(f"健康迁移被拒: {e}")
        results.append(row)

    counts = {h: sum(1 for r in results if r["health"] == h)
              for h in ("healthy", "weakening", "invalid", "unknown")}
    run_id = new_run_id("fl_inspect")
    out = {"run_id": run_id, "as_of": day, "drill": drill, "inject_decay": sorted(inj),
           "benchmark": benchmark, "n_inspected": len(results),
           **{f"n_{k}": v for k, v in counts.items()},
           "factors": results,
           "active_vs_cap": {"active": len(active), "cap": ACTIVE_CAP},
           "thresholds": t, "code_version": CODE_VERSION}

    if not drill:                                    # T-L5 降级动作（演练态跳过）
        out.update(downgrade_actions(ctx, out))

    art_dir = ctx.artifacts_dir / run_id
    art_dir.mkdir(parents=True, exist_ok=True)
    out["artifacts"] = {"json": str(art_dir / "inspection.json"),
                        "md": str(art_dir / "inspection.md")}
    if out.get("notifications_md"):
        out["artifacts"]["notifications_md"] = out["notifications_md"]
    (art_dir / "inspection.json").write_text(          # JSON 自包含 artifacts 引用
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = _render_md(out)
    (art_dir / "inspection.md").write_text(md, encoding="utf-8")
    return out


# ── 降级动作编排（计划 8.4「自动」语义，T-L5）─────────────────────────
_INVALID_ACTIONS = (
    "出路① 假设复查确认失效 → `ap hypothesis set-status --hypothesis <id> --status refuted`"
    " → 因子人工退役（lifecycle deprecated；两维分离，巡检不自动迁移）",
    "出路② 复验后人工恢复：invalid → unknown（复查中）→ healthy，须先过假设复查"
    "（invalid→healthy 直通已被封）",
)


def downgrade_actions(ctx, out: dict) -> dict:
    """巡检尾部降级动作（judge → set_health → 落库之后接续，计划 8.4 自动语义）。

    - weakening/invalid → 必答复查项（review_items_fl，随巡检 JSON `review` 字段落盘）；
    - 本巡检发生健康迁移（set_health 返回 from≠to）→ 影响面评估：impact_report
      （被引用 evals/gates/回测摘要附进通知）——「降级前强制影响面评估」由
      「降级时自动出影响面」落地（巡检自动降级无人工环节）；
    - invalid → 通知（notifications）+ 两条出路文案；合成池摘除由 active_pool
      天然排除实现，不迁 lifecycle（两维分离红线）；
    - 全链幂等：同 as_of 重跑 set_health 幂等返回，notifications 按 factor+as_of 去重；
    - 通知落盘 fl_inspect_<runid>/notifications.md（人读）+ 巡检 JSON notifications
      字段（机读）；无外部推送渠道（sandbox 契约：artifact + CLI stdout）。
    """
    if out.get("drill"):
        return {"review": None, "notifications": []}   # 演练只是 what-if，不产生复查/通知
    from applib.hypothesis import load as hyp_load, review_items_fl
    from factorlib.registry import load_registry

    repo = Path(ctx.local_repo)
    db_path = Path(ctx.sandbox_dir) / "data" / "fl.duckdb"
    day = out["as_of"]
    metas = {m.id: m for m in load_registry(repo)}
    rows = [{"factor_id": f["factor_id"], "health": f["health"],
             "hypothesis_id": metas[f["factor_id"]].hypothesis_id if f["factor_id"] in metas else None,
             "reasons": list(f["reasons"])} for f in out["factors"]]
    review = review_items_fl(repo, hyp_load(repo), rows)

    notes: dict[tuple[str, str], dict] = {}
    for f in out["factors"]:
        if f["health"] not in ("weakening", "invalid"):
            continue
        fid = f["factor_id"]
        tr = f.get("health_transition") or {}
        migrated = bool(tr) and tr.get("as_of") == day and tr.get("from") != tr.get("to")
        impact = None
        if migrated:
            try:
                from factorlib.lifecycle import impact_report      # 迟绑定：测试可 mock
                rep = impact_report(db_path, repo, fid, version=f.get("version"))
                impact = {"version": rep["version"],
                          "n_strategies": rep["n_strategies"], "n_rows": rep["n_rows"],
                          "n_evals": len(rep["refs"]["evals"]),
                          "n_gates": len(rep["refs"]["gates"])}
            except Exception as e:                     # 影响面失败透明披露，不阻断通知
                impact = {"error": f"{type(e).__name__}: {e}"}
        notes[(fid, day)] = {                          # factor+as_of 去重键
            "factor_id": fid, "health": f["health"], "as_of": day,
            "reasons": list(f["reasons"]),
            "migration": ({"from": tr.get("from"), "to": tr.get("to")} if migrated else None),
            "impact": impact,
            "actions": list(_INVALID_ACTIONS) if f["health"] == "invalid"
                       else ["标警留池（weakening）：策略层消费 `ap fl pool` 的 warnings 提示",
                             "必答复查项已随巡检 review 字段产出（`ap fl review` 可复现）"],
        }

    res: dict = {"review": review, "notifications": list(notes.values())}
    if res["notifications"]:
        art_dir = Path(ctx.artifacts_dir) / out["run_id"]
        art_dir.mkdir(parents=True, exist_ok=True)
        p = art_dir / "notifications.md"
        p.write_text(_render_notifications_md(res["notifications"]), encoding="utf-8")
        res["notifications_md"] = str(p)
    return res


def _render_notifications_md(notes: list[dict]) -> str:
    """降级通知 MD（人读）：逐因子 标警/摘除 + 影响面摘要 + 出路文案。"""
    L = ["# 降级通知（M5 8.4 · 巡检自动降级）\n",
         "> 消费方式：本 artifact + `ap fl inspect` stdout（sandbox 无邮件/webhook 外推"
         "渠道，RUNBOOK 消费方式登记见 T-L6）。\n"]
    for n in notes:
        L.append(f"## {n['factor_id']} · {n['health']}（as_of {n['as_of']}）\n")
        if n["reasons"]:
            L.append(f"- 触发原因：{'；'.join(n['reasons'])}")
        if n["migration"]:
            L.append(f"- 健康迁移：{n['migration']['from']} → {n['migration']['to']}"
                     "（影响面评估已随本通知产出）")
        imp = n.get("impact")
        if imp:
            if imp.get("error"):
                L.append(f"- 影响面评估失败（透明披露）：{imp['error']}")
            else:
                L.append(f"- 影响面：在用策略 {imp['n_strategies']} · 物化 {imp['n_rows']} 行"
                         f" · evals {imp['n_evals']} · gates {imp['n_gates']}"
                         f"（version {imp['version']}）")
        L += [f"- {a}" for a in n["actions"]]
        L.append("")
    return "\n".join(L)


def health_rows_from_state(repo: Path) -> list[dict]:
    """状态文件 → review_items_fl 入参行（`ap fl review` 独立复查入口；仅 active 因子）。

    reasons 取 health_history 最新记录的 why（T-L4 set_health 落档）——
    与巡检时点的实时 reasons 相比是「最新落档口径」，复查场景够用且零 DB 依赖。
    """
    from factorlib.registry import get_health, load_registry
    rows = []
    for m in load_registry(repo):
        if m.status != "active":
            continue
        health, hist = get_health(repo, m.id)
        why = (hist[-1].get("why") if hist else "") or ""
        rows.append({"factor_id": m.id, "health": health, "hypothesis_id": m.hypothesis_id,
                     "reasons": [why] if why else []})
    return rows


def _render_md(out: dict) -> str:
    """巡检报告 MD：头部四元组 + 三态汇总 + 逐因子明细 + 降级动作 + 软上限 + 下一步。"""
    L = [f"# 因子库季度巡检 · {out['run_id']}\n",
         f"`as_of={out['as_of']}` · `drill={out['drill']}` · `benchmark={out['benchmark']}` · "
         f"`code={out['code_version']}`"
         + (f" · `inject_decay={','.join(out['inject_decay'])}`" if out["inject_decay"] else "")
         + "\n",
         "| 健康态 | 因子数 |", "| --- | --- |"]
    for h in ("healthy", "weakening", "invalid", "unknown"):
        L.append(f"| {h} | {out[f'n_{h}']} |")
    L += ["", f"active vs 软上限（§11 #6）：**{out['active_vs_cap']['active']} / "
              f"{out['active_vs_cap']['cap']}**\n",
          "## 逐因子明细\n",
          "| 因子 | 健康态 | IC衰减比 | 近窗IC均值 | 覆盖度比 | regime失效 | 序列来源 | 触发原因 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in out["factors"]:
        m = f.get("metrics") or {}
        rf = m.get("regime_fail") or {}
        if rf.get("bull") or rf.get("bear"):
            reg = ",".join(k for k in ("bull", "bear") if rf.get(k))
        else:
            reg = "样本不足" if rf.get("bull") is None and rf.get("bear") is None else "无"
        L.append(f"| {f['factor_id']} | {f['health']} | {m.get('ic_decay')} | "
                 f"{m.get('ic_recent_mean')} | {m.get('coverage_ratio')} | {reg} | "
                 f"{m.get('ic_series_source', '—')} | {'；'.join(f['reasons']) or '—'} |")
    bad = [f["factor_id"] for f in out["factors"] if f["health"] in ("weakening", "invalid")]
    notes = out.get("notifications") or []
    if notes:                                        # T-L5 降级动作（标警/摘除/影响面）
        L += ["", "## 降级动作（8.4 自动语义）\n"]
        for n in notes:
            tag = "⛔ 摘除合成池" if n["health"] == "invalid" else "⚠ 标警留池"
            L.append(f"- **{n['factor_id']}**（{n['health']}）→ {tag}")
            imp = n.get("impact") or {}
            if imp.get("error"):
                L.append(f"  - 影响面评估失败（透明披露）：{imp['error']}")
            elif imp:
                L.append(f"  - 影响面：策略 {imp['n_strategies']} · 物化 {imp['n_rows']} 行"
                         f" · evals {imp['n_evals']} · gates {imp['n_gates']}")
            for a in n["actions"]:
                L.append(f"  - {a}")
    rv = out.get("review")
    if rv:
        L += ["", f"## 假设复查（T-L5）：必答 {rv['n_mandatory']} · 抽查 {rv['n_sample']}",
              "",
              f"- 必答：{'、'.join(it['factor_id'] for it in rv['mandatory']) or '—'}",
              f"- 抽查：{'、'.join(it['factor_id'] for it in rv['sample']) or '—'}\n"]
    L += ["", "## 下一步\n"]
    if out["drill"]:
        L.append("> 演练态：以上判定不落 fl_factor_health、不写状态文件（仅本次输入注入）。\n")
    if bad:
        L.append(f"- weakening/invalid 因子（{'、'.join(bad)}）→ `ap fl review` 触发假设复查"
                 "（必答/抽查）；invalid 已自动摘出合成池（`ap fl pool` 复核）。\n")
    else:
        L.append("- 全部健康：无需动作（逐季记录已落 fl_factor_health）。\n")
    return "\n".join(L)
