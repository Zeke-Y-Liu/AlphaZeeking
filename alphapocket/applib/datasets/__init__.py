"""数据集注册表（T-R9）：全部数据集的声明式登记与派生入口。

用法（datasource 的快照合并面由此派生，行为与改造前逐项相等）：

    from .datasets import NON_SNAPSHOT_POLICIES, active_specs
    _MERGEABLE = {s.name: s.path for s in active_specs()
                  if s.merge_policy not in NON_SNAPSHOT_POLICIES}

新增数据集的标准路径（维度增能力）：声明一条 DatasetSpec（specs_*.py 或
register_spec 运行时登记）→ active_specs() 自动并入 → _MERGEABLE 自动接管。
"""
from __future__ import annotations

from . import specs_dim, specs_factors, specs_market
from .base import (
    CATEGORIES as CATEGORIES,
)
from .base import (
    MERGE_POLICIES as MERGE_POLICIES,
)
from .base import (
    NON_SNAPSHOT_POLICIES as NON_SNAPSHOT_POLICIES,
)
from .base import (
    ROLES as ROLES,
)
from .base import (
    VERIFICATION_NAMES as VERIFICATION_NAMES,
)
from .base import (
    DatasetSpec,
)
from .base import (
    SourceRef as SourceRef,
)

_EXTRA: list[DatasetSpec] = []       # 运行时登记的自定义数据集（register_spec）


def all_specs() -> list[DatasetSpec]:
    """全量 spec（内置声明 + 运行时登记；顺序：market → dim → factors → extra）。"""
    return [*specs_market.SPECS, *specs_dim.SPECS, *specs_factors.SPECS, *_EXTRA]


def active_specs() -> list[DatasetSpec]:
    """status == active 的 spec 子集（inactive=软删：fetch 跳过 + 报告登记）。"""
    return [s for s in all_specs() if s.status == "active"]


def get_spec(name: str) -> DatasetSpec:
    """按名取 spec；未知名 → KeyError（中文消息）。"""
    for s in all_specs():
        if s.name == name:
            return s
    raise KeyError(f"未知数据集：{name}（注册清单共 {len(all_specs())} 条）")


def get_spec_safe(name: str, path: str | None = None) -> DatasetSpec | None:
    """按名（未命中再按 repo 相对路径）查 spec；未注册 → None（透明降级，不抛）。

    T-R11：_reg 的 provenance 登记口径查询口——_reg 的 dataset 参数是溯源名
    （如 dwd_daily_bar），与 spec.name（daily_bars）不同构，故按 path 兜底；
    未注册数据集走手工登记（WARNING），绝不因注册表缺项阻断同步。
    """
    for s in all_specs():
        if s.name == name or (path is not None and s.path == path):
            return s
    return None


def spec_primary_interface(spec: DatasetSpec) -> str | None:
    """spec 主源的 interface（provenance interface 登记口径；无 primary → None）。"""
    for s in spec.sources:
        if s.role == "primary":
            return s.interface
    return None


def spec_primary_upstream(spec: DatasetSpec) -> str | None:
    """spec 主源的 upstream（数据机构；未填 → None，调用方需手工传）。"""
    for s in spec.sources:
        if s.role == "primary":
            return s.upstream or None
    return None


def register_spec(spec: DatasetSpec) -> None:
    """运行时登记自定义数据集（重复 name → ValueError；结果必过 DatasetSpec 校验）。"""
    if any(s.name == spec.name for s in all_specs()):
        raise ValueError(f"数据集已登记：{spec.name}（如需调整请改声明）")
    _EXTRA.append(spec)
