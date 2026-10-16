"""M6 T-C4：策略层 walk-forward（扩展窗口）+ lockbox 一次性验证 + 参数 ±30% 敏感性。

口径对齐 factor_governance.walk_forward（M4 IC 层）：扩展窗口（train 从样本起点扩展，
禁止滑动重置）+ lockbox 末端封存只验证一次。差异登记（诚实口径）：
- lockbox 按 cfg.lockbox_start 日历锚定（缺省 2022-01，计划原文「2022–至今」）；
- 真实库样本 2021-01..2026-09（69 月）下 2022-01 锚封存 57/69 ≈ 84%，滚动无折叠空间
  → CLI 冒烟以 --lockbox-start 2025-10 显式覆盖（末端封存 12 月，与 M4 n_lock=12 对齐），
  封存占比如实归档；
- train/valid 任一段触及 lockbox = 协议违规即报错（不存在「看一眼再排除」）。

红线：滚动/lockbox/敏感性回测一律走 run_factor_backtest（T-C3 撮合口径，含成本轨）；
全链确定性：同输入重跑折序列 bit 级一致（runid = 内容哈希，非时间戳）。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import duckdb

from . import backtest as BT
from .factor_backtest import run_factor_backtest

_METHOD_ORDER = ("equal", "ic_12m", "ic_ir", "lasso")

DEFAULT_CFG = {"train_min_months": 24, "valid_months": 6, "step_months": 6,
               "lockbox_start": "2022-01", "min_lockbox_months": 12,
               "sample_start": None, "sensitivity": False}


class StrategyWfError(Exception):
    """滚动协议 / lockbox 纪律 / 敏感性配置违规。"""


# ── 纯函数：折叠规划 / 自审 / 选法 ──────────────────────
def plan_folds(months: list[str], cfg: dict | None = None) -> list[dict]:
    """扩展窗口折叠规划（纯函数，确定性）：train=[0,t)（从样本起点扩展）、valid=[t,t+v)。

    t 从 train_min_months 起步、step_months 推进；窗口是否触 lockbox 由 audit_folds 判定。
    """
    c = {**DEFAULT_CFG, **(cfg or {})}
    t0, v, step = int(c["train_min_months"]), int(c["valid_months"]), int(c["step_months"])
    if t0 < 1 or v < 1 or step < 1:
        raise StrategyWfError(f"train_min/valid/step 月数须 ≥1：{(t0, v, step)}")
    folds = []
    t = t0
    while t + v <= len(months):
        folds.append({"t_index": t, "train_months": list(months[:t]),
                      "valid_months": list(months[t:t + v])})
        t += step
    return folds


def audit_folds(folds: list[dict], lockbox_start: str | None) -> dict:
    """折叠自审（walkforward.audit_records 思路）：valid 窗口无重叠/倒退 + train 单向扩展
    + train/valid 均不触 lockbox。违规 → StrategyWfError（协议违规不得静默）。"""
    prev_end: str | None = None
    for f in folds:
        vm = f["valid_months"]
        if prev_end is not None and vm[0] <= prev_end:
            raise StrategyWfError(
                f"折叠窗口重叠/倒退：本折 valid 起 {vm[0]} ≤ 前折 valid 末 {prev_end}"
                "（step_months < valid_months 会触发）")
        prev_end = vm[-1]
    tr_lens = [f["t_index"] for f in folds]
    if tr_lens != sorted(tr_lens) or len(set(tr_lens)) != len(tr_lens):
        raise StrategyWfError(f"train 窗口未单向扩展：{tr_lens}")
    if lockbox_start:
        for f in folds:
            for seg, name in ((f["train_months"], "train"), (f["valid_months"], "valid")):
                touch = [m for m in seg if m >= lockbox_start]
                if touch:
                    raise StrategyWfError(
                        f"协议违规：折 valid={f['valid_months'][0]}..{f['valid_months'][-1]} 的 "
                        f"{name} 段触及 lockbox（{touch[0]} ≥ {lockbox_start}）——"
                        "lockbox 段禁止参与选法/调参")
    return {"check": "pass", "n_folds": len(folds),
            "lockbox_start": lockbox_start,
            "note": "折叠无重叠、单向往前推进、train/valid 未触 lockbox"}


def select_method(sharpes: dict[str, float | None], order: list[str] | None = None) -> str:
    """train 期 Sharpe 最大者当选；并列 → 按枚举序取先（确定性）；None（无波动）视为最差。"""
    seq = [m for m in (order or _METHOD_ORDER) if m in sharpes] \
        + sorted(set(sharpes) - set(order or _METHOD_ORDER))
    if not seq:
        raise StrategyWfError("无候选合成法可选")
    best = seq[0]
    for m in seq[1:]:
        v, b = sharpes[m], sharpes[best]
        if v is not None and (b is None or v > b):
            best = m
    return best


# ── 月轴 / 回测封装 ─────────────────────────────────────
def _month_bounds(db_path: Path) -> dict[str, tuple[str, str]]:
    """fl_daily_bar 交易日 → {YYYY-MM: (月首交易日, 月末交易日)}（契约指定月轴来源）。"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = [str(r[0]) for r in
                con.execute("SELECT DISTINCT dt FROM fl_daily_bar ORDER BY dt").fetchall()]
    finally:
        con.close()
    if not rows:
        raise StrategyWfError("fl_daily_bar 无交易日（月轴为空）")
    bounds: dict[str, tuple[str, str]] = {}
    for d in rows:
        m = d[:7]
        if m not in bounds:
            bounds[m] = (d, d)
        else:
            bounds[m] = (bounds[m][0], d)
    return bounds


