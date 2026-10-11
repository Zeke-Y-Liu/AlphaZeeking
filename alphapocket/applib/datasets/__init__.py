"""DatasetSpec 声明式数据集规格（组件化 T-R5）：新增数据集只写声明、不写代码。

五要素（schema 冻结，字段改动须走契约修订）：
1. ``path``        落盘位置（相对 repo）
2. ``merge_policy`` 合并策略名（T-R7 收敛后的单一机制引用；T-R5 冻结枚举集合）
3. ``fetch_params`` 入参（拉取参数 / 频率 / 窗口）
4. ``sources``     多源（主源 / 备源 / 交叉源，降级序 = 列表序）
5. ``validation``  验证联动（data_missing 检测声明，T-R10 消费）

红线（T-R5）：
- **单点声明**：数据集定义只允许出现在 datasets/ 声明中，第二实现=违规
  （fetch_all 内联定义 → T-R6 逐项物理迁移，见模块尾迁移清单）。
- **无第二实现**：merge_policy 一律委托既有实现（writeguard.merge_* 等），
  本模块不复制任何合并/拉取逻辑。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import writeguard

__all__ = ["KNOWN_MERGE_POLICIES", "MERGE_POLICY_FN", "SourceSpec", "DatasetSpec",
           "validate_spec", "load_specs", "merge_by_policy", "persist_spec",
           "PENDING_MIGRATION", "registry_specs", "load_registry_raw",
           "carry_old_rows", "carry_old_merge"]


@dataclass(frozen=True)
class SourceSpec:
    """多源降级序单元：列表序即降级序。"""
    interface: str              # 上游接口表达式（如 ak.stock_zh_a_daily）
    upstream: str               # 来源机构名（provenance 登记口径）
    role: str = "primary"       # primary / fallback / crosscheck
    params: dict = field(default_factory=dict)   # 接口级参数覆写


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str             # 数据集标识（如 dwd_daily_bar）
    path: str                   # ① 落盘位置（相对 repo，禁止绝对路径/..）
    merge_policy: str           # ② 合并策略（KNOWN_MERGE_POLICIES 内引用）
    fetch_params: dict = field(default_factory=dict)   # ③ 入参（拉取参数/频率/窗口）
    sources: tuple[SourceSpec, ...] = ()               # ④ 多源降级序
    validation: dict = field(default_factory=dict)     # ⑤ 验证联动（T-R10 消费）


# 合并策略枚举（T-R5 冻结；T-R7 收敛为注册表后此处降级为引用）
KNOWN_MERGE_POLICIES = ("overwrite", "union_bars", "carry_old",
                        "registry_merge", "portfolio_merge")

# 策略→实现委托（零第二实现：全部复用既有函数；未注册策略显式拒绝）
MERGE_POLICY_FN = {
    "union_bars": writeguard.merge_bars,            # (old, new) -> (merged, info)
    "registry_merge": lambda old, new: (writeguard.merge_registry(old, new), {}),
    "portfolio_merge": lambda old, new: (writeguard.merge_portfolio(old, new), {}),
    # "carry_old" ← datasource.fetch_macro_liquidity 内联逻辑，T-R6 物理迁移后注册
}

_VALID_ROLES = ("primary", "fallback", "crosscheck")


# ── 校验器（非法 spec 拒绝，错误信息含字段定位）───────────────────────────
def validate_spec(spec: DatasetSpec) -> None:
    """逐字段校验 → 非法即 raise ``DatasetSpec.<field>: 原因``（可定位）。"""
    if not spec.dataset_id or not isinstance(spec.dataset_id, str):
        raise ValueError(f"DatasetSpec.dataset_id: 须为非空字符串，得到 {spec.dataset_id!r}")
    if not spec.path or not isinstance(spec.path, str):
        raise ValueError(f"DatasetSpec.path: 须为非空字符串，得到 {spec.path!r}")
    if spec.path.startswith("/") or ".." in Path(spec.path).parts:
        raise ValueError(f"DatasetSpec.path: 须为相对 repo 路径（禁止绝对路径/..），得到 {spec.path!r}")
    if not spec.path.endswith(".json"):
        raise ValueError(f"DatasetSpec.path: 落盘产物仅支持 .json，得到 {spec.path!r}")
    if spec.merge_policy not in KNOWN_MERGE_POLICIES:
        raise ValueError(f"DatasetSpec.merge_policy: {spec.merge_policy!r} 不在冻结枚举 "
                         f"{KNOWN_MERGE_POLICIES}（引用 T-R7 单一机制，不得私造）")
    if not isinstance(spec.fetch_params, dict):
        raise ValueError(f"DatasetSpec.fetch_params: 须为 dict，得到 {type(spec.fetch_params).__name__}")
    if not isinstance(spec.sources, tuple) or not spec.sources:
        raise ValueError(f"DatasetSpec.sources: 须为非空 SourceSpec 元组（多源降级序），得到 {spec.sources!r}")
    for i, s in enumerate(spec.sources):
        if not isinstance(s, SourceSpec):
            raise ValueError(f"DatasetSpec.sources[{i}]: 须为 SourceSpec，得到 {type(s).__name__}")
        if not s.interface or not s.upstream:
            raise ValueError(f"DatasetSpec.sources[{i}]: interface/upstream 须为非空字符串"
                             f"（provenance 口径），得到 interface={s.interface!r} upstream={s.upstream!r}")
        if s.role not in _VALID_ROLES:
            raise ValueError(f"DatasetSpec.sources[{i}].role: {s.role!r} 不在 {_VALID_ROLES}")
    v = spec.validation
    if not isinstance(v, dict):
        raise ValueError(f"DatasetSpec.validation: 须为 dict（data_missing 检测声明），得到 {type(v).__name__}")
    if "min_rows" in v and (not isinstance(v["min_rows"], int) or v["min_rows"] < 0):
        raise ValueError(f"DatasetSpec.validation.min_rows: 须为非负 int，得到 {v['min_rows']!r}")
    if "required_keys" in v and (not isinstance(v["required_keys"], list)
                                 or not all(isinstance(k, str) for k in v["required_keys"])):
        raise ValueError(f"DatasetSpec.validation.required_keys: 须为字符串列表，得到 {v['required_keys']!r}")
    if "as_of_field" in v and not isinstance(v["as_of_field"], str):
        raise ValueError(f"DatasetSpec.validation.as_of_field: 须为字符串，得到 {v['as_of_field']!r}")


def load_specs(path: str | Path) -> dict[str, DatasetSpec]:
    """JSON 声明文件 → {dataset_id: DatasetSpec}（非法条目拒绝并定位 dataset）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"DatasetSpec 文件顶层须为 {{dataset_id: ...}}：{path}")
    out = {}
    for ds_id, raw in data.items():
        try:
            sources = tuple(SourceSpec(**s) for s in raw.get("sources", []))
            spec = DatasetSpec(dataset_id=ds_id, path=raw.get("path"),
                               merge_policy=raw.get("merge_policy"),
                               fetch_params=raw.get("fetch_params", {}),
                               sources=sources, validation=raw.get("validation", {}))
            validate_spec(spec)
        except (TypeError, ValueError) as e:
            raise ValueError(f"DatasetSpec[{ds_id}]: {e}") from e
        out[ds_id] = spec
    return out


