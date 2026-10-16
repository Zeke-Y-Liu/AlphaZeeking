"""M6 T-C1：Strategy DSL（计划 §9.4）——策略 YAML 加载/校验/归一 + universe 过滤 + 池准入。

职责边界：
- 本模块只做「加载与校验」（查询态），不执行回测（T-C3/C4 边界）；
- weight 的 auto_* 枚举只校验不解析（T-C2 composite 消费）；
- 因子准入：factors 必须全落在 active_pool（M5 契约：active ∧ health≠invalid）——
  excluded/draft/deprecated/未注册一律拒跑并报原因（宁缺毋滥 §11 #6）；
- universe 过滤数据源缺失 → degraded 如实披露，禁止静默全通过。

依赖方向：applib → factorlib 单向（读 active_pool，与 factor_backtest 同口径）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from . import backtest as BT

_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")          # kebab-case
_METHODS = ("equal", "ic_12m", "ic_ir", "lasso")
_EXEC_CHOICES = ("next_day_vwap", "close")
_WEIGHT_AUTOS = ("auto_ic_12m", "auto_ic_ir", "auto_lasso")
_MAX_FACTORS = 8
_MARKETS = ("CN", "HK", "US")                      # T-F9 多市场扩展位（计划 10.3）


class StrategyError(Exception):
    """策略 DSL 非法 / 因子不在合成池 / universe 过滤配置非法。"""


def parse_weight_kind(w) -> str:
    """weight 判别：数值/equal → "fixed"；auto_* → 原样（T-C2 分派依据）。"""
    if isinstance(w, str):
        if w == "equal":
            return "fixed"
        if w in _WEIGHT_AUTOS:
            return w
        raise StrategyError(f"weight 枚举非法：{w!r}（可选 equal / {' / '.join(_WEIGHT_AUTOS)} / 数值）")
    if isinstance(w, (int, float)) and not isinstance(w, bool):
        return "fixed"
    raise StrategyError(f"weight 类型非法：{w!r}")


def load_strategy(path: Path, repo: Path, db_path: Path) -> dict:
    """策略 YAML → 归一 spec。校验失败一次报全（StrategyError 逐条列全，不挤牙膏）。"""
    path = Path(path)
    if not path.exists():
        raise StrategyError(f"策略文件不存在：{path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise StrategyError(f"YAML 解析失败：{e}") from e
    if not isinstance(raw, dict):
        raise StrategyError("策略文件顶层必须是映射（dict）")

    errs: list[str] = []

    # ── id / name ──
    sid = raw.get("id")
    if not sid or not isinstance(sid, str) or not _ID_RE.match(sid):
        errs.append(f"id 必填且为 kebab-case：{sid!r}")
    name = str(raw.get("name") or sid or "")

    # ── factors ──
    factors_raw = raw.get("factors")
    if not isinstance(factors_raw, list) or not (1 <= len(factors_raw) <= _MAX_FACTORS):
        errs.append(f"factors 必填且为 1..{_MAX_FACTORS} 个的列表")
        factors_raw = []
    factors: list[dict] = []
    seen_fid: set[str] = set()
    for i, f in enumerate(factors_raw):
        if not isinstance(f, dict) or not f.get("id"):
            errs.append(f"factors[{i}].id 必填")
            continue
        fid = str(f["id"])
        if fid in seen_fid:
            errs.append(f"factors[{i}] 因子重复：{fid}")
            continue
        seen_fid.add(fid)
        direction = f.get("direction", "desc")
        if direction not in ("asc", "desc"):
            errs.append(f"factors[{i}].direction 枚举非法：{direction!r}（asc | desc）")
        w = f.get("weight", 1.0)
        try:
            kind = parse_weight_kind(w)
        except StrategyError as e:
            errs.append(f"factors[{i}].{e}")
            kind = "fixed"
            w = 1.0
        factors.append({"id": fid, "direction": direction,
                        "weight": (1.0 if w == "equal" else float(w)) if kind == "fixed" else w,
                        "weight_kind": kind})

    # ── composite ──
    comp = raw.get("composite") or {}
    if not isinstance(comp, dict):
        comp, errs = {}, errs + ["composite 必须是映射"]
    top_n = comp.get("top_n")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        errs.append(f"composite.top_n 必须为 ≥1 整数：{top_n!r}")
        top_n = 1
    methods = comp.get("method")
    if (not isinstance(methods, list) or not methods
            or any(m not in _METHODS for m in methods)):
        errs.append(f"composite.method 必须为 {_METHODS} 的非空子集：{methods!r}")
        methods = ["equal"]
    if len(set(methods)) != len(methods):
        errs.append(f"composite.method 存在重复：{methods!r}")

    # ── rebalance / exec / benchmark / pin ──
    rebalance = raw.get("rebalance", "monthly")
    if rebalance not in BT.REBALANCE_CHOICES:
        errs.append(f"rebalance 枚举非法：{rebalance!r}（可选 {BT.REBALANCE_CHOICES}）")
    ex = raw.get("exec", "close")
    if ex not in _EXEC_CHOICES:
        errs.append(f"exec 枚举非法：{ex!r}（可选 {_EXEC_CHOICES}；next_day_vwap 由撮合层 T-C3 实现）")
    benchmark = str(raw.get("benchmark") or "510300.SH")
    fv = raw.get("factor_version")
    if fv is not None and not isinstance(fv, str):
        errs.append(f"factor_version 必须为字符串或 null：{fv!r}")

    # ── constraints / universe ──
    cons = raw.get("constraints") or {}
    if not isinstance(cons, dict):
        cons, errs = {}, errs + ["constraints 必须是映射"]
    uni = raw.get("universe")
    filters: dict = {}
    markets = ["CN"]                                  # T-F9：缺省 CN（存量 YAML 零迁移）
    if isinstance(uni, str):
        uni_base = uni                      # 串形式 = 只声明底池，无过滤
    elif isinstance(uni, dict):
        uni_base = str(uni.get("base") or "CN_EQUITY_ALL")
        filters = uni.get("filters") or {}
        if not isinstance(filters, dict):
            filters, errs = {}, errs + ["universe.filters 必须是映射"]
        raw_mkts = uni.get("markets", ["CN"])
        if (not isinstance(raw_mkts, list) or not raw_mkts
                or any(m not in _MARKETS for m in raw_mkts)):
            errs.append(f"universe.markets 必须为 {_MARKETS} 的非空子集：{raw_mkts!r}")
        elif len(set(raw_mkts)) != len(raw_mkts):
            errs.append(f"universe.markets 存在重复：{raw_mkts!r}")
        else:
            markets = [str(m) for m in raw_mkts]
    else:
        if uni is None:
            uni_base, errs = "CN_EQUITY_ALL", errs + ["universe 必填（字符串底池或映射）"]
        else:
            uni_base, errs = "CN_EQUITY_ALL", errs + [f"universe 必须为字符串或映射：{uni!r}"]

    if errs:
        raise StrategyError("策略 DSL 校验失败（" + str(len(errs)) + " 项）：\n  - " + "\n  - ".join(errs))

    spec = {"id": sid, "name": name, "factors": factors, "top_n": top_n, "methods": methods,
            "rebalance": rebalance, "exec": ex, "benchmark": benchmark,
            "factor_version": fv,
            "constraints": {"t_plus_1": bool(cons.get("t_plus_1", False)),
                            "limit_move": cons.get("limit_move", "defer"),
                            "suspension": cons.get("suspension", "defer"),
                            "lot_size": int(cons.get("lot_size", 0) or 0),
                            "max_weight": cons.get("max_weight"),
                            "min_history": cons.get("min_history")},
            "universe": {"base": uni_base, "filters": filters, "markets": markets}}

    # ── 池准入（M5 契约：合成器因子只准来自 active_pool）──
    _gate_pool(spec, repo, db_path)

    # ── 多市场扩展位（T-F9 接口预留）：非 CN 市场就绪性预检，缺口显式披露不阻断 ──
    if set(markets) - {"CN"}:
        spec["market_gaps"] = _market_readiness(spec, repo, db_path)
    return spec


def assert_cn_only(spec: dict) -> None:
    """回测/wf 链路的市场口径守卫：多市场混合回测 M8+ 前显式拒绝。

    红线：不把混合市场静默当 CN 跑（基准/成本/交易日历跨市场不一致，语义未定义）。
    """
    mkts = (spec.get("universe") or {}).get("markets") or ["CN"]
    if set(mkts) != {"CN"}:
        raise StrategyError(
            f"多市场混合回测未实现（universe.markets={mkts}）：T-F9 为接口预留位，"
            "M8+ 分市场重过 §6.4 后开放混合口径——显式拒绝，不按 CN 静默降级")


def _market_readiness(spec: dict, repo: Path, db_path: Path) -> list[str]:
    """非 CN 市场就绪性预检（T-F9 接口预留）：缺口逐条列示，不阻断加载（诚实披露）。

    预检 ≠ 重检验：结论只用于披露「HK/US 财务科目映射/数据就绪性」，不做替代计算。
    """
    from factorlib.account_map import market_readiness
    from factorlib.registry import RegistryError, load_factor
    finputs: list[dict] = []
    for f in spec["factors"]:
        try:
            m = load_factor(repo, f["id"])
        except RegistryError:
            continue                                  # 池准入已保证注册，此处只防御
        finputs.append({"id": m.id, "inputs": list(m.inputs)})
    ext = [m for m in spec["universe"]["markets"] if m != "CN"]
    res = market_readiness(repo, finputs, ext, db_path)
    gaps = [g for seg in res["markets"].values() for g in seg["gaps"]]
    if len(spec["universe"]["markets"]) > 1:
        gaps.append("混合口径披露：多市场回测的基准/成本/交易日历跨市场不一致，混合回测语义"
                    "M8+ 分市场重过 §6.4 后才开放；当前回测链路仅支持 CN 单市场（显式拒绝混合回测）")
    return gaps


def _gate_pool(spec: dict, repo: Path, db_path: Path) -> None:
    """factors ⊆ active_pool；非池因子逐个列出现状（excluded 原因/状态/health）。"""
    from factorlib.lifecycle import active_pool
    from factorlib.registry import load_registry
    pool_ids = {p["factor_id"] for p in active_pool(db_path, repo)["pool"]}
    defs = {m.id: m for m in load_registry(repo)}
    bad = []
    for f in spec["factors"]:
        fid = f["id"]
        if fid in pool_ids:
            continue
        if fid in defs:
            m = defs[fid]
            bad.append(f"{fid}（status={m.status}, version={m.version}——不在合成池："
                       "draft/deprecated 或 health=invalid 摘除）")
        else:
            bad.append(f"{fid}（未注册：factorlib defs 中无此因子定义）")
    if bad:
        raise StrategyError("因子不在合成池（active ∧ health≠invalid），拒绝合成：\n  - "
                            + "\n  - ".join(bad))


# ── universe 过滤（flt_st / flt_listed_days / flt_avg_amount_20d）─────────
_FILTER_NAMES = ("flt_st", "flt_listed_days", "flt_avg_amount_20d")
_AMT_WINDOW = 20


def _load_names(repo: Path) -> dict[str, str] | None:
    """secid → name（ST 判定源）。dim 缺失 → None（flt_st 降级）。"""
    p = Path(repo) / "dim" / "security.json"
    if not p.exists():
        return None
    try:
        dims = json.loads(p.read_text(encoding="utf-8"))
        return {d["secid"]: str(d.get("name") or "") for d in dims}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def apply_universe(filters: dict, bars: dict, dates: list[str],
                   repo: Path | None = None) -> tuple[dict[str, set[str]], dict]:
    """三过滤器作用于回测日轴，返回 ({d: secid_set}, 过滤快照)。

    - flt_st：名称含 ST/*ST 剔除；name 源（dim/security.json）缺失 → degraded（N/A 披露）；
    - flt_listed_days：交易日口径近似——secid 在 bars 中 dt ≤ d 的根数 ≥ 阈值
      （无独立上市日数据源，首 bar 近似，如实登记）；
    - flt_avg_amount_20d：dt < d 的过去 20 根 bar amount 均值 ≥ 阈值；样本不足 20 根 → 排除
      （数据不足不放行）。
    快照：每个过滤器记首个启用日的 n_before/n_after（建仓期代表性）+ degraded 标记。
    数据源：bars 行自带 amount（T-C1 盘点确认），零 duckdb 依赖。
    """
    unknown = set(filters) - set(_FILTER_NAMES)
    if unknown:
        raise StrategyError(f"universe.filters 未知过滤器：{sorted(unknown)}（可选 {_FILTER_NAMES}）")
    names = _load_names(repo) if repo else None   # 无 repo = 无名称源 → flt_st 降级
    snap: dict[str, dict] = {}
    out: dict[str, set[str]] = {}
    for d in sorted(set(dates)):
        cur = set(bars)
        for fname, cfg in filters.items():
            if not cfg:                                   # false / 0 / null = 关闭
                continue
            before = len(cur)
            degraded = None
            if fname == "flt_st":
                if names is None:
                    degraded = "name 源缺失（dim/security.json）——未过滤"
                else:
                    cur = {s for s in cur if "ST" not in names.get(s, "").upper()}
            elif fname == "flt_listed_days":
                need = int(cfg)
                cur = {s for s in cur
                       if sum(1 for r in bars[s] if r["dt"] <= d) >= need}
            else:                                         # flt_avg_amount_20d
                need = float(cfg)
                keep = set()
                for s in cur:
                    hist = [r["amount"] for r in bars[s] if r["dt"] < d]
                    if len(hist) >= _AMT_WINDOW and sum(hist[-_AMT_WINDOW:]) / _AMT_WINDOW >= need:
                        keep.add(s)
                cur = keep
            entry = snap.setdefault(fname, {})
            if "n_before" not in entry:                   # 首个启用日 = 快照锚点
                entry.update({"n_before": before, "n_after": len(cur)})
                if degraded:
                    entry["degraded"] = degraded
        out[d] = cur
    return out, snap