def _method_spec(spec: dict, method: str) -> dict:
    s = dict(spec)
    s["methods"] = [method]
    s["method_primary"] = method
    return s


def _run_bt(spec: dict, db_path: Path, repo: Path, start: str, end: str) -> dict:
    """统一走 T-C3 撮合口径（run_factor_backtest → run_backtest），禁止绕开撮合直算收益。"""
    return run_factor_backtest(db_path, repo, spec, start=start, end=end)


def _norm_cfg(cfg: dict | None) -> dict:
    c = {**DEFAULT_CFG, **(cfg or {})}
    for k in ("train_min_months", "valid_months", "step_months", "min_lockbox_months"):
        c[k] = int(c[k])
    if c["train_min_months"] < 1 or c["valid_months"] < 1 or c["step_months"] < 1:
        raise StrategyWfError(f"train_min/valid/step 月数须 ≥1：{c}")
    return c


def _apply_sample_start(months: list[str], c: dict) -> list[str]:
    """前端截断（mom 等价差因子需要历史预热）：月轴 ≥ sample_start。"""
    s = c.get("sample_start")
    if not s:
        return months
    kept = [m for m in months if m >= s]
    if not kept:
        raise StrategyWfError(f"sample_start={s} 晚于样本末端 {months[-1]}：无可用月份")
    return kept


def _split_lockbox(months: list[str], lockbox_start: str | None, min_seal: int) -> tuple[list, list]:
    """core（滚动可用）与 sealed（lockbox 封存段）；封存不足 min_seal 月 → 报错。"""
    if lockbox_start is None:
        return months, []
    sealed = [m for m in months if m >= lockbox_start]
    if not sealed:
        raise StrategyWfError(
            f"lockbox_start={lockbox_start} 晚于样本末端 {months[-1]}：封存段为空"
            f"（协议最少 {min_seal} 个月样本外）")
    if len(sealed) < min_seal:
        raise StrategyWfError(
            f"lockbox 封存 {len(sealed)} 月 < 协议最少 {min_seal} 月"
            f"（lockbox_start={lockbox_start}，样本末端 {months[-1]}）")
    core = [m for m in months if m < lockbox_start]
    if not core:
        raise StrategyWfError(f"lockbox_start={lockbox_start} 封存全部样本：滚动无可用区间")
    return core, sealed


def _mean(vs: list[float]) -> float | None:
    return sum(vs) / len(vs) if vs else None


def _std(vs: list[float]) -> float | None:
    if len(vs) < 2:
        return None
    m = sum(vs) / len(vs)
    return math.sqrt(sum((x - m) ** 2 for x in vs) / (len(vs) - 1))


