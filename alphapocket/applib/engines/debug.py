"""指标调试闭环（T-R8）：metric test（单指标验证/参数扫描）与 diff（版本前后对比）。

编排层纪律（红线）：
- **只读引擎、只写 artifacts**——绝不自动修改注册表（verdict 仅提示，人决策红线）；
- 零新公式：walkforward 复用 ``walkforward.run_validation``（合成 v1 式 meta 走
  L127-131 既有分派协议：type=alpha→compute_alpha / type=signal→compute_beta）；
  sensitivity 复用 ``sensitivity.run_strategy_bt/_cell/_oat``（薄封装，不改其公共 API）；
  diff 复用三引擎包装器的 registry 配置链路（before 快照替换 metric 条目后重跑引擎，
  同输入数据 → 输出差异即该变更的行为影响）；
- 产物落 ``sandbox/artifacts/debug_test_* / debug_diff_*``（JSON，可 json.load）。
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import time
from pathlib import Path

from .. import sensitivity as SEN
from .. import walkforward as WF
from ..ctx import Ctx
from .alpha import AlphaEngine
from .base import load_registry
from .beta import BetaEngine
from .macro import MacroEngine

# verdict 阈值（初版；hit_rate 分位）：≥0.55 keep；0.45~0.55 watch；<0.45 drop。
# 仅提示不改注册表（人决策红线）。
_VERDICT_BANDS = ((0.55, "keep"), (0.45, "watch"))


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _locate(reg: dict, metric_id: str) -> tuple[str | None, dict | None]:
    """跨引擎定位指标（orphan 条目不入调试面——公式已移除）。"""
    for eid, sec in reg["engines"].items():
        for m in sec.get("metrics", []):
            if isinstance(m, dict) and m.get("id") == metric_id and not m.get("orphan"):
                return eid, m
    return None, None


def _verdict(hit_rate: float | None) -> str:
    if hit_rate is None:
        return "watch（样本不足：无有效 IC 折）"
    for th, label in _VERDICT_BANDS:
        if hit_rate >= th:
            return label
    return "drop"


def parse_grid(grid: str | None) -> dict:
    """"win=20,25,30" → {"win": [20, 25, 30]}（值按 JSON 解析，失败回退字符串）。"""
    if not grid or "=" not in grid:
        raise ValueError("--grid 需要 参数名=v1,v2,… 形式（如 win=20,25,30）")
    name, vals = grid.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError("--grid 参数名不能为空")
    out = []
    for v in vals.split(","):
        v = v.strip()
        try:
            out.append(json.loads(v))
        except ValueError:
            out.append(v)
    return {name: out}


# ── metric test ──────────────────────────────────────────────


def _bars_fund(ctx: Ctx) -> tuple[dict, dict]:
    bp = ctx.local_repo / "market" / "daily_bars.json"
    if not bp.exists():
        raise FileNotFoundError("行情数据未同步（先 `ap fetch`）")
    fp = ctx.local_repo / "factors" / "fundamental_values.json"
    return _load_json(bp), (_load_json(fp) if fp.exists() else {})


def _wf_metric(ctx: Ctx, mid: str, eid: str, entry: dict,
               overrides: dict | None, start=None, end=None) -> dict:
    """walkforward 模式：合成 v1 式 meta 走既有分派协议 → 单因子滚动验证。"""
    if eid == "macro":
        return {"error": "宏观指标无横截面回放，请用 sensitivity 模式或 diff 对比"}
    try:
        bars, fund = _bars_fund(ctx)
    except FileNotFoundError as e:
        return {"error": str(e)}
    meta = {"id": mid, "type": "signal" if eid == "beta" else "alpha",
            "name": entry.get("note") or mid, "version": entry.get("version", 1)}
    # overrides 中命中滚动协议键的部分 → cfg（horizon_days/train_min_months/…；其余无注入面）
    cfg = {k: v for k, v in (overrides or {}).items()
           if k in WF.DEFAULT_CFG or k == "horizon_days"}
    rep = WF.run_validation(mid, bars, fund, {"factors": [meta]}, cfg or None,
                            start=start, end=end)
    if "error" in rep:
        return rep
    fold_ics = [f["ic_mean"] for f in rep["summary"]["fold_ic"] if f["ic_mean"] is not None]
    hit = (sum(1 for x in fold_ics if x > 0) / len(fold_ics)) if fold_ics else None
    return {"payload": {"n_folds": rep["n_folds"], "fold_ic": rep["summary"]["fold_ic"],
                        "lockbox": rep["lockbox"], "data_range": rep["data_range"],
                        "horizon_days": rep["horizon_days"], "wf_verdict": rep["verdict"]},
            "summary": {"n_folds": rep["n_folds"], "mean_ic": rep["summary"]["avg_ic"],
                        "hit_rate": hit, "verdict": _verdict(hit)}}


def _sens_metric(ctx: Ctx, mid: str, eid: str, entry: dict, grid: str | None,
                 overrides: dict | None, portfolio: str = "cn4",
                 start=None, end=None) -> dict:
    """sensitivity 模式：--grid 单参数 OAT → 该指标的扫描曲线（复用 SEN 基建）。

    - beta：参数 ∈ 引擎B 策略层注入面（win/band/band_sigma）→ 逐值 run_strategy_bt
      （引擎B 参数驱动信号/止损层）；bias_thresh 等信号级参数无策略层注入面 → 诚实报错；
    - macro：仅 ``arg``（打分器尺度）→ 逐值经 overrides scorers 注入重跑市场分；
    - alpha：数值参数为代码内建（weights/anchors 为引擎级）→ 诚实报错，不硬凑。
    """
    try:
        g = parse_grid(grid)
    except ValueError as e:
        return {"error": str(e)}
    if len(g) != 1:
        return {"error": "单参数 OAT：--grid 一次只扫一个参数（多参数请用 ap sensitivity）"}
    param, values = next(iter(g.items()))

    if eid == "beta":
        if param not in ("win", "band", "band_sigma"):
            return {"error": f"beta 策略层可扫参数：['win', 'band', 'band_sigma']（实际 {param!r}；"
                             f"其余为信号级登记参数，请用 diff 对比或 walkforward 验证）"}
        try:
            bars, fund = _bars_fund(ctx)
        except FileNotFoundError as e:
            return {"error": str(e)}
        pf_p = ctx.local_repo / "portfolio" / f"{portfolio}.json"
        if not pf_p.exists():
            return {"error": f"组合不存在：{portfolio}（{pf_p}）"}
        pf = _load_json(pf_p)
        extra = {k: v for k, v in (overrides or {}).items() if k in ("rebalance", "cost_bps",
                                                                     "weighting", "stops")}
        oat = SEN._oat(param, values, lambda v: SEN.run_strategy_bt(
            pf, bars, fund, start=start, end=end, _cache={}, **extra, **{param: v}),
            SEN.R1_DEFAULTS[param])
        return {"payload": {"param": param, "values": values, "rows": oat["rows"],
                            "portfolio": portfolio},
                "summary": {"param": param, "baseline": oat["baseline"],
                            "n_values": len(oat["rows"]), "spread": oat["spread"],
                            "verdict": oat["verdict"]}}

    if eid == "macro":
        if param != "arg":
            return {"error": "宏观指标仅支持 arg（打分器尺度）扫描；启停/归属影响请用 diff 对比"}
        sc = entry.get("scorer") or {}
        if (sc.get("type") or "quant") == "quant":
            return {"error": "quant 分位法无 arg 注入面（打分由历史分位决定），请用 diff 对比"}
        mp = ctx.local_repo / "market" / "macro_liquidity.json"
        if not mp.exists():
            return {"error": "宏观流动性数据未同步（先执行数据同步）"}
        data = _load_json(mp)
        hk_p = ctx.local_repo / "market" / "hk_rates.json"
        hk = _load_json(hk_p).get("series") if hk_p.exists() else None
        eng = MacroEngine()
        eng.registry = load_registry(ctx.local_repo)
        # 其余指标沿用 registry 打分器，仅扫描目标指标替换 arg（overrides scorers 是整表语义）
        reg_scorers = dict(eng._config_from_registry().get("scorers") or {})
        mkt, dim = entry.get("market"), entry.get("dim")
        rows = []
        for v in values:
            r = eng.run(data.get("series") or {}, hk,
                        overrides={"scorers": {**reg_scorers, mid: (sc["type"], v)}})
            mk = (r.get("markets") or {}).get(mkt) or {}
            rows.append({"value": v, "market_score": mk.get("score"),
                         "dim_score": ((mk.get("dims") or {}).get(dim) or {}).get("score")})
        scores = [r["market_score"] for r in rows if r["market_score"] is not None]
        spread = ((max(scores) - min(scores)) / max(abs(max(scores, key=abs)), 1e-9)
                  if len(scores) >= 2 else None)
        verdict = ("平坦·稳健（档间极差 < 5%）" if spread is not None and spread < 0.05 else
                   "存在敏感档位（档间极差 ≥ 5%）→ 结合 diff 评审" if spread is not None else
                   "n/a（市场分不可用）")
        return {"payload": {"param": param, "values": values, "rows": rows,
                            "market": mkt, "dim": dim, "scorer_type": sc.get("type")},
                "summary": {"param": param, "baseline": sc.get("arg"),
                            "n_values": len(rows), "spread": spread, "verdict": verdict}}

    return {"error": "alpha 指标无数值参数注入面（weights/anchors 为引擎级）；"
                     "请用 walkforward 模式验证 IC，或用 diff 对比启停影响"}


def metric_test(ctx: Ctx, metric_id: str, tool: str = "walkforward", grid: str | None = None,
                overrides: dict | None = None, portfolio: str = "cn4",
                start=None, end=None) -> dict:
    """单指标调试：walkforward（滚动验证）/ sensitivity（参数扫描）→ artifacts。"""
    reg = load_registry(ctx.local_repo)
    eid, entry = _locate(reg, metric_id)
    if entry is None:
        return {"error": f"指标不存在：{metric_id}（先 `ap metric list` 查看）"}
    if tool == "walkforward":
        res = _wf_metric(ctx, metric_id, eid, entry, overrides, start=start, end=end)
    elif tool == "sensitivity":
        res = _sens_metric(ctx, metric_id, eid, entry, grid, overrides,
                           portfolio=portfolio, start=start, end=end)
    else:
        return {"error": f"未知工具：{tool}（可选 walkforward/sensitivity）"}
    if "error" in res:
        return res
    out = {"metric_id": metric_id, "engine": eid, "tool": tool, "generated_at": _now(),
           "registry_version": entry.get("version", 1),
           "result": res["payload"], "summary": res["summary"],
           "note": "verdict 仅提示，不自动改注册表（人决策红线）"}
    p = ctx.artifacts_dir / f"debug_test_{metric_id}_{_ts()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    out["file"] = str(p)
    return out


# ── metric diff ──────────────────────────────────────────────


def _resolve_snapshot(cl: list[dict], mid: str, baseline: int | None,
                      changelog_idx: int | None) -> dict:
    """从 changelog 解析基线快照：version=n 取该版本的 before/after；idx=k 取第 k 条。"""
    if changelog_idx is not None:
        if not (0 <= changelog_idx < len(cl)):
            raise ValueError(f"changelog 下标越界：{changelog_idx}（共 {len(cl)} 条）")
        rec = cl[changelog_idx]
        if rec.get("id") != mid:
            raise ValueError(f"changelog[{changelog_idx}] 是指标 {rec.get('id')} 的变更，非 {mid}")
        snap = rec.get("before") or rec.get("after")
        if not snap:
            raise ValueError(f"changelog[{changelog_idx}] 无可用快照")
        return snap
    # baseline=version n：优先取「从 v_n 变出」的 before，其次「变成 v_n」的 after（取最近）
    hits = [r for r in cl if isinstance(r.get("before"), dict)
            and r["before"].get("version") == baseline]
    if hits:
        return hits[-1]["before"]
    hits = [r for r in cl if isinstance(r.get("after"), dict)
            and r["after"].get("version") == baseline]
    if hits:
        return hits[-1]["after"]
    raise ValueError(f"快照不存在：version={baseline}（changelog 被截断或为首版）；"
                     f"可先 `ap metric show {mid}` 查看当前版本")


def _config_delta(before: dict, after: dict) -> dict:
    """字段级差异（params/scorer 两级展开；version 差异单列）。"""
    out: dict = {}
    for k in sorted((set(before) | set(after)) - {"version"}):
        b, a = before.get(k), after.get(k)
        if isinstance(b, dict) and isinstance(a, dict):
            sub = {sk: {"before": b.get(sk), "after": a.get(sk)}
                   for sk in sorted(set(b) | set(a)) if b.get(sk) != a.get(sk)}
            if sub:
                out[k] = sub
        elif b != a:
            out[k] = {"before": b, "after": a}
    return out


def _engine_output(ctx: Ctx, reg: dict, eid: str, mid: str, entry: dict) -> dict:
    """以 entry 替换该指标后重跑引擎（同输入数据；复用 T-R3/4/5 配置链路）。"""
    reg2 = copy.deepcopy(reg)
    metrics = reg2["engines"][eid]["metrics"]
    i = next(i for i, m in enumerate(metrics) if m.get("id") == mid)
    metrics[i] = dict(entry)
    eng = {"alpha": AlphaEngine, "beta": BetaEngine, "macro": MacroEngine}[eid]()
    eng.registry = reg2

    if eid == "alpha":
        bars, fund = _bars_fund(ctx)
        by = {}
        for s, arr in bars.items():
            if not arr:
                continue
            fd = fund.get(s, {})
            by[s] = eng.run(s, arr, fd.get("reports", []), fd.get("dividends", [])).get("composite")
        vals = [v for v in by.values() if v is not None]
        return {"composite_mean": (sum(vals) / len(vals) if vals else None),
                "n_secids": len(vals), "by_secid": by}

    if eid == "beta":
        bars, _ = _bars_fund(ctx)
        sig_n, vals = 0, []
        for s, arr in bars.items():
            if not arr:
                continue
            r = eng.run(s, arr)
            sig_n += len(r.get("signals") or [])
            if r.get(mid) is not None:
                vals.append(r[mid])
        return {"signal_count": sig_n,
                "metric_value_mean": (sum(vals) / len(vals) if vals else None),
                "n_secids": sum(1 for arr in bars.values() if arr)}

    mp = ctx.local_repo / "market" / "macro_liquidity.json"
    if not mp.exists():
        raise FileNotFoundError("宏观流动性数据未同步（先执行数据同步）")
    data = _load_json(mp)
    hk_p = ctx.local_repo / "market" / "hk_rates.json"
    hk = _load_json(hk_p).get("series") if hk_p.exists() else None
    r = eng.run(data.get("series") or {}, hk)
    if r.get("status") == "unavailable":
        return {"market_score": None, "dim_score": None, "global_score": None}
    mkt = entry.get("market")
    mk = (r.get("markets") or {}).get(mkt) or {}
    return {"market_score": mk.get("score"),
            "dim_score": ((mk.get("dims") or {}).get(entry.get("dim")) or {}).get("score"),
            "global_score": (r.get("global") or {}).get("score")}


def metric_diff(ctx: Ctx, metric_id: str, baseline: int | None = None,
                changelog_idx: int | None = None) -> dict:
    """注册表历史版本前后对比：before/after 两套配置各跑一次引擎 → 字段级+输出差异。"""
    reg = load_registry(ctx.local_repo)
    eid, cur = _locate(reg, metric_id)
    if cur is None:
        return {"error": f"指标不存在：{metric_id}（先 `ap metric list` 查看）"}
    if baseline is None and changelog_idx is None:
        return {"error": "需要 --baseline <version_n> 或 --changelog-idx <k>"}
    cl = [r for r in (reg.get("changelog") or []) if isinstance(r, dict)]
    try:
        snap = _resolve_snapshot(cl, metric_id, baseline, changelog_idx)
    except ValueError as e:
        return {"error": str(e)}

    try:
        out_before = _engine_output(ctx, reg, eid, metric_id, snap)
        out_after = _engine_output(ctx, reg, eid, metric_id, cur)
    except FileNotFoundError as e:
        return {"error": str(e)}

    out = {"metric_id": metric_id, "engine": eid,
           "baseline_version": snap.get("version"), "current_version": cur.get("version"),
           "config_delta": _config_delta(snap, cur),
           "output_delta": {k: {"before": out_before.get(k), "after": out_after.get(k)}
                            for k in sorted(set(out_before) | set(out_after))},
           "generated_at": _now(),
           "note": "beta 指标 params 为登记面（runtime 由引擎 defaults 驱动）——输出无差异属预期"}
    if out_before == out_after:
        out["note"] += "；本次对比输出无差异（参数未影响运行时配置）"
    ts = _ts()
    files = []
    for suffix, payload in (( "", out), ("_before", out_before), ("_after", out_after)):
        p = ctx.artifacts_dir / f"debug_diff_{metric_id}_{ts}{suffix}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(str(p))
    out["files"] = files
    return out
