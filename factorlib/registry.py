"""注册中心（实施计划 §4 任务 1.1 / 1.5）。

- 因子定义即代码：definitions/ 下一个因子一个 YAML，Git 管理；
- FactorMeta：架构文档 §15 的最小实现（M5 升级为 hypothesis_id 主键 + health 字段）；
- 状态机：draft → active → deprecated，单向，禁止跳级与回退；
- 注册校验：id 唯一且带族前缀、引擎必须已注册、依赖表非空、声明体与引擎匹配；
- 依赖解析与 lookback：resolve_start 给出回测起点 = max(lookback_days) 满足日。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml

# ── 状态机（M1 单向；M5 接管后扩版本冻结与门禁） ─────────────
STATUS_DRAFT, STATUS_ACTIVE, STATUS_DEPRECATED = "draft", "active", "deprecated"
VALID_TRANSITIONS = {STATUS_DRAFT: {STATUS_ACTIVE}, STATUS_ACTIVE: {STATUS_DEPRECATED},
                     STATUS_DEPRECATED: set()}

# 族前缀（id 纪律：{family 缩写}_{口径}，过滤因子 flt_ 不参与打分）
FAMILY_PREFIX = {"valuation": "val", "quality": "qua", "growth": "gro",
                 "momentum": "mom", "lowvol": "low", "filter": "flt"}

# 引擎与声明体匹配：sql→sql 字段、python_udf→fn、qlib_expr→expr
_ENGINE_BODY = {"sql": "sql", "python_udf": "fn", "qlib_expr": "expr"}


class RegistryError(ValueError):
    """注册/状态迁移违例（M1：失败即拒，无静默降级）。"""


@dataclass
class FactorMeta:
    """因子元数据（YAML 落库前的内存态；字段集 = 架构文档 §15 最小集）。"""

    id: str
    name: str
    family: str
    type: str                    # alpha | filter（filter 不参与打分，M2 flt_* 用）
    status: str                  # draft | active | deprecated
    version: str                 # 语义化版本；口径变更 = 新 version（M5 冻结）
    universe: list               # ["CN"]（M7 扩 HK/US）
    engine: str                  # sql | python_udf | qlib_expr
    lookback_days: int           # 依赖回看天数（回测起点推导）
    depends: list                # 依赖数据表（注册时声明，执行前校验存在）
    direction: int               # +1 正向 / -1 负向（打分方向；M1 仅登记）
    hypothesis_id: str | None    # 研究假设来源（M1 允许 None；M5 激活门禁强制）
    transform: list              # 预处理链（winsorize/neutralize/zscore/rank）
    body: str                    # 声明体：SQL 文本 / UDF 函数名 / qlib 表达式
    source: str = ""             # YAML 相对路径（可追溯）
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


_REQUIRED = ("id", "name", "family", "engine", "depends")
_ALLOWED_KEYS = {f for f in FactorMeta.__dataclass_fields__} | {"sql", "fn", "expr", "params"}


def _parse_factor(doc: dict, source: str) -> FactorMeta:
    missing = [k for k in _REQUIRED if not doc.get(k)]
    if missing:
        raise RegistryError(f"{source}: 缺少必填字段 {missing}")
    unknown = set(doc) - _ALLOWED_KEYS
    if unknown:
        raise RegistryError(f"{source}: 未声明字段 {sorted(unknown)}")
    if doc["family"] not in FAMILY_PREFIX:
        raise RegistryError(f"{source}: 未知 family {doc['family']}（可选 {sorted(FAMILY_PREFIX)}）")
    prefix = FAMILY_PREFIX[doc["family"]]
    if not str(doc["id"]).startswith(prefix + "_"):
        raise RegistryError(f"{source}: id 须以族前缀 {prefix}_ 开头，实得 {doc['id']}")
    engine = doc["engine"]
    if engine not in _ENGINE_BODY:
        raise RegistryError(f"{source}: 未知引擎 {engine}（可选 {sorted(_ENGINE_BODY)}）")
    body_key = _ENGINE_BODY[engine]
    body = doc.get(body_key)
    if not body or not str(body).strip():
        raise RegistryError(f"{source}: engine={engine} 须提供非空 {body_key} 字段")
    status = doc.get("status", STATUS_DRAFT)
    if status != STATUS_DRAFT:
        raise RegistryError(f"{source}: 注册初始状态必须 draft（实得 {status}）")
    lookback = int(doc.get("lookback_days") or 0)
    if lookback < 0:
        raise RegistryError(f"{source}: lookback_days 不可为负")
    direction = int(doc.get("direction", 1))
    if direction not in (1, -1, 0):
        raise RegistryError(f"{source}: direction 须为 ±1 或 0")
    if direction == 0 and doc.get("type") != "filter":
        raise RegistryError(f"{source}: direction=0（不打分）仅限过滤因子 type=filter")
    return FactorMeta(
        id=doc["id"], name=doc["name"], family=doc["family"],
        type=doc.get("type", "alpha"), status=STATUS_DRAFT,
        version=str(doc.get("version", "1.0.0")), universe=list(doc.get("universe", ["CN"])),
        engine=engine, lookback_days=lookback, depends=list(doc["depends"]),
        direction=direction, hypothesis_id=doc.get("hypothesis_id"),
        transform=list(doc.get("transform") or []), body=str(body),
        source=source, params=dict(doc.get("params") or {}),
    )


def load_definitions(root: str | Path) -> list[FactorMeta]:
    """加载 definitions/ 下全部 YAML（递归），校验后按 id 排序（检查点锚：registry 按 id 排序）。"""
    root = Path(root)
    metas: dict[str, FactorMeta] = {}
    for p in sorted(root.glob("definitions/**/*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise RegistryError(f"{p}: YAML 顶层须为映射")
        meta = _parse_factor(doc, source=str(p.relative_to(root)))
        if meta.id in metas:
            raise RegistryError(f"因子 id 重复：{meta.id}（{p} 与 {metas[meta.id].source}）")
        metas[meta.id] = meta
    return sorted(metas.values(), key=lambda m: m.id)


def set_status(meta: FactorMeta, new_status: str) -> FactorMeta:
    """状态迁移（封闭集合，无旁路；M1 纪律：非法迁移直接拒绝）。"""
    if new_status not in VALID_TRANSITIONS:
        raise RegistryError(f"未知状态 {new_status!r}")
    if new_status not in VALID_TRANSITIONS[meta.status]:
        raise RegistryError(f"{meta.id}: 非法迁移 {meta.status} → {new_status}"
                            f"（合法：{sorted(VALID_TRANSITIONS[meta.status]) or '无（终态）'}）")
    meta.status = new_status
    return meta


def resolve_start(metas: list[FactorMeta], trade_days: list[str]) -> str | None:
    """依赖解析与 lookback 校验（任务 1.5）。

    回测起点 = max(lookback_days) 满足日：起点前须有足量 lookback 交易日供计算窗口。
    trade_days 为升序交易日列表；数据不足时显式报错（不静默截断）。
    """
    if not metas:
        return None
    need = max(m.lookback_days for m in metas)
    if need == 0:
        return trade_days[0] if trade_days else None
    if len(trade_days) <= need:
        raise RegistryError(f"交易日不足：lookback 需 {need} 日，实得 {len(trade_days)} 日")
    return trade_days[need]