# ── 折叠滚动（扩展窗口）─────────────────────────────────
def walk_forward_folds(spec: dict, db_path: Path, repo: Path, cfg: dict | None = None) -> dict:
    """策略层扩展窗口滚动：每折 train=[样本起点,t) 四法各跑回测（T-C3 口径）→ 按 train 期
    Sharpe 选 method*（并列取枚举序先）→ valid=[t,t+v) 固定 method* 跑回测记折绩效。

    train/valid 均不触 lockbox（audit_folds 强制）；同输入重跑 bit 级一致。
    """
    c = _norm_cfg(cfg)
    bounds = _month_bounds(db_path)
    months = _apply_sample_start(sorted(bounds), c)
    core, sealed = _split_lockbox(months, c["lockbox_start"], c["min_lockbox_months"])
    folds = plan_folds(core, c)
    if not folds:
        raise StrategyWfError(
            f"core {len(core)} 月 < train_min {c['train_min_months']} + valid {c['valid_months']}"
            f"：滚动无折叠空间（lockbox_start={c['lockbox_start']} 封存 {len(sealed)} 月）——"
            "请调低 train_min_months 或推迟 lockbox_start")
    audit = audit_folds(folds, c["lockbox_start"])
    methods = [str(m) for m in (spec.get("methods") or ["equal"])]
    order = [m for m in _METHOD_ORDER if m in methods] + \
        [m for m in methods if m not in _METHOD_ORDER]

    records = []
    for i, f in enumerate(folds):
        t = f["t_index"]
        tr_start = bounds[core[0]][0]
        tr_end = bounds[core[t - 1]][1]
        va_start = bounds[f["valid_months"][0]][0]
        va_end = bounds[f["valid_months"][-1]][1]
        sharpes: dict[str, float | None] = {}
        for m in methods:
            bt = _run_bt(_method_spec(spec, m), db_path, repo, tr_start, tr_end)
            sharpes[m] = bt["strategy_metrics"]["sharpe"]
        m_star = select_method(sharpes, order=order)
        vb = _run_bt(_method_spec(spec, m_star), db_path, repo, va_start, va_end)
        records.append({
            "fold": i + 1,
            "train_range": [f["train_months"][0], f["train_months"][-1]],
            "valid_range": [f["valid_months"][0], f["valid_months"][-1]],
            "train_months_n": t, "valid_months_n": len(f["valid_months"]),
            "method_sharpes_is": sharpes, "method_chosen": m_star,
            "is_sharpe": sharpes[m_star],
            "oos_sharpe": vb["strategy_metrics"]["sharpe"],
            "oos_return": vb["strategy_metrics"]["total_return"],
            "oos_max_dd": vb["strategy_metrics"]["max_dd"],
        })

    is_vals = [r["is_sharpe"] for r in records if r["is_sharpe"] is not None]
    oos_vals = [r["oos_sharpe"] for r in records if r["oos_sharpe"] is not None]
    is_mean, oos_mean = _mean(is_vals), _mean(oos_vals)
    sd = _std(oos_vals)
    aggregate = {
        "is_mean_sharpe": round(is_mean, 6) if is_mean is not None else None,
        "oos_mean_sharpe": round(oos_mean, 6) if oos_mean is not None else None,
        "oos_ir": round(oos_mean / sd, 6) if oos_mean is not None and sd and sd > 1e-12 else None,
        "decay": round(oos_mean / is_mean, 4) if is_mean and oos_mean is not None and abs(is_mean) > 1e-12 else None,
        "n_folds": len(records),
    }
    return {"months_n": len(months), "core_months_n": len(core),
            "sealed_months": sealed, "n_sealed": len(sealed),
            "sealed_share": round(len(sealed) / len(months), 4) if months else None,
            "folds": records, "n_folds": len(records), "aggregate": aggregate,
            "audit": audit, "cfg": {k: c[k] for k in DEFAULT_CFG}}