# ── spec→fetch 驱动通道（T-R6 存量迁移后 fetch_all 按 spec 驱动）───────────
def merge_by_policy(spec: DatasetSpec, new, old=None):
    """按 spec.merge_policy 委托既有实现（零第二实现）；未注册策略显式拒绝。"""
    fn = MERGE_POLICY_FN.get(spec.merge_policy)
    if fn is None:
        raise NotImplementedError(
            f"DatasetSpec[{spec.dataset_id}].merge_policy={spec.merge_policy!r} "
            f"实现未注册（T-R6 收编存量定义后注册；当前拒绝私造第二实现）")
    return fn(old or {}, new or {})


def persist_spec(repo: Path, spec: DatasetSpec, payload) -> Path:
    """落盘 spec.path（相对 repo；目录自动创建）。"""
    p = repo / spec.path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── T-R6 存量迁移：单点声明文件（datasets/registry.json，15 项逐字迁移）─────
_REGISTRY_FILE = Path(__file__).resolve().parent / "registry.json"
_SPECS_CACHE: dict[str, DatasetSpec] | None = None


def load_registry_raw() -> dict:
    """声明文件原样加载（含 macro indicators 等扩展键；load_specs 忽略的键保留）。"""
    return json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))


def registry_specs() -> dict[str, DatasetSpec]:
    """全部数据集 spec（单点声明 + schema 校验；缓存，T-R6 起唯一数据集定义来源）。"""
    global _SPECS_CACHE
    if _SPECS_CACHE is None:
        _SPECS_CACHE = load_specs(_REGISTRY_FILE)
    return _SPECS_CACHE


# ── carry_old 合并策略（T-R6 从 datasource.fetch_macro_liquidity 内联逻辑物理迁移；
#    单点实现，datasource 与 MERGE_POLICY_FN 共同引用，零第二实现）────────────────
def carry_old_rows(old_rows: list, new_rows: list) -> list:
    """行级 union 内核：旧点全保留；同 dt 新值覆盖=点位修正；永不物理删除已落盘点。"""
    merged: dict = {r["dt"]: r["value"] for r in (old_rows or []) if r.get("dt")}
    merged.update({r["dt"]: r["value"] for r in (new_rows or []) if r.get("dt")})
    return [{"dt": d, "value": merged[d]} for d in sorted(merged)]


