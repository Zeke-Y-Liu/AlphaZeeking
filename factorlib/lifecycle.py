"""M5 注册生命周期与季度巡检（实施计划 §8，步骤 ⑦）。

8.1 激活门禁：draft → active 须 ③④⑤⑥ 全达标（CLI 强制无旁路）且必带 hypothesis_id
    （R3：无来源不激活）。③ 凭据 = meta_factor_eval 1M 行（对齐 IC > 0）；
    ④⑤⑥ 凭据 = M4 治理报告 verdict（time/param/regime/incremental 全 pass + PIT 0 违例）。
8.2 季度巡检 DAG：滚动 24 月 IC 衰减 / 覆盖度漂移 / 失效环境（复用 ④c regime 口径）
    → healthy | weakening | invalid；「假设仍成立吗」复查步：weakening/invalid 自动带出
    假设 statement 生成待答项，healthy 按 20% 确定性抽查（按 id 哈希序，可复现）。
8.3 版本冻结：激活即快照定义（def_hash = sha256 计算体规范 JSON）；verify_frozen 校验
    定义未漂移（漂移未 bump version = 拒绝重跑 → 凡能重跑必与激活时 bit 级一致）；
    被引用关系查询（strategy_factor_ref）支撑降级前强制影响面评估。
8.4 降级动作：weakening 标警（factor_health 可查）；invalid 自动 deprecated +
    从合成池摘除通知（登记动作并列出引用影响面；合成池本体 M6 落地）。

诚实口径（CP-0007）：巡检对 draft/deprecated 因子显式跳过不裁决；库内仅 4 股票截面时
覆盖度/环境轴与 ④c 同源退化，按标注不误杀。依赖纪律：纯标准库 + duckdb。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import duckdb

from .evaluate import DEFAULT_BENCHMARK, _git_sha, build_universe_db, cross_sections
from .governance import ic_series, regime_gate
from .registry import (STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_DRAFT, FactorMeta,
                       load_definitions, set_status)

# ── 巡检阈值（§8 冻结口径） ──
INSPECT_ROLLING_MONTHS = 24     # 滚动窗：最近 24 月 vs 之前 24 月
INSPECT_DECAY_MAX = 0.50        # 与 ④a 时间关同口径
INSPECT_MIN_RECENT = 6          # recent 有效月下限（不足 → 衰减降级标注，不误杀）
INSPECT_COVER_DROP = 0.20       # 覆盖率较基期下降 ≥ 20pp → weakening
SPOT_CHECK_RATE = 0.20          # healthy 抽查比例（R3）

ARCHIVE_DDL = """CREATE TABLE IF NOT EXISTS meta_factor_archive (
    factor_id TEXT, version TEXT, def_hash TEXT, def_snapshot TEXT,
    status TEXT, activated_at TEXT, git_sha TEXT)"""
INSPECTION_DDL = """CREATE TABLE IF NOT EXISTS meta_factor_inspection (
    factor_id TEXT, version TEXT, as_of TEXT, verdict TEXT, reason TEXT,
    metrics_json TEXT, actions_json TEXT, created_at TEXT)"""
REFS_DDL = """CREATE TABLE IF NOT EXISTS strategy_factor_ref (
    strategy_id TEXT, factor_id TEXT, version TEXT, registered_at TEXT)"""


class LifecycleError(ValueError):
    """生命周期违例（门禁/冻结/降级：失败即拒，无静默降级）。"""


# ── 假设注册表（R3：无来源不激活） ──
def load_hypotheses(root: str | Path) -> dict:
    """加载 hypotheses.yaml：{hypothesis_id: {statement, status, created_at}}。"""
    p = Path(root) / "hypotheses.yaml"
    if not p.exists():
        return {}
    import yaml
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    bad = [k for k, v in doc.items()
           if not isinstance(v, dict) or not (v.get("statement") or "").strip()]
    if bad:
        raise LifecycleError(f"假设注册表条目缺 statement：{bad}")
    return doc


# ── 版本冻结凭据（8.3） ──
_DEF_FIELDS = ("engine", "body", "transform", "direction", "params",
               "lookback_days", "universe", "depends")


def def_hash(meta: FactorMeta) -> tuple[str, str]:
    """计算体规范快照 → (sha256, snapshot JSON)。仅计算相关字段（name/source 不入哈希）。"""
    snap = {k: getattr(meta, k) for k in _DEF_FIELDS}
    txt = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(txt.encode()).hexdigest(), txt


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in (ARCHIVE_DDL, INSPECTION_DDL, REFS_DDL):
        con.execute(ddl)


# ── 8.1 激活门禁（无旁路） ──
def activate(con: duckdb.DuckDBPyConnection, meta: FactorMeta, gov_report: dict,
             root: str | Path | None = None, actor: str = "system",
             git_sha: str | None = None) -> dict:
    """draft → active 门禁：③④⑤⑥ 全达标 + hypothesis_id 强制 + 定义快照冻结。

    任一检查不过即 LifecycleError（无旁路）；全过 → set_status + meta_factor_archive 快照。
    """
    if not meta.source:
        raise LifecycleError(f"{meta.id}: 激活仅接受注册中心加载的因子（source 为空 = 内存手搓）")
    if meta.status != STATUS_DRAFT:
        raise LifecycleError(f"{meta.id}: 门禁仅对 draft 开放（实得 {meta.status}）")
    if not re.fullmatch(r"\d+\.\d+\.\d+", meta.version):
        raise LifecycleError(f"{meta.id}: version 须语义化三段（实得 {meta.version}）")
    if not (meta.hypothesis_id or "").strip():
        raise LifecycleError(f"{meta.id}: hypothesis_id 缺失（R3：无来源不激活）")
    hyp = None
    if root is not None:
        hyp = load_hypotheses(root)
        if meta.hypothesis_id not in hyp:
            raise LifecycleError(
                f"{meta.id}: hypothesis_id {meta.hypothesis_id} 不在假设注册表")
        if hyp[meta.hypothesis_id]["status"] == "refuted":
            raise LifecycleError(f"{meta.id}: 假设 {meta.hypothesis_id} 已被证伪，禁止激活")
    # ④⑤⑥：M4 治理报告凭据
    if not isinstance(gov_report, dict) or "verdicts" not in gov_report:
        raise LifecycleError(f"{meta.id}: 缺 M4 治理报告凭据（④⑤⑥ 无旁路）")
    v = next((x for x in gov_report["verdicts"] if x["factor_id"] == meta.id), None)
    if v is None:
        raise LifecycleError(f"{meta.id} 不在治理报告 verdicts 内")
    if v["overall"] != "pass":
        raise LifecycleError(f"{meta.id}: 治理裁决 {v['overall']} ≠ pass（不可激活）")
    for gate in ("time_gate", "param_gate", "regime_gate", "incremental"):
        if not v[gate]["pass"]:
            raise LifecycleError(f"{meta.id}: {gate} 未过（显式复核）")
    if v["pit"]["violations"] != 0:
        raise LifecycleError(f"{meta.id}: PIT 违例 {v['pit']['violations']} > 0（红线）")
    # ③：M3 检验凭据（meta_factor_eval 1M 行，对齐 IC 须为正）
    missing = "1M horizon 无检验记录"
    try:
        row = con.execute(
            "SELECT ic_mean FROM meta_factor_eval"
            " WHERE factor_id = ? AND horizon_months = 1"
            " ORDER BY as_of DESC LIMIT 1", [meta.id]).fetchone()
    except duckdb.CatalogException:
        row, missing = None, "meta_factor_eval 表不存在"
    if row is None:
        raise LifecycleError(f"{meta.id}: ③ 检验记录缺失（{missing}）——先跑 run_eval")
    # ③ 口径：eval 存原始 IC（未乘 direction），门禁按 direction 对齐判定
    # （如 val_ps_ttm direction=-1、原始 IC −0.206 → 对齐 +0.206 → 通过）
    aligned = None if row[0] is None else row[0] * (meta.direction or 1)
    if aligned is None or aligned <= 0:
        raise LifecycleError(
            f"{meta.id}: ③ 对齐 IC {aligned} 非正（门禁要求 > 0；"
            f"原始 {row[0]} × direction {meta.direction}）")
    # 全过：状态迁移 + 版本快照
    set_status(meta, STATUS_ACTIVE)
    ensure_tables(con)
    h, snap = def_hash(meta)
    con.execute("INSERT INTO meta_factor_archive VALUES (?,?,?,?,?,?,?)",
                [meta.id, meta.version, h, snap, STATUS_ACTIVE,
                 date.today().isoformat(), git_sha or _git_sha()])
    return {"factor_id": meta.id, "version": meta.version, "def_hash": h,
            "hypothesis_id": meta.hypothesis_id, "status": meta.status,
            "actor": actor, "activated_at": date.today().isoformat()}


def verify_frozen(con: duckdb.DuckDBPyConnection, meta: FactorMeta) -> dict:
    """版本冻结校验：当前定义 hash 须与该 version 激活快照一致（漂移未 bump version = 拒绝）。

    保证「引用旧 version 的回测报告重跑 bit 级一致」：凡通过校验的重跑，定义体与激活时逐位相同。
    """
    ensure_tables(con)
    row = con.execute("SELECT def_hash FROM meta_factor_archive"
                      " WHERE factor_id = ? AND version = ?"
                      " ORDER BY activated_at DESC LIMIT 1",
                      [meta.id, meta.version]).fetchone()
    if row is None:
        raise LifecycleError(f"{meta.id} v{meta.version}: 无激活快照（版本冻结凭据缺失）")
    h, _ = def_hash(meta)
    if h != row[0]:
        raise LifecycleError(
            f"{meta.id} v{meta.version}: 定义漂移（激活 {row[0][:12]}… vs 当前 {h[:12]}…）"
            "——口径变更须 bump 新 version 并重新走门禁，旧版本冻结")
    return {"factor_id": meta.id, "version": meta.version, "def_hash": h, "frozen": True}


# ── 被引用关系与降级影响面（8.3/8.4） ──
def register_ref(con: duckdb.DuckDBPyConnection, strategy_id: str, factor_id: str,
                 version: str) -> None:
    """策略引用登记（M6 策略落地的写入口；降级影响面评估的数据源）。"""
    ensure_tables(con)
    con.execute("INSERT INTO strategy_factor_ref VALUES (?,?,?,?)",
                [strategy_id, factor_id, version, date.today().isoformat()])


def who_references(con: duckdb.DuckDBPyConnection, factor_id: str) -> list[dict]:
    """被引用关系查询：{strategy_id, version} 列表（降级前强制影响面评估）。"""
    try:
        rows = con.execute("SELECT strategy_id, version FROM strategy_factor_ref"
                           " WHERE factor_id = ? ORDER BY strategy_id", [factor_id]).fetchall()
    except duckdb.CatalogException:
        return []
    return [{"strategy_id": s, "version": v} for s, v in rows]


def deactivate(con: duckdb.DuckDBPyConnection, meta: FactorMeta,
               confirm_impact: bool = False) -> dict:
    """active → deprecated 降级（8.4）：有策略引用时须显式 confirm_impact（强制影响面评估）。

    降级动作同步入 meta_factor_archive（状态账本：archive 最新行 = 因子当前权威状态）。
    """
    if meta.status != STATUS_ACTIVE:
        raise LifecycleError(f"{meta.id}: 降级仅对 active 开放（实得 {meta.status}）")
    refs = who_references(con, meta.id)
    if refs and not confirm_impact:
        raise LifecycleError(
            f"{meta.id}: 被 {len(refs)} 个策略引用 {refs}——先完成影响面评估再 confirm_impact=True")
    set_status(meta, STATUS_DEPRECATED)
    ensure_tables(con)
    h, snap = def_hash(meta)
    con.execute("INSERT INTO meta_factor_archive VALUES (?,?,?,?,?,?,?)",
                [meta.id, meta.version, h, snap, STATUS_DEPRECATED,
                 date.today().isoformat(), _git_sha()])
    return {"factor_id": meta.id, "status": meta.status, "impacted_strategies": refs,
            "removed_from_pool": True,
            "notice": f"{meta.id} 已摘除出合成池（M6 合成模块消费此登记）"}


def sync_status(con: duckdb.DuckDBPyConnection, metas: dict[str, FactorMeta]) -> None:
    """YAML 是定义，状态库是状态：加载后从 archive 账本 sync 权威 status（draft 无记录 = 保持）。"""
    ensure_tables(con)
    for fid, meta in metas.items():
        row = con.execute("SELECT status FROM meta_factor_archive"
                          " WHERE factor_id = ? ORDER BY activated_at DESC LIMIT 1",
                          [fid]).fetchone()
        if row:
            meta.status = row[0]


# ── 8.2 季度巡检 DAG ──
def _cover_series(con: duckdb.DuckDBPyConnection, factor_id: str, start: str, end: str,
                  total: int) -> dict[str, float]:
    """逐月覆盖率 {月末日: 有值标的数 / 库内标的全集}（分母 = M2 诚实口径）。"""
    xs = cross_sections(con, factor_id, start, end)
    return {dt: len(secvals) / total for dt, secvals in xs.items() if secvals}


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _rolling_decay(ser: list[dict]) -> dict:
    """滚动 24 月 IC 衰减：recent = 最近 24 月，prior = 之前 ≤24 月；decay = 1 − recent/prior。"""
    ics = [r for r in ser if r["ic"] is not None]
    recent = ics[-INSPECT_ROLLING_MONTHS:]
    prior = ics[-2 * INSPECT_ROLLING_MONTHS:-INSPECT_ROLLING_MONTHS]
    m_r, m_p = _mean([r["ic"] for r in recent]), _mean([r["ic"] for r in prior])
    out = {"n_recent": len(recent), "n_prior": len(prior),
           "recent_mean": m_r, "prior_mean": m_p, "decay": None,
           "note": None if len(recent) >= INSPECT_MIN_RECENT
           else f"recent 有效月 {len(recent)} < {INSPECT_MIN_RECENT}（衰减降级标注）"}
    if m_r is not None and m_p is not None and m_p > 0:
        out["decay"] = 1 - m_r / m_p
    return out


def _cover_drift(cover: dict[str, float]) -> dict:
    """覆盖度漂移：最近 6 月均值 vs 基期 6 月均值；下降 ≥ 20pp → weakening。"""
    dts = sorted(cover)
    if len(dts) < 12:
        return {"drift": None, "note": f"覆盖序列 {len(dts)} 月 < 12（漂移降级标注）"}
    base = _mean([cover[d] for d in dts[:6]])
    cur = _mean([cover[d] for d in dts[-6:]])
    return {"base": base, "current": cur,
            "drift": (cur - base) if (base is not None and cur is not None) else None,
            "note": None}


def _deterministic_sample(ids: list[str], rate: float) -> list[str]:
    """确定性抽查样本：按 id 的 sha256 排序取前 rate 比例（可复现，不用随机数）。"""
    if not ids:
        return []
    n = max(1, round(len(ids) * rate))
    return sorted(ids, key=lambda x: hashlib.sha256(x.encode()).hexdigest())[:n]


def quarterly_inspection(con: duckdb.DuckDBPyConnection, metas: dict[str, FactorMeta],
                         root: str | Path | None = None, as_of: str | None = None,
                         benchmark: str | None = DEFAULT_BENCHMARK,
                         out_dir: str | Path | None = None) -> dict:
    """季度巡检 DAG（8.2/8.4）：active 因子逐个三探针 → verdict + 假设复查步 + 降级动作。

    三探针：滚动 24 月 IC 衰减（≥50% → weakening；recent ≤ 0 → invalid）、
    覆盖度漂移（≥20pp → weakening）、失效环境（全环境失效 → invalid）。
    巡检入库 meta_factor_inspection；invalid 自动 deprecated（8.4 自动摘除）。
    """
    ensure_tables(con)
    hyp = load_hypotheses(root) if root else {}
    as_of = as_of or con.execute("SELECT max(CAST(dt AS VARCHAR))"
                                 " FROM dwd_daily_bar").fetchone()[0]
    # 窗口 = as_of 往前全部（巡检以 as_of 为右端）
    start = con.execute("SELECT min(CAST(dt AS VARCHAR)) FROM dwd_daily_bar"
                        " WHERE dt <= CAST(? AS DATE)", [as_of]).fetchone()[0]
    end = as_of
    total = con.execute("SELECT count(DISTINCT secid) FROM dwd_daily_bar"
                        " WHERE dt <= CAST(? AS DATE)", [as_of]).fetchone()[0]
    rows, skipped = [], []
    healthy_ids = []
    for fid in sorted(metas):
        meta = metas[fid]
        if meta.status != STATUS_ACTIVE:
            skipped.append({"factor_id": fid, "status": meta.status})
            continue
        ser = ic_series(con, meta, start, end, horizon=1)
        rd = _rolling_decay(ser)
        cov = _cover_series(con, fid, start, end, total)
        cd = _cover_drift(cov)
        rg = regime_gate(con, meta, start, end, benchmark=benchmark, lockbox_months=0)
        # 裁决（invalid 优先）
        invalid, weakening = [], []
        if rd["recent_mean"] is not None and rd["recent_mean"] <= 0:
            invalid.append(f"近 {rd['n_recent']} 月对齐 IC {rd['recent_mean']:.3f} ≤ 0")
        if not rg["pass"] and "全部环境失效" in rg["reason"]:
            invalid.append("全环境失效（牛熊 IC 均 ≤ 0）")
        if rd["decay"] is not None and rd["decay"] >= INSPECT_DECAY_MAX:
            weakening.append(f"滚动衰减 {rd['decay']:.0%} ≥ {INSPECT_DECAY_MAX:.0%}")
        if cd["drift"] is not None and cd["drift"] <= -INSPECT_COVER_DROP:
            weakening.append(f"覆盖率漂移 {cd['drift']:+.0%} ≤ −{INSPECT_COVER_DROP:.0%}")
        verdict = ("invalid" if invalid else "weakening" if weakening else "healthy")
        reason = "；".join(invalid + weakening) or "三探针均通过"
        # 假设复查步（R3）
        actions = []
        if verdict == "healthy":
            healthy_ids.append(fid)
        else:
            actions.append({"type": "hypothesis_review", "priority":
                            "high" if verdict == "invalid" else "normal",
                            "hypothesis_id": meta.hypothesis_id,
                            "statement": (hyp.get(meta.hypothesis_id, {})
                                          .get("statement")) if hyp else None,
                            "question": f"{fid} 判定 {verdict}：假设仍成立吗？"})
        # 8.4 降级动作
        refs = who_references(con, fid)
        if verdict == "invalid" and meta.status == STATUS_ACTIVE:
            d = deactivate(con, meta, confirm_impact=True)   # 自动摘除：影响面随动作通知
            actions.append({"type": "removed_from_pool",
                            "impacted_strategies": d["impacted_strategies"],
                            "notice": d["notice"]})
        elif verdict == "weakening":
            actions.append({"type": "warning",
                            "notice": f"{fid} 标警 weakening——策略层提示：降权观察"})
        row = {"factor_id": fid, "version": meta.version, "as_of": end,
               "verdict": verdict, "reason": reason,
               "metrics": {"rolling": rd, "cover": cd,
                           "regime": {"pass": rg["pass"], "reason": rg["reason"],
                                      "axes": {k: val for k, val in rg["axes"].items()
                                               if isinstance(val, dict)
                                               and "mean_ic" in str(val)}}},
               "actions": actions, "refs": refs, "status_after": meta.status}
        rows.append(row)
        con.execute("INSERT INTO meta_factor_inspection VALUES (?,?,?,?,?,?,?,?)",
                    [fid, meta.version, end, verdict, reason,
                     json.dumps(row["metrics"], ensure_ascii=False),
                     json.dumps(actions, ensure_ascii=False),
                     date.today().isoformat()])
    # healthy 20% 确定性抽查（复查步的另一半）
    spot = _deterministic_sample(healthy_ids, SPOT_CHECK_RATE)
    for fid in spot:
        meta = metas[fid]
        action = {"type": "spot_check", "hypothesis_id": meta.hypothesis_id,
                  "statement": (hyp.get(meta.hypothesis_id, {}).get("statement"))
                  if hyp else None,
                  "question": f"{fid} 抽查：假设证据链复核（{SPOT_CHECK_RATE:.0%} 例行）"}
        for r in rows:
            if r["factor_id"] == fid:
                r["actions"].append(action)
        con.execute("UPDATE meta_factor_inspection SET actions_json = ?"
                    " WHERE factor_id = ? AND as_of = ?",
                    [json.dumps([a for r2 in rows if r2["factor_id"] == fid
                                 for a in r2["actions"]], ensure_ascii=False),
                     fid, end])
    report = {"as_of": end, "window": {"start": start, "end": end},
              "verdicts": rows, "skipped": skipped, "spot_check": spot,
              "summary": {k: sum(1 for r in rows if r["verdict"] == k)
                          for k in ("healthy", "weakening", "invalid")}}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"m5_inspection_{end}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / f"m5_inspection_{end}.md").write_text(render_md(report), encoding="utf-8")
    return report


def factor_health(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """各因子最新巡检健康度（8.4 weakening 标警的策略层查询口）。"""
    try:
        rows = con.execute(
            "SELECT factor_id, verdict FROM meta_factor_inspection"
            " WHERE (factor_id, as_of) IN (SELECT factor_id, max(as_of)"
            "       FROM meta_factor_inspection GROUP BY factor_id)").fetchall()
    except duckdb.CatalogException:
        return {}
    return dict(rows)


# ── 报告 ──
def render_md(rep: dict) -> str:
    """巡检报告 → Markdown（巡检看板 + 逐因子 + 待答项）。"""
    s = rep["summary"]
    lines = [f"# M5 季度巡检报告（as_of={rep['as_of']}）", "",
             f"- 窗口：{rep['window']['start']}..{rep['window']['end']} · "
             f"healthy {s['healthy']} / weakening {s['weakening']} / invalid {s['invalid']}"
             f"（跳过 {len(rep['skipped'])} 个非 active）",
             "", "## 巡检看板", "",
             "| 因子 | 滚动衰减 | 近期 IC | 覆盖漂移 | 环境 | 裁决 |", "|---|---|---|---|---|---|"]
    for r in rep["verdicts"]:
        rd, cd = r["metrics"]["rolling"], r["metrics"]["cover"]
        decay = ("—" if rd["decay"] is None else f"{rd['decay']:+.0%}"
                 + ("（改善）" if rd["decay"] < 0 else ""))
        recent = "—" if rd["recent_mean"] is None else f"{rd['recent_mean']:+.3f}"
        drift = "—" if cd["drift"] is None else f"{cd['drift']:+.0%}"
        rg = r["metrics"]["regime"]
        lines.append(f"| {r['factor_id']} | {decay} | {recent} | {drift} | "
                     f"{'✅' if rg['pass'] else '❌'} | **{r['verdict']}** |")
    lines += ["", "## 待答项与动作", ""]
    for r in rep["verdicts"]:
        for a in r["actions"]:
            if a["type"] == "hypothesis_review":
                lines.append(f"- 🔍 **[{a['priority']}] {r['factor_id']}** {a['question']}"
                             f"（假设 {a['hypothesis_id']}）")
            elif a["type"] == "removed_from_pool":
                lines.append(f"- ⛔ {r['factor_id']} {a['notice']} · "
                             f"影响面：{a['impacted_strategies'] or '无引用'}")
            elif a["type"] == "warning":
                lines.append(f"- ⚠️ {r['factor_id']} {a['notice']}")
            elif a["type"] == "spot_check":
                lines.append(f"- 🎯 抽查 {r['factor_id']}：{a['question']}")
    return "\n".join(lines) + "\n"


# ── CLI（8.1：激活门禁强制走 CLI 通道，无旁路） ──
_REPO_DEFAULT = "alphapocket/remote/repo"
_DB_DEFAULT = "alphapocket/sandbox/factorlib.duckdb"   # 状态库：门禁凭据/巡检历史/引用（跨进程持久）


def _load_metas() -> dict[str, FactorMeta]:
    root = Path(__file__).resolve().parent
    return {m.id: m for m in load_definitions(root)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factorlib.lifecycle",
                                 description="M5 注册生命周期：activate/inspect/freeze-verify/refs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("activate", help="draft → active 门禁（③④⑤⑥ 全达标 + hypothesis_id）")
    p.add_argument("--factor", required=True)
    p.add_argument("--gov-report", required=True, help="M4 治理报告 JSON 路径")
    p.add_argument("--repo", default=_REPO_DEFAULT)
    p.add_argument("--db", default=_DB_DEFAULT, help="状态库（duckdb 文件，跨进程持久）")
    p = sub.add_parser("inspect", help="季度巡检 DAG")
    p.add_argument("--repo", default=_REPO_DEFAULT)
    p.add_argument("--as-of", default=None)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--db", default=_DB_DEFAULT)
    p = sub.add_parser("freeze-verify", help="版本冻结校验（重跑前必过）")
    p.add_argument("--factor", required=True)
    p.add_argument("--repo", default=_REPO_DEFAULT)
    p.add_argument("--db", default=_DB_DEFAULT)
    p = sub.add_parser("refs", help="被引用关系查询")
    p.add_argument("--factor", required=True)
    p.add_argument("--repo", default=_REPO_DEFAULT)
    p.add_argument("--db", default=_DB_DEFAULT)
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent
    metas = _load_metas()
    if args.cmd == "activate":
        # ③ 凭据现场生成：单因子物化 → 检验 → 入库，再走门禁（状态落 --db 文件库）
        from .evaluate import evaluate_factor, save_eval
        from .materialize import materialize
        con = build_universe_db(args.repo, path=args.db)
        sync_status(con, metas)               # 权威状态 = archive 账本（防重复激活）
        meta = metas[args.factor]
        materialize(con, meta, mode="full")
        save_eval(con, evaluate_factor(con, meta,
                                       *con.execute("SELECT min(CAST(dt AS VARCHAR)),"
                                                    " max(CAST(dt AS VARCHAR)) FROM dwd_daily_bar")
                                       .fetchone()))
        gov = json.loads(Path(args.gov_report).read_text(encoding="utf-8"))
        out = activate(con, meta, gov, root=root)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    con = build_universe_db(args.repo, path=args.db)
    if args.cmd == "inspect":
        sync_status(con, metas)               # 巡检对象 = 状态库权威 active 集
        rep = quarterly_inspection(con, metas, root=root, as_of=args.as_of,
                                   out_dir=args.out_dir)
        print(json.dumps(rep["summary"], ensure_ascii=False))
        return 0
    if args.cmd == "freeze-verify":
        print(json.dumps(verify_frozen(con, metas[args.factor]), ensure_ascii=False))
        return 0
    if args.cmd == "refs":
        print(json.dumps(who_references(con, args.factor), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