# ── lockbox 一次性验证（§11 #4）─────────────────────────
def _read_ledger(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def lockbox_verify(spec: dict, method: str, db_path: Path, repo: Path,
                   cfg: dict | None = None, artifact_dir: Path | None = None) -> dict:
    """lockbox 段（[lockbox_start, 样本末端]）以 method 全段跑一次（T-C3 口径）并归档。

    一次性协议：同 strategy id 二次调用 → StrategyWfError（重跑须升 strategy version，
    DSL id 后缀 _v2；重切样本同罪）。报告归档 artifacts/lockbox_<id>_<runid>/report.json，
    runid 为内容哈希（确定性）。封存占比如实登记。
    """
    c = _norm_cfg(cfg)
    if not c["lockbox_start"]:
        raise StrategyWfError("lockbox_start 未配置：无从验证")
    adir = Path(artifact_dir) if artifact_dir else Path("sandbox/artifacts")
    ledger_path = adir / "lockbox_ledger.json"
    bounds = _month_bounds(db_path)
    months = sorted(bounds)
    sealed = [m for m in months if m >= c["lockbox_start"]]
    if not sealed:
        raise StrategyWfError(
            f"lockbox_start={c['lockbox_start']} 晚于样本末端 {months[-1]}：封存段为空")
    sid = str(spec.get("id") or "strategy")
    ledger = _read_ledger(ledger_path)
    if sid in ledger:
        raise StrategyWfError(
            f"协议违规：策略 {sid} 的 lockbox 已验证过（runid={ledger[sid]['runs'][0]['runid']}，"
            f"lockbox_start={ledger[sid]['runs'][0]['lockbox_start']}）——样本外只验证一次（§11 #4）；"
            "重跑须升 strategy version（DSL id 后缀 _v2）后重录")
    lb_start = bounds[sealed[0]][0]
    lb_end = bounds[months[-1]][1]
    bt = _run_bt(_method_spec(spec, method), db_path, repo, lb_start, lb_end)
    m = bt["strategy_metrics"]
    runid = hashlib.sha1(json.dumps(
        {"id": sid, "lockbox_start": c["lockbox_start"], "sealed": sealed,
         "method": method, "n_days": bt["n_days"]}, sort_keys=True).encode()).hexdigest()[:12]
    report = {
        "strategy_id": sid, "method": method,
        "lockbox_start": c["lockbox_start"],
        "sealed_months": sealed, "n_sealed": len(sealed),
        "sealed_share": round(len(sealed) / len(months), 4),
        "window": [lb_start, lb_end], "verify_count": 1,
        "oos": {"total_return": m["total_return"], "cagr": m["cagr"],
                "sharpe": m["sharpe"], "max_dd": m["max_dd"], "n_days": bt["n_days"]},
        "cost": ({"total_fees": bt["cost"]["total_fees"], "n_orders": bt["cost"]["n"]}
                 if isinstance(bt.get("cost"), dict) and "total_fees" in bt["cost"]
                 else None),   # close 模式 cost=旧换手结构（无成本轨）→ None（如实披露）
        "no_lookahead": bt.get("no_lookahead"),
        "runid": runid,
        "note": "样本外只验证一次（§11 #4）；重跑须升 strategy version（DSL id 后缀 _v2）",
    }
    out_dir = adir / f"lockbox_{sid}_{runid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ledger[sid] = {"runs": [{"runid": runid, "lockbox_start": c["lockbox_start"],
                             "artifact": str(out_dir / "report.json")}]}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


# ── 敏感性网格（±30%，DoD#2）────────────────────────────
def rank_stable_from(finals: list[float]) -> bool:
    """排序稳定判据（单策略退化式）：扰动方向与绩效变化单调——slippage↑ → 终值单调不增。"""
    return all(finals[i] >= finals[i + 1] - 1e-12 for i in range(len(finals) - 1))


