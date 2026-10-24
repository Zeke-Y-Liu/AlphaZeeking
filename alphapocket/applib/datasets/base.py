"""DatasetSpec 数据集声明基座（T-R9）：数据维度声明化的唯一登记面。

纪律：
- **声明即文档**：每个数据集一条 spec（路径/合并策略/来源/验证关联/入库表），
  fetch_all 的快照合并面（_MERGEABLE）由 active_specs() 派生——新增数据集只改声明；
- 行为零变化：派生后的 _MERGEABLE 与改造前硬编码逐项相等（tests 对拍锚定）；
- portfolio/cn4|multi5 不登记（组合域，materialize_from_pg 唯一写方，现状红线）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# 增量合并策略（与 fetch_all 增量段的实际行为一一对应）：
#   replace=不可合并（全量重写）；bars_union=按 dt 并集（重叠日新胜 + 首根 pct 边界修复）；
#   new_nonempty=新非空用新否则携带旧（滚动窗口）；set_union=去重并集；
#   fill_missing=仅补缺失 secid（已有标的全量重写不降级）；carry_old=失败/空窗口携带旧值；
#   registry_merge=T-R6 专用：factors/registry.json 经 merge_registry 写保护合并
#   （用户改动优先/orphan 停车场），不走快照合并面。
MERGE_POLICIES = ("replace", "bars_union", "new_nonempty", "set_union",
                  "fill_missing", "carry_old", "registry_merge")

CATEGORIES = ("market", "dim", "factors", "portfolio")
ROLES = ("primary", "cross", "reconcile", "fallback")

# 不进入快照合并面的策略（replace=全量重写；registry_merge 有 T-R6 专用写入口）
NON_SNAPSHOT_POLICIES = ("replace", "registry_merge")

# 六项验证（runner.VERIFY_SUITE）的检查名——verifications 字段的合法取值域
VERIFICATION_NAMES = ("pit_audit", "golden_test", "cross_source",
                      "calendar_check", "adjust_check", "provenance_check")


@dataclass
class SourceRef:
    """数据集来源登记（多源路由声明：primary/cross/reconcile/fallback，T-R11 填实）。"""
    id: str
    role: str                 # primary|cross|reconcile|fallback
    interface: str            # akshare/HTTP 接口表达式（与 provenance 口径一致）
    note: str = ""            # 降级/口径说明（fallback 必须写明触发条件与豁免依据）
    upstream: str = ""        # 数据机构（provenance upstream 登记口径；仅 primary 消费）


@dataclass
class DatasetSpec:
    """数据集声明（一条 spec = 一个 repo 内数据文件的完整档案）。"""
    name: str                    # 数据集名（_reg 的 dataset 参数 / registry 的 dataset 字段）
    path: str                    # repo 相对路径
    category: str                # market|dim|factors|portfolio
    merge_policy: str            # MERGE_POLICIES 之一；replace=不可合并（全量重写）
    always: bool                 # 恒拉取 vs 条件（universe 含港美股等）
    duckdb_table: str | None     # data.py 建表名；None=不入库（多表用 + 连接）
    verifications: list[str]     # 六项验证关联子集（VERIFICATION_NAMES）
    sources: list[SourceRef]
    status: str = "active"       # 数据集软删（inactive=fetch 跳过 + 报告登记）
    note: str = ""

    def __post_init__(self):
        if self.merge_policy not in MERGE_POLICIES:
            raise ValueError(f"{self.name}: merge_policy 非法 {self.merge_policy!r}（可选 {MERGE_POLICIES}）")
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.name}: category 非法 {self.category!r}（可选 {CATEGORIES}）")
        if not self.path:
            raise ValueError(f"{self.name}: path 不能为空")
        for s in self.sources:
            if s.role not in ROLES:
                raise ValueError(f"{self.name}: 来源 {s.id} 的 role 非法 {s.role!r}（可选 {ROLES}）")

    def to_dict(self) -> dict:
        """JSON 可序列化 dict（round-trip：DatasetSpec.from_dict(spec.to_dict()) == spec）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DatasetSpec:
        d = dict(d)
        d["sources"] = [SourceRef(**s) if isinstance(s, dict) else s
                        for s in d.get("sources", [])]
        return cls(**d)
