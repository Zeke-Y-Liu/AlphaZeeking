"""引擎指标注册表 v2（T-R1）：schema 常量、导出器、加载器、校验器。

设计要点（00-总览 §2.2）：
- 配置即数据：指标启停/参数/权重/锚点/维度归属全部进 ``repo/factors/registry.json`` v2，
  增删改 = 改注册表 + 门禁校验，不再改代码；
- 公式真源不搬家：本模块只**引用** factors.py 既有常量做导出，不复制公式实现；
- 透明降级：注册表缺失/损坏/版本不符 → 回退内置默认（现常量值）并记 WARNING，
  绝不抛异常阻断评估或同步（引擎C 纪律红线同款）。

alpha/beta 的运行时定义均已收敛为 factors 模块级常量（真源唯一），本模块直接引用：
锚点 factors._ALPHA_ANCHORS（T-R5，原 compute_alpha 内联字面量）、信号
factors._BETA_SIGNALS（T-R4，含 detail 模板）。
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import factors as F

log = logging.getLogger("ap.engines")

# ── schema 常量 ──────────────────────────────────────────────
REGISTRY_VERSION = 2
METRIC_STATUS = ("active", "inactive", "experimental")
SCORER_TYPES = ("quant", "mom", "step", "clip", "pulse")
ALPHA_DIMS = tuple(F.DEFAULT_DIM_WEIGHTS.keys())          # 估值/质量/成长/动量/红利低波
MACRO_DIMS = ("D1", "D2", "D3", "D4")
MACRO_MARKETS = ("A", "HK", "US")
BETA_SIGNAL_IDS = tuple(F.ENGINE_B_SIGNAL_IDS)
ALPHA_ANCHOR_KEYS = ("s_val", "s_qua", "s_gro", "s_mom", "s_dvd", "s_lvol")
BETA_DEFAULT_KEYS = ("win", "band", "band_sigma", "bias_thresh", "vpd_thresh", "pct_window")
MACRO_REGIME_KEYS = ("chg_win", "peg_strong", "peg_weak", "carry_widen_pt")

# 代码内置公式清单（formula 形如 "builtin:<id>"；T-R7 add 校验用；
# macro_series = 引擎C 序列打分型指标（scorer 驱动），scorer 型公式的唯一合法值）
KNOWN_FORMULAS = frozenset({
    # 引擎 A（factors.compute_alpha）
    "pe_ttm_pit", "pb_pit", "pctile_pe_5y", "pctile_pb_5y", "div_yield_ttm",
    "roe_ttm", "margin", "rev_g", "np_g", "mom_12_1", "vol_250", "rev",
    "quality_leverage", "quality_accrual", "quality_fcf",
    "amihud_20d", "roll_60d", "cs_spread_60d",
    # 引擎 B（factors.compute_beta）
    "mom", "vol", "ma_break", "high_touch", "dist_high", "amt_pctile",
    # 引擎 C（factors.compute_macro_liquidity：scorer 型序列指标）
    "macro_series",
})


@dataclass
class MetricSpec:
    """注册表中的单条指标声明（三引擎同构）。"""
    id: str
    engine: str                       # "alpha" | "beta" | "macro"
    formula: str                      # "builtin:<公式id>"；macro 引擎用 scorer 字段
    dim: str | None = None            # alpha: ALPHA_DIMS；macro: D1..D4；beta: None
    market: str | None = None         # 仅 macro："A"|"HK"|"US"
    dataset: str | None = None        # 仅 macro：数据来源数据集名
    scorer: dict | None = None        # 仅 macro：{"type": ..., "arg": ...}
    params: dict = field(default_factory=dict)
    status: str = "active"
    version: int = 1
    hypothesis_id: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("dim", "market", "dataset", "scorer", "hypothesis_id")}


# ── 引擎基类（T-R2）：包装器零公式，run 一行委托 factors ─────────


class BaseEngine:
    """引擎包装器基类：注册表加载（透明降级）+ 指标查询面 + describe。

    ``run`` 等计算入口由子类定义，体内只做一行委托到 factors 纯函数（公式真源不搬家）。
    """
    id: str = ""
    name: str = ""
    kind: str = ""                # cross_section | timing | macro_regime

    def __init__(self):
        self.registry: dict | None = None   # None = 未 load（内置默认）

    def load(self, repo_dir: Path | str | None = None) -> BaseEngine:
        """从 workspace repo 加载注册表（幂等刷新）；repo_dir=None 保持内置默认。"""
        if repo_dir is not None:
            self.registry = load_registry(repo_dir)
        return self

    @property
    def _eng(self) -> dict:
        """当前生效的引擎配置节（registry 优先，否则内置默认）。"""
        reg = self.registry or export_registry_v2()
        return reg["engines"].get(self.id) or {}

    def metrics(self) -> list[MetricSpec]:
        """指标清单（含 status；orphan 条目不入运行时面——公式已移除，仅盘上留存）。"""
        return [MetricSpec(**{k: v for k, v in m.items() if k != "orphan"})
                for m in self._eng.get("metrics", []) if not m.get("orphan")]

    def active_ids(self) -> set[str]:
        """status == active 的指标 id 集合（T-R3/4/5 的启停白名单来源）。"""
        return {m.id for m in self.metrics() if m.status == "active"}

    def describe(self) -> dict:
        """自描述（调试/文档用；T-R8 复用）。"""
        ms = self.metrics()
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "metric_count": len(ms), "active_count": sum(1 for m in ms if m.status == "active"),
                "dim_weights": self._eng.get("dim_weights")}


# ── 导出器：从 factors.py 现有常量/函数内字面量导出 v2 ───────────


# alpha 锚点表（T-R5 起直接引用 factors._ALPHA_ANCHORS，真源唯一；导出时转为
# 注册表同构 list 形状 [[x, score], ...]）
_ALPHA_ANCHORS: dict[str, list] = {k: [list(p) for p in v] for k, v in F._ALPHA_ANCHORS.items()}

# alpha 因子 → 维度归属（来源：factors.compute_alpha L233 dims 构成）
_ALPHA_DIM_OF = {
    "val_pe_ttm": "估值", "val_pb": "估值", "val_pe_pct": "估值", "val_pb_pct": "估值",
    "div_yield_ttm": "红利低波",
    "qua_roe_ttm": "质量", "qua_margin": "质量",
    "qua_leverage": "质量", "qua_accrual": "质量", "qua_fcf": "质量",
    "grow_rev_yoy": "成长", "grow_np_yoy": "成长",
    "mom_12_1": "动量", "vol_250": "动量", "rev_20d": "动量", "rev_60d": "动量",
    # 微观流动性诊断族：语义近低风险溢价（红利低波），不参与该维度评分（诊断口径同 qua_margin）
    "liq_amihud_20d": "红利低波", "liq_roll_60d": "红利低波", "liq_cs_60d": "红利低波",
}

_ALPHA_FORMULA = {
    "val_pe_ttm": "pe_ttm_pit", "val_pb": "pb_pit",
    "val_pe_pct": "pctile_pe_5y", "val_pb_pct": "pctile_pb_5y",
    "div_yield_ttm": "div_yield_ttm", "qua_roe_ttm": "roe_ttm", "qua_margin": "margin",
    "grow_rev_yoy": "rev_g", "grow_np_yoy": "np_g",
    "mom_12_1": "mom_12_1", "vol_250": "vol_250",
    "rev_20d": "rev", "rev_60d": "rev",
    "qua_leverage": "quality_leverage", "qua_accrual": "quality_accrual", "qua_fcf": "quality_fcf",
    "liq_amihud_20d": "amihud_20d", "liq_roll_60d": "roll_60d", "liq_cs_60d": "cs_spread_60d",
}

_ALPHA_PARAMS = {"rev_20d": {"n": 20}, "rev_60d": {"n": 60}}

# beta 信号定义（T-R4 起直接引用 factors._BETA_SIGNALS 运行时形状：真源唯一，
# {id, status, evidence, cooldown, type/action/detail(方向分支)}，detail 为 str.format 模板）

_BETA_FORMULA = {
    "mom_20d": ("mom", {"n": 20}), "vol_20d": ("vol", {"n": 20}),
    "ma20_break": ("ma_break", {"win": 20, "band": 0.02}),
    "high20_touch": ("high_touch", {"win": 20}),
    "dist_high20": ("dist_high", {"win": 20}),
    "turnover_pct": ("amt_pctile", {"pct_window": 250}),
}

# beta 公式默认参数（来源：factors.compute_beta 签名默认值 L262-265）
_BETA_DEFAULTS = {"win": 20, "band": 0.02, "band_sigma": 1.5,
                  "bias_thresh": 0.08, "vpd_thresh": 0.30, "pct_window": 250}

# macro regime 阈值（来源：factors L406-408 常量）
_MACRO_REGIME = {"chg_win": 20, "peg_strong": 7.75, "peg_weak": 7.85, "carry_widen_pt": 0.5}


def _macro_metrics() -> list[dict]:
    """从 _MACRO_DIM_OF / _MACRO_SCORERS 反演每指标声明（13 条：10+3）。"""
    dim_of: dict[str, tuple[str, str]] = {}      # id -> (market, dim)
    for mkt, dims in F._MACRO_DIM_OF.items():
        for dim, ids in dims.items():
            for mid in ids:
                dim_of[mid] = (mkt, dim)
    out = []
    for mid in F.MACRO_FACTORS:                                   # macro_liquidity 数据集 10 条
        mkt, dim = dim_of.get(mid, ("US", None))
        sc = F._MACRO_SCORERS.get(mid)
        out.append({
            "id": mid, "engine": "macro",
            "formula": "builtin:macro_series",
            "dim": dim, "market": mkt, "dataset": "macro_liquidity",
            "scorer": ({"type": sc[0], "arg": sc[1]} if sc else None),
            "params": {}, "status": "active", "version": 1,
            "hypothesis_id": None,
            "note": "" if sc else "背景指标：同步落盘但不参与打分（scorer 未登记）",
        })
    for mid in ("hibor_1m", "carry_hk_us", "peg_pressure"):       # hk_rates 数据集 3 条（HK）
        sc = F._MACRO_SCORERS[mid]
        out.append({
            "id": mid, "engine": "macro", "formula": "builtin:macro_series",
            "dim": dim_of[mid][1], "market": "HK", "dataset": "hk_rates",
            "scorer": {"type": sc[0], "arg": sc[1]},
            "params": {}, "status": "active", "version": 1, "hypothesis_id": None,
            "note": "来源 market/hk_rates.json（H-0006 港美利率族）",
        })
    return out


def export_registry_v2() -> dict:
    """从 factors.py 现有常量导出完整 v2 注册表（内存产物，不落盘；落盘写保护在 T-R6）。"""
    return {
        "version": REGISTRY_VERSION,
        "updated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "engines": {
            "alpha": {
                "dim_weights": dict(F.DEFAULT_DIM_WEIGHTS),
                "anchors": {k: [list(p) for p in v] for k, v in _ALPHA_ANCHORS.items()},
                "metrics": [{
                    "id": fid, "engine": "alpha",
                    "formula": f"builtin:{_ALPHA_FORMULA[fid]}",
                    "dim": _ALPHA_DIM_OF[fid], "market": None, "dataset": None,
                    "scorer": None, "params": dict(_ALPHA_PARAMS.get(fid, {})),
                    "status": "active", "version": 1, "hypothesis_id": None, "note": "",
                } for fid in F.ENGINE_A_FACTORS],
            },
            "beta": {
                "defaults": dict(_BETA_DEFAULTS),
                "signals": [copy.deepcopy(s) for s in F._BETA_SIGNALS],
                "metrics": [{
                    "id": fid, "engine": "beta",
                    "formula": f"builtin:{_BETA_FORMULA[fid][0]}",
                    "dim": None, "market": None, "dataset": None, "scorer": None,
                    "params": dict(_BETA_FORMULA[fid][1]),
                    "status": "active", "version": 1, "hypothesis_id": None, "note": "",
                } for fid in F.ENGINE_B_FACTORS],
            },
            "macro": {
                "dim_weights": dict(F.MACRO_DIM_WEIGHTS),
                "regime": dict(_MACRO_REGIME),
                "metrics": _macro_metrics(),
            },
        },
        "changelog": [],
    }


# ── 校验器 ────────────────────────────────────────────────────


def validate_registry(reg: dict) -> list[str]:
    """返回错误清单（空列表 = 合法）；只报告不修复。"""
    errs: list[str] = []

    def _err(msg: str):
        errs.append(msg)

    if not isinstance(reg, dict):
        return ["顶层必须是 dict"]
    if reg.get("version") != REGISTRY_VERSION:
        _err(f"version 必须为 {REGISTRY_VERSION}（实际 {reg.get('version')!r}）")
    eng = reg.get("engines")
    if not isinstance(eng, dict) or set(eng.keys()) != {"alpha", "beta", "macro"}:
        _err("engines 必须含且仅含 alpha/beta/macro 三键")
        return errs

    for eid in ("alpha", "beta", "macro"):
        sec = eng[eid]
        if not isinstance(sec, dict):
            _err(f"engines.{eid} 必须是 dict")
            continue
        metrics = sec.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            _err(f"engines.{eid}.metrics 必须是非空 list")
            continue
        seen: set[str] = set()
        for i, m in enumerate(metrics):
            tag = f"engines.{eid}.metrics[{i}]"
            if not isinstance(m, dict) or not m.get("id"):
                _err(f"{tag}.id 缺失或为空")
                continue
            mid = m["id"]
            if mid in seen:
                _err(f"{tag} 指标 id 重复：{mid}")
            seen.add(mid)
            if m.get("engine", eid) != eid:
                _err(f"{tag}.engine 与所在节不符：{m.get('engine')!r} != {eid}")
            if m.get("status", "active") not in METRIC_STATUS:
                _err(f"{tag}.status 非法：{m.get('status')!r}")
            if not isinstance(m.get("version", 1), int) or m.get("version", 1) < 1:
                _err(f"{tag}.version 必须 ≥ 1")
            fml = m.get("formula", "")
            if not (isinstance(fml, str) and fml.startswith("builtin:")):
                _err(f"{tag}.formula 必须形如 builtin:<id>（实际 {fml!r}）")

    # alpha 专属：维度/权重/锚点
    a = eng["alpha"]
    for m in a.get("metrics", []):
        if m.get("dim") not in ALPHA_DIMS:
            errs.append(f"alpha 指标 {m.get('id')} 的 dim 非法：{m.get('dim')!r}")
    w = a.get("dim_weights")
    if not isinstance(w, dict) or set(w.keys()) != set(ALPHA_DIMS):
        errs.append(f"alpha.dim_weights 键必须为 {list(ALPHA_DIMS)}")
    elif any(not isinstance(v, (int, float)) or v < 0 for v in w.values()):
        errs.append("alpha.dim_weights 权重必须为非负数")
    anc = a.get("anchors")
    if not isinstance(anc, dict) or not set(anc.keys()) <= set(ALPHA_ANCHOR_KEYS):
        errs.append(f"alpha.anchors 键必须 ⊆ {list(ALPHA_ANCHOR_KEYS)}")

    # beta 专属：defaults/信号
    b = eng["beta"]
    d = b.get("defaults")
    if not isinstance(d, dict) or not set(d.keys()) <= set(BETA_DEFAULT_KEYS):
        errs.append(f"beta.defaults 键必须 ⊆ {list(BETA_DEFAULT_KEYS)}")
    sigs = b.get("signals")
    if not isinstance(sigs, list) or not sigs:
        errs.append("beta.signals 必须是非空 list")
    else:
        sids = [s.get("id") for s in sigs if isinstance(s, dict)]
        if len(sids) != len(set(sids)):
            errs.append("beta.signals 存在重复 id")
        for s in sigs:
            if not isinstance(s, dict):
                errs.append(f"beta.signals 条目非 dict：{s!r}")
            elif s.get("orphan"):
                continue                                 # orphan 信号：盘上留存，免内置集校验
            elif s.get("id") not in BETA_SIGNAL_IDS:
                errs.append(f"beta.signals 的 id 必须 ∈ {list(BETA_SIGNAL_IDS)}（实际 {s.get('id')!r}）")
            elif s.get("status", "active") not in METRIC_STATUS:
                errs.append(f"beta 信号 {s['id']}.status 非法")

    # macro 专属：维度/市场/scorer/regime
    mc = eng["macro"]
    mw = mc.get("dim_weights")
    if not isinstance(mw, dict) or not (set(mw.keys()) <= set(MACRO_DIMS)) or not mw:
        errs.append(f"macro.dim_weights 键必须 ⊆ {list(MACRO_DIMS)} 且非空")
    rg = mc.get("regime")
    if not isinstance(rg, dict) or not set(rg.keys()) <= set(MACRO_REGIME_KEYS):
        errs.append(f"macro.regime 键必须 ⊆ {list(MACRO_REGIME_KEYS)}")
    elif rg.get("peg_weak", 7.85) <= rg.get("peg_strong", 7.75):
        errs.append("macro.regime.peg_weak 必须 > peg_strong")
    for m in mc.get("metrics", []):
        if m.get("market") not in MACRO_MARKETS:
            errs.append(f"macro 指标 {m.get('id')} 的 market 非法：{m.get('market')!r}")
        if m.get("dim") is not None and m.get("dim") not in MACRO_DIMS:
            errs.append(f"macro 指标 {m.get('id')} 的 dim 非法：{m.get('dim')!r}")
        sc = m.get("scorer")
        if sc is not None and (not isinstance(sc, dict) or sc.get("type") not in SCORER_TYPES):
            errs.append(f"macro 指标 {m.get('id')} 的 scorer.type 非法：{sc!r}")

    # changelog 结构
    cl = reg.get("changelog", [])
    if not isinstance(cl, list):
        errs.append("changelog 必须是 list")
    return errs


# ── 加载器（透明降级）────────────────────────────────────────


def load_registry(repo_dir: Path | str) -> dict:
    """加载 ``<repo_dir>/factors/registry.json``。

    文件不存在 / JSON 解析失败 / version != 2 / 校验失败 → 记 WARNING 并返回
    ``export_registry_v2()`` 深拷贝（内置默认，与现常量一致）；合法 → 返回解析结果深拷贝。
    """
    p = Path(repo_dir) / "factors" / "registry.json"
    default = lambda: copy.deepcopy(export_registry_v2())
    if not p.exists():
        log.warning("注册表不存在（%s），回退内置默认指标清单", p)
        return default()
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("注册表解析失败（%s：%s），回退内置默认指标清单", p, e)
        return default()
    if not isinstance(reg, dict) or reg.get("version") != REGISTRY_VERSION:
        log.warning("注册表版本不符（version=%r，期望 %s），回退内置默认指标清单",
                    reg.get("version") if isinstance(reg, dict) else type(reg).__name__,
                    REGISTRY_VERSION)
        return default()
    errs = validate_registry(reg)
    if errs:
        log.warning("注册表校验未通过（%d 项：%s…），回退内置默认指标清单", len(errs), errs[0])
        return default()
    return copy.deepcopy(reg)


def load_registry_raw(repo_dir: Path | str) -> dict | None:
    """原样读取盘上注册表（不校验、不降级）：缺失/JSON 损坏/非 dict → None。

    供 merge_registry（T-R6 写保护）作输入——与 load_registry 的差别：不校验
    version/schema，盘上内容原样带回（含 v1 旧格式），损坏才返回 None。
    """
    p = Path(repo_dir) / "factors" / "registry.json"
    if not p.exists():
        return None
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("注册表原样读取失败（%s：%s），按无文件处理", p, e)
        return None
    return reg if isinstance(reg, dict) else None


# ── 因子条目视图（v1/v2 兼容；只读/写回消费方零形态感知）────────


def factor_entries(reg: dict) -> list[dict]:
    """注册表因子条目扁平视图（v1/v2 同构），供只读消费方复用。

    v2 注册表（``{version, engines, changelog}``）无顶层 ``factors`` 键——因子条目分散在
    ``engines.{alpha,beta,macro}.metrics`` 与 ``engines.beta.signals``；v1 旧格式
    （``{factors:[…]}``）直取。统一摊平为含 ``id``/``status`` 的条目（附 ``engine``
    标注），使评估/巡检/审计等消费方无需感知盘上形态差异。

    返回浅拷贝快照：修改返回值不影响 ``reg``（需原地写回请用 :func:`find_factor_entry`）。
    """
    if not isinstance(reg, dict):
        return []
    if isinstance(reg.get("factors"), list):
        return [f for f in reg["factors"] if isinstance(f, dict) and f.get("id")]
    out: list[dict] = []
    for eid, sec in (reg.get("engines") or {}).items():
        if not isinstance(sec, dict):
            continue
        for key in ("metrics", "signals"):
            for item in sec.get(key) or []:
                if isinstance(item, dict) and item.get("id"):
                    out.append({**item, "engine": eid})
    return out


def find_factor_entry(reg: dict, factor_id: str) -> dict | None:
    """取注册表中该因子的**底层条目引用**（v1/v2 同构；可原地写回随 ``reg`` 落盘）。

    与 :func:`factor_entries` 的差别：不摊平、不拷贝——直接返回盘上原始条目对象，供需要
    mutate 后落盘的调用方（假设血缘绑定/激活、延迟衰减降级）原地修改。
    """
    if not isinstance(reg, dict):
        return None
    if isinstance(reg.get("factors"), list):
        return next((f for f in reg["factors"]
                     if isinstance(f, dict) and f.get("id") == factor_id), None)
    for sec in (reg.get("engines") or {}).values():
        if not isinstance(sec, dict):
            continue
        for key in ("metrics", "signals"):
            for item in sec.get(key) or []:
                if isinstance(item, dict) and item.get("id") == factor_id:
                    return item
    return None


# merge 规则 2 中「用户改动优先」的 metric 级字段（formula/id/engine 属代码所有，不随盘上）
_METRIC_USER_KEYS = ("status", "params", "scorer", "dim", "market", "dataset",
                     "version", "hypothesis_id", "note")


def merge_registry(on_disk: dict | None, defaults: dict) -> dict:
    """registry 写保护合并（T-R6）：用户盘上改动优先，代码公式演进不丢数据。

    规则（metric 维度，三引擎同构；beta.signals 以 id 为键同规则）：
    1. defaults 有、on_disk 没有 → 补默认条目（新指标自动出现）；
    2. 两边都有 → 保留 on_disk 的 status/params/scorer/dim/market/dataset/version/
       hypothesis_id/note（用户改动优先；formula/id/engine 属代码所有不随盘上）；
    3. on_disk 有、defaults 没有 → 门禁新增（hypothesis_id 非空且 formula ∈
       KNOWN_FORMULAS）保留为正式条目（用户新增因子同步后不丢）；其余标
       ``"orphan": true``（代码公式已移除，不删数据；orphan 不入运行时面，
       见 BaseEngine.metrics）；
    4. on_disk 损坏/非注册表结构 → 整体回退 defaults（记 WARNING，与 load_registry 一致）；
    5. 顶层 updated_at 刷新为当前时间；version 恒为 2。

    v1 兼容：盘上 ``{"factors":[…]}``（现网旧格式，无 version 键）→ 仅继承条目 status
    （合法值才继承），其余字段取 defaults；v1 独有条目（实践为空集）迁入顶层
    ``"orphans"`` 停车场（形状与 v2 不同构，不入 metrics 以保 validate 通过）。

    引擎级配置：alpha.dim_weights/macro.dim_weights **整表取 on_disk**（不逐键混合，
    避免半新半旧；alpha 键集须与 defaults 全等、macro 须非空子集——保 validate 红线）；
    alpha.anchors 整表取（过滤未知键）；beta.defaults/macro.regime 逐键合并（键集受
    校验器约束）；merge 结果必过 ``validate_registry``（不产非法文件）。
    """
    merged = copy.deepcopy(defaults)
    if not isinstance(on_disk, dict):
        return merged                                   # 规则4/首次 bootstrap：全量 defaults

    od_engines = on_disk.get("engines")
    is_v2 = on_disk.get("version") == REGISTRY_VERSION and isinstance(od_engines, dict)
    is_v1 = isinstance(on_disk.get("factors"), list)
    if not is_v2 and not is_v1:
        log.warning("注册表结构不可识别（无 version==%d 亦无 factors 节），整体回退内置默认",
                    REGISTRY_VERSION)
        return merged                                   # 规则4：整体回退

    # v1 旧格式：摊平为 {id: entry}，仅 status 可继承；独有条目迁入顶层 orphans 停车场
    v1_by_id: dict[str, dict] = {}
    if is_v1 and not is_v2:
        for f in on_disk["factors"]:
            if isinstance(f, dict) and f.get("id"):
                v1_by_id[f["id"]] = f
        defaults_ids = {m["id"] for sec in merged["engines"].values() for m in sec["metrics"]}
        stale = [dict(f, note="v1 遗留条目（代码清单已无此 id）")
                 for fid, f in v1_by_id.items() if fid not in defaults_ids]
        if stale:
            merged["orphans"] = stale

    for eid, d_sec in merged["engines"].items():
        o_sec = od_engines.get(eid) if is_v2 else None

        # ── metric 条目 merge（规则1/2/3）──
        od_metrics: dict[str, dict] = {}
        if is_v2:
            od_metrics = {m["id"]: m for m in ((o_sec or {}).get("metrics") or [])
                          if isinstance(m, dict) and m.get("id")}
        out_metrics = []
        for d_m in d_sec["metrics"]:
            o_m = od_metrics.pop(d_m["id"], None)
            if o_m is None:
                if is_v1 and d_m["id"] in v1_by_id:      # v1 兼容：仅继承合法 status
                    st = v1_by_id[d_m["id"]].get("status")
                    if st in METRIC_STATUS:
                        d_m = {**d_m, "status": st}
                out_metrics.append(dict(d_m))            # 规则1：补默认条目
                continue
            m = dict(d_m)                                # 规则2：用户改动优先
            for k in _METRIC_USER_KEYS:
                if k in o_m:
                    m[k] = o_m[k]
            out_metrics.append(m)
        for o_m in od_metrics.values():              # 规则3：盘上独有条目
            # 门禁新增（engine.metrics.add 强制 hypothesis_id）且 formula 仍在内置
            # 清单 → 保留为正式条目（用户新增因子在同步后完整保留，不入 orphan）；
            # 其余（代码公式已移除的旧默认指标）→ orphan 保留（不删数据，不入运行时面）
            fml = o_m.get("formula", "")
            user_added = bool(o_m.get("hypothesis_id")) and isinstance(fml, str) \
                and fml.startswith("builtin:") and fml.split(":", 1)[1] in KNOWN_FORMULAS
            out_metrics.append(dict(o_m, engine=eid) if user_added
                               else dict(o_m, engine=eid, orphan=True))
        d_sec["metrics"] = out_metrics

        if not is_v2 or not isinstance(o_sec, dict):
            continue

        # ── 引擎级配置（权重表整取 / anchors 过滤整取 / defaults·regime 逐键）──
        for key in ("dim_weights", "anchors", "defaults", "regime"):
            o_val = o_sec.get(key)
            if not isinstance(o_val, dict) or key not in d_sec or not isinstance(d_sec[key], dict):
                continue
            if key == "dim_weights":                     # 整取，键集守卫保 validate
                need = set(d_sec[key].keys())
                ok = (set(o_val.keys()) == need if eid == "alpha"
                      else bool(o_val) and set(o_val.keys()) <= need)
                if ok:
                    d_sec[key] = copy.deepcopy(o_val)
            elif key == "anchors":                       # 过滤未知键后整取
                d_sec[key] = {k: copy.deepcopy(v) for k, v in o_val.items()
                              if k in ALPHA_ANCHOR_KEYS and isinstance(v, list)}
            else:                                        # defaults/regime：逐键（键集受校验器约束）
                d_sec[key] = {**d_sec[key], **{k: copy.deepcopy(v) for k, v in o_val.items()
                                               if k in d_sec[key]}}

        # ── beta.signals：id 为键同规则（规范序以 defaults 为准）──
        o_sigs = o_sec.get("signals")
        if eid == "beta" and isinstance(o_sigs, list) and o_sigs:
            o_by_id = {s["id"]: s for s in o_sigs if isinstance(s, dict) and s.get("id")}
            d_ids = {s["id"] for s in d_sec["signals"] if isinstance(s, dict) and s.get("id")}
            out = [copy.deepcopy(o_by_id[s["id"]]) if s.get("id") in o_by_id else dict(s)
                   for s in d_sec["signals"]]
            out += [{**copy.deepcopy(s), "orphan": True}          # 盘上独有信号 → orphan 保留
                    for sid, s in o_by_id.items() if sid not in d_ids]
            d_sec["signals"] = out

    merged["version"] = REGISTRY_VERSION
    # changelog 保留（审计链不断）：盘上 v2 changelog 接在 defaults 空表之后，FIFO 截断
    od_cl = on_disk.get("changelog") if is_v2 else None
    if isinstance(od_cl, list) and od_cl:
        merged["changelog"] = ((merged.get("changelog") or []) + od_cl)[-CHANGELOG_MAX:]
    merged["updated_at"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    return merged


# ── 变更应用器（T-R7）：指标增删改的唯一写入口（CLI/MCP 共用纯函数）──

CHANGELOG_MAX = 200        # changelog 上限（FIFO 截断，防注册表膨胀）


def apply_metric_change(reg: dict, engine_id: str, change: dict) -> tuple[dict, dict]:
    """指标全生命周期变更应用器（T-R7 纯函数：dict 进 (new_reg, record) 出，不改入参）。

    ``change = {"op": "add"|"update"|"remove"|"show", "id": "m2_yoy",
                "patch": {...},              # add: 完整条目；update: 部分字段
                "hypothesis_id": "H-0006",   # add/remove 必填；update 改 status 时必填
                "reason": "..."}``

    - add：formula 须 ``builtin:<id>`` 且 id ∈ KNOWN_FORMULAS（新公式需先在 factors.py
      实现）；条目 id/engine 以参数为准（代码所有，盘上不可篡改）；status 缺省 active；
    - update：仅 _METRIC_USER_KEYS 可改（formula/id/engine 属代码所有）；params/scorer
      深合并（部分更新语义），其余字段整值替换；version 自增；status 翻转须带
      hypothesis_id 并回写条目；
    - remove：**软删红线**——仅置 status="inactive"，条目与历史保留（重新启用走 update）；
    - show：只读返回条目（不落 changelog、不改注册表）。

    返回 ``(new_reg, change_record)``；除 show 外 record 追加至 ``new_reg["changelog"]``
    （{"ts", "op", "engine", "id", "before", "after", "hypothesis_id", "reason"}，
    超 CHANGELOG_MAX 条 FIFO 截断；ts 由系统生成，不可伪造）。任何校验失败
    （未知 id/引擎/公式/非法字段/缺 hypothesis_id/结果过不了 validate_registry）
    → ValueError（中文消息）；变更结果必过 ``validate_registry``（不产非法文件）。
    """
    if not isinstance(reg, dict) or not isinstance(reg.get("engines"), dict):
        raise ValueError("注册表结构不可识别（engines 节缺失）——先 `ap clone` 同步")  # noqa: TRY004 - ValueError 为契约语义
    if engine_id not in ("alpha", "beta", "macro"):
        raise ValueError(f"未知引擎：{engine_id}（可选 alpha/beta/macro）")
    op = change.get("op")
    if op not in ("add", "update", "remove", "show"):
        raise ValueError(f"未知操作：{op!r}（可选 add/update/remove/show）")
    mid = change.get("id")
    if not mid or not isinstance(mid, str):
        raise ValueError("缺少指标 id")
    hyp = change.get("hypothesis_id")
    reason = change.get("reason") or ""

    new_reg = copy.deepcopy(reg)
    metrics = new_reg["engines"][engine_id].setdefault("metrics", [])
    idx = next((i for i, m in enumerate(metrics) if isinstance(m, dict) and m.get("id") == mid), None)

    if op == "show":
        if idx is None:
            raise ValueError(f"指标不存在：{mid}（引擎 {engine_id}）")
        return new_reg, {"op": "show", "engine": engine_id, "id": mid,
                         "after": copy.deepcopy(metrics[idx])}

    if op == "add":
        if idx is not None:
            raise ValueError(f"指标已存在：{mid}（引擎 {engine_id}）；如需调整请用 update")
        if not hyp:
            raise ValueError("新增指标必须带 hypothesis_id（来源可溯红线：无来源不放行）")
        patch = change.get("patch") or {}
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是 dict（完整条目声明）")
        fml = patch.get("formula", "")
        if not (isinstance(fml, str) and fml.startswith("builtin:")):
            raise ValueError(f"formula 必须形如 builtin:<id>（实际 {fml!r}）")
        if fml.split(":", 1)[1] not in KNOWN_FORMULAS:
            raise ValueError(f"未知公式：{fml}——新公式需先在 factors.py 实现并登记 KNOWN_FORMULAS")
        entry = {"id": mid, "engine": engine_id, "params": {},
                 "status": "active", "version": 1, "hypothesis_id": hyp, "note": ""}
        entry.update(copy.deepcopy(patch))
        entry["id"], entry["engine"] = mid, engine_id            # 代码所有字段以参数为准
        before = None
        metrics.append(entry)

    elif op == "update":
        if idx is None:
            raise ValueError(f"指标不存在：{mid}（引擎 {engine_id}）；新增请用 add")
        patch = change.get("patch") or {}
        if not isinstance(patch, dict) or not patch:
            raise ValueError("update 需要非空 patch（部分字段）")
        bad = set(patch) - set(_METRIC_USER_KEYS)
        if bad:
            raise ValueError(f"非法字段：{sorted(bad)}"
                             f"（可改 {list(_METRIC_USER_KEYS)}；formula/id/engine 属代码所有）")
        entry = metrics[idx]
        before = copy.deepcopy(entry)
        st_flip = "status" in patch and patch["status"] != entry.get("status", "active")
        if st_flip and not hyp:
            raise ValueError(f"指标 {mid} 启停（status）变更必须带 hypothesis_id（无来源不放行）")
        for k, v in patch.items():                               # params/scorer 深合并，其余整值
            if k in ("params", "scorer") and isinstance(v, dict) and isinstance(entry.get(k), dict):
                merged_kv = dict(entry[k])
                merged_kv.update(copy.deepcopy(v))
                entry[k] = merged_kv
            else:
                entry[k] = copy.deepcopy(v)
        if st_flip:
            entry["hypothesis_id"] = hyp
        entry["version"] = int(entry.get("version", 1)) + 1

    else:                                                        # remove：软删红线
        if idx is None:
            raise ValueError(f"指标不存在：{mid}（引擎 {engine_id}）")
        if not hyp:
            raise ValueError("移除指标必须带 hypothesis_id（来源可溯红线）")
        entry = metrics[idx]
        before = copy.deepcopy(entry)
        entry["status"] = "inactive"                             # 条目与历史保留，可 update 复活

    errs = validate_registry(new_reg)
    if errs:
        raise ValueError("变更未通过注册表校验：" + "；".join(errs[:3]))

    after = copy.deepcopy(entry)
    rec = {"ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
           "op": op, "engine": engine_id, "id": mid, "before": before, "after": after,
           "hypothesis_id": hyp, "reason": reason}
    cl = new_reg.get("changelog")
    if not isinstance(cl, list):
        cl = new_reg["changelog"] = []
    cl.append(rec)
    if len(cl) > CHANGELOG_MAX:
        new_reg["changelog"] = cl[-CHANGELOG_MAX:]
    return new_reg, rec