def sensitivity_grid(spec: dict, db_path: Path, repo: Path, cfg: dict | None = None) -> dict:
    """三参数 ±30%：top_n（取整）、slippage_bps（需 exec=next_day_vwap）、method 全枚举。

    窗口 = core（lockbox 不参与扰动评估）；slippage↑ 终值单调不增 → rank_stable，
    并披露终值带宽（max−min）。只扰动允许集合，禁止触碰成本表其他项。
    """
    c = _norm_cfg(cfg)
    bounds = _month_bounds(db_path)
    months = _apply_sample_start(sorted(bounds), c)
    core = [m for m in months if m < c["lockbox_start"]] if c["lockbox_start"] else months
    if not core:
        raise StrategyWfError("敏感性窗口为空（core 无月份）")
    w0, w1 = bounds[core[0]][0], bounds[core[-1]][1]
    out: dict = {"window": [w0, w1],
                 "note": "敏感性窗口=core（lockbox 不参与扰动评估）；只扰动 top_n/slippage/method",
                 "axes": {}, "rank_stable": None, "band": None}

    def metrics(bt: dict) -> dict:
        return {"total_return": bt["strategy_metrics"]["total_return"],
                "sharpe": bt["strategy_metrics"]["sharpe"]}

    n0 = int(spec.get("top_n") or 1)
    axis = {}
    for n in sorted({max(1, math.floor(n0 * 0.7)), n0, max(1, math.ceil(n0 * 1.3))}):
        axis[str(n)] = metrics(_run_bt({**spec, "top_n": n}, db_path, repo, w0, w1))
    out["axes"]["top_n"] = {"base": n0, "values": axis}

    if str(spec.get("exec") or "close") == "next_day_vwap":
        s0 = float((spec.get("cost_table") or {}).get(
            "slippage_bps", BT.DEFAULT_COST_TABLE["slippage_bps"]))
        finals, axis = [], {}
        for s in sorted({round(s0 * 0.7, 4), s0, round(s0 * 1.3, 4)}):
            bt = _run_bt({**spec, "cost_table": {"slippage_bps": s}},
                         db_path, repo, w0, w1)
            axis[str(s)] = metrics(bt)
            finals.append(bt["strategy_metrics"]["total_return"])
        out["axes"]["slippage_bps"] = {"base": s0, "values": axis}
        out["rank_stable"] = rank_stable_from(finals)
        out["band"] = round(max(finals) - min(finals), 6)
    else:
        out["axes"]["slippage_bps"] = {
            "skipped": "exec != next_day_vwap：滑点轴需 T-C3 撮合口径（close 模式无滑点参数）"}

    methods = [str(m) for m in (spec.get("methods") or ["equal"])]
    axis = {}
    for m in [x for x in _METHOD_ORDER if x in methods] + \
            [x for x in methods if x not in _METHOD_ORDER]:
        axis[m] = metrics(_run_bt(_method_spec(spec, m), db_path, repo, w0, w1))
    out["axes"]["method"] = {"values": axis}
    return out


# ── 主流程：滚动 + 最终选法 + lockbox 一次性 + 敏感性 ────
def strategy_walk_forward(spec: dict, db_path: Path, repo: Path, cfg: dict | None = None,
                          artifact_dir: Path | None = None) -> dict:
    """滚动验证 → 全 core 最终选法 → lockbox 一次性验证（归档）→（可选）敏感性网格。"""
    c = _norm_cfg(cfg)
    wf = walk_forward_folds(spec, db_path, repo, c)
    bounds = _month_bounds(db_path)
    months = _apply_sample_start(sorted(bounds), c)
    core = [m for m in months if m < c["lockbox_start"]] if c["lockbox_start"] else months
    methods = [str(m) for m in (spec.get("methods") or ["equal"])]
    order = [m for m in _METHOD_ORDER if m in methods] + \
        [m for m in methods if m not in _METHOD_ORDER]
    final_sharpes = {
        m: _run_bt(_method_spec(spec, m), db_path, repo,
                   bounds[core[0]][0], bounds[core[-1]][1])["strategy_metrics"]["sharpe"]
        for m in methods}
    final_method = select_method(final_sharpes, order=order)
    lb = lockbox_verify(spec, final_method, db_path, repo, c, artifact_dir=artifact_dir)
    sens = sensitivity_grid(spec, db_path, repo, c) if c["sensitivity"] else None
    return {**wf, "final_method": final_method, "final_method_sharpes": final_sharpes,
            "lockbox": lb, "sensitivity": sens}