def carry_old_merge(old: dict, new: dict) -> tuple[dict, dict]:
    """payload 级 carry_old（MERGE_POLICY_FN 契约 ``(old, new) -> (merged, info)``）。

    ``new.indicators`` 逐指标：status=ok → 行级 union（first/last/rows 以合并后为准）；
    status≠ok（拉取失败占位）→ 旧指标序列完整保留并标 carried_old（旧数据零丢失）；
    无旧可保 → 占位原样（unavailable 语义，不虚报）。
    """
    old_ind = (old or {}).get("indicators", {})
    new_ind = (new or {}).get("indicators", {})
    out, info = {}, {"carried": [], "unioned": []}
    for sid, nd in new_ind.items():
        od = old_ind.get(sid) or {}
        if nd.get("status") == "ok":
            series = carry_old_rows(od.get("series"), nd.get("series"))
            out[sid] = {**nd, "series": series, "rows": len(series),
                        "first_dt": series[0]["dt"] if series else None,
                        "last_dt": series[-1]["dt"] if series else None}
            info["unioned"].append(sid)
        elif od.get("series"):                       # 失败但有旧数据 → carry_old（红线 4）
            osr = od["series"]
            out[sid] = {**nd, "status": "carried_old", "series": osr,
                        "rows": len(osr), "first_dt": osr[0]["dt"], "last_dt": osr[-1]["dt"]}
            info["carried"].append(sid)
        else:                                        # 失败且无旧 → 透明 unavailable
            out[sid] = nd
    info["carried"].sort()
    info["unioned"].sort()
    merged = {**new, "indicators": out, "_merge": info}
    return merged, info


MERGE_POLICY_FN["carry_old"] = carry_old_merge


# ── T-R6 迁移审计记录（2026-10-11 全部 15 项迁移完成并销账，保留为审计轨迹）──
# 单点声明 = datasets/registry.json；fetch_all/_macro 已全部改为 spec 驱动，
# 等价性实测：provenance 的 dataset/interface/upstream/file 逐字节零差异。
MIGRATION_LOG: tuple[dict, ...] = (
    {"dataset_id": "dwd_trade_calendar", "path": "market/trade_calendar.json", "src": "fetch_all ①"},
    {"dataset_id": "dwd_daily_bar", "path": "market/daily_bars.json", "src": "fetch_all ②"},
    {"dataset_id": "dwd_daily_bar_raw", "path": "market/daily_bars_raw.json", "src": "fetch_all ②"},
    {"dataset_id": "cross_check_tx", "path": "market/cross_check_tx.json", "src": "fetch_all ②"},
    {"dataset_id": "adjust_ex_dates", "path": "market/adjust_ex_dates.json", "src": "fetch_all ②'"},
    {"dataset_id": "dwd_financial_report", "path": "factors/fundamental_values.json", "src": "fetch_all ②"},
    {"dataset_id": "dim_ggt_components", "path": "market/ggt_components.json", "src": "fetch_all ③"},
    {"dataset_id": "dim_market_cap", "path": "dim/market_cap.json", "src": "fetch_all ③"},
    {"dataset_id": "dwd_trade_calendar_mkt", "path": "market/trade_calendar_*.json", "src": "fetch_all ③"},
    {"dataset_id": "dim_security", "path": "dim/security.json", "src": "fetch_all ③（seed 同构）"},
    {"dataset_id": "portfolio_cn4", "path": "portfolio/cn4.json", "src": "fetch_all ④"},
    {"dataset_id": "portfolio_multi5", "path": "portfolio/multi5.json", "src": "fetch_all ④"},
    {"dataset_id": "factor_registry", "path": "factors/registry.json", "src": "fetch_all ④"},
    {"dataset_id": "market_fx", "path": "market/fx.json", "src": "fetch_all ④（seed 同）"},
    {"dataset_id": "macro_liquidity", "path": "market/macro_liquidity.json", "src": "fetch_macro_liquidity（T-C1，carry_old→datasets 单点）"},
)
# 不迁项（离线 mock 种子初始化，非拉取数据集）：seed.py（fx/security/registry/
# fundamental_values/portfolio×3/signals/strategy 等 10 处）——豁免登记保持有效。
# 兼容别名（T-R5 引入名，T-R7 收敛时清理）：
PENDING_MIGRATION = MIGRATION_LOG
