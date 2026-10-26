"""引擎指标注册表端点（配置即数据：因子增删改查 + 数据集联动）。

复用 applib MCP 工具 ``engine.metrics.list/add/update/remove``（注册表 v2 +
changelog + 门禁校验），REST 层只做参数透传与响应包装：

- ``GET  /engines/metrics?engine=&status=``：指标清单（含 changelog 计数）；
- ``GET  /engines/describe``：四引擎自描述（指标数/活跃数/维度权重）；
- ``POST /engines/metrics``：新增指标（hypothesis_id 必填）；
- ``PATCH /engines/metrics/{mid}``：修改（部分字段深合并；启停需 hypothesis_id）；
- ``DELETE /engines/metrics/{mid}``：软删（status=inactive，条目保留）；
- ``GET  /engines/datasets``：指标引用的数据集清单 + 落盘状态 + as_of
  （新增因子数据缺失提示与数据板块「引擎数据集」卡片共用此端点）。

数据缺失透明提示（不阻断）：新增/修改携带 dataset 而对应
``sandbox/repo/market/<dataset>.json`` 缺失 → 响应附 ``data_missing``
（含数据集名与提示文案），前端引导前往数据页触发同步。
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from applib import tools
from applib.ctx import Ctx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode

router = APIRouter(prefix="/engines", tags=["engines"])

# 合法引擎 id（与 applib.engines.base 注册表四键一致；引擎D 指数趋势 2026-10-26 增补）
_ENGINES = ("alpha", "beta", "macro", "index_trend")


def _ctx(settings: Annotated[Settings, Depends(get_settings)]) -> Ctx:
    """构造 workspace Ctx（ensure_sandbox 幂等，保证 repo 目录就位）。"""
    ctx = Ctx(root=settings.workspace_path)
    ctx.ensure_sandbox()
    return ctx


def _unwrap(res: dict[str, Any]) -> dict[str, Any]:
    """MCP 工具错误 → 422 VALIDATION_ERROR（门禁拒绝语义）；成功原样透传。"""
    if "error" in res:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(res["error"]))
    return res


# ── 数据缺失检测（需求：新增因子数据缺失 → 提示获取，数据板块同步展示）──


def _dataset_state(ctx: Ctx, dataset: str | None) -> dict[str, Any] | None:
    """单数据集落盘状态：文件存在性 + as_of + series 数（缺失返回 missing 形态）。

    仅检查指标显式声明的 dataset（macro 引擎）；alpha/beta 的量价财务面
    由既有 daily_bars / dws 管道覆盖，dataset 为 None 不在此列。
    """
    if not dataset:
        return None
    path = ctx.local_repo / "market" / f"{dataset}.json"
    if not path.exists():
        return {"dataset": dataset, "synced": False, "as_of": None, "series": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        series = data.get("series") if isinstance(data, dict) else None
        if isinstance(series, dict):
            n_series = len(series)
        elif isinstance(data, dict) and data and all(isinstance(v, list) for v in data.values()):
            n_series = len(data)     # 指数/清单形态（如 index_bars：{指数id: [bars]}，无 series 键）
        else:
            n_series = 0
        return {
            "dataset": dataset,
            "synced": True,
            "as_of": data.get("as_of") if isinstance(data, dict) else None,
            "series": n_series,
        }
    except (OSError, ValueError):
        # 文件损坏视同未同步（透明降级，不抛异常）
        return {"dataset": dataset, "synced": False, "as_of": None, "series": 0}


def _data_missing_hint(ctx: Ctx, datasets: list[str | None]) -> list[dict[str, Any]]:
    """对声明数据集逐个检测缺失，仅返回未同步条目（附引导提示）。"""
    out = []
    for ds in datasets:
        state = _dataset_state(ctx, ds)
        if state and not state["synced"]:
            out.append({
                **state,
                "hint": f"数据集 {ds} 尚未同步，请前往数据页触发同步（incremental）后再评估",
            })
    return out


# ── 查询面 ────────────────────────────────────────────────


@router.get("/metrics")
def list_metrics(
    ctx: Annotated[Ctx, Depends(_ctx)],
    engine: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """指标清单（按引擎/状态过滤；含 changelog 计数）。"""
    return _unwrap(tools.call_tool(
        "engine.metrics.list", {"engine": engine, "status": status}, ctx))


@router.get("/describe")
def describe_engines(ctx: Annotated[Ctx, Depends(_ctx)]) -> dict[str, Any]:
    """四引擎自描述（id/name/指标数/活跃数/维度权重）。"""
    from applib.engines import alpha, beta, index_trend, macro  # noqa: PLC0415 - 延迟导入

    return {"engines": [
        alpha.AlphaEngine().load(ctx.local_repo).describe(),
        beta.BetaEngine().load(ctx.local_repo).describe(),
        macro.MacroEngine().load(ctx.local_repo).describe(),
        index_trend.IndexTrendEngine().load(ctx.local_repo).describe(),
    ]}


@router.get("/datasets")
def list_datasets(ctx: Annotated[Ctx, Depends(_ctx)]) -> dict[str, Any]:
    """指标引用的数据集清单 + 落盘状态（数据板块「引擎数据集」卡片数据源）。

    汇总所有 active 指标声明的 dataset（去重），附 synced/as_of/series 与
    引用该数据集的指标 id 列表；未同步条目带 hint 引导同步。
    """
    res = _unwrap(tools.call_tool("engine.metrics.list", {}, ctx))
    refs: dict[str, list[str]] = {}
    for m in res.get("metrics", []):
        ds = m.get("dataset")
        if ds and m.get("status", "active") == "active":
            refs.setdefault(ds, []).append(m["id"])
    items = []
    for ds in sorted(refs):
        state = _dataset_state(ctx, ds)
        if state is None:
            continue
        item = {**state, "metrics": sorted(refs[ds])}
        if not state["synced"]:
            item["hint"] = f"数据集 {ds} 尚未同步，请前往数据页触发同步（incremental）"
        items.append(item)
    return {"datasets": items, "missing": sum(1 for i in items if not i["synced"])}


# ── 变更面（增/改/删）────────────────────────────────────


class MetricSpecIn(BaseModel):
    """新增指标完整声明（formula 须 builtin 清单；hypothesis_id 必填）。"""

    id: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    dim: str | None = None
    market: str | None = None
    dataset: str | None = None
    scorer: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    note: str = ""


class MetricAddIn(BaseModel):
    """新增入参：engine + spec + 门禁字段。"""

    engine: str
    spec: MetricSpecIn
    hypothesis_id: str = Field(min_length=1)
    reason: str = ""


class MetricUpdateIn(BaseModel):
    """修改入参：patch（部分字段）+ 门禁字段（status 翻转需 hypothesis_id）。"""

    patch: dict[str, Any] = Field(min_length=1)
    hypothesis_id: str | None = None
    reason: str = ""
    engine: str | None = None


class MetricRemoveIn(BaseModel):
    """软删入参（条目保留 status=inactive；可 update 复活）。"""

    hypothesis_id: str = Field(min_length=1)
    reason: str = ""
    engine: str | None = None


@router.post("/metrics", status_code=201)
def add_metric(payload: MetricAddIn, ctx: Annotated[Ctx, Depends(_ctx)]) -> dict[str, Any]:
    """新增指标；声明数据集未落盘 → 响应附 data_missing 提示（不阻断）。"""
    if payload.engine not in _ENGINES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知 engine：{payload.engine!r}",
            detail={"engine": payload.engine, "allowed": list(_ENGINES)},
        )
    spec = payload.spec.model_dump()
    spec["hypothesis_id"] = payload.hypothesis_id
    res = _unwrap(tools.call_tool("engine.metrics.add", {
        "engine": payload.engine, "spec": spec,
        "hypothesis_id": payload.hypothesis_id, "reason": payload.reason,
    }, ctx))
    missing = _data_missing_hint(ctx, [payload.spec.dataset])
    if missing:
        res["data_missing"] = missing
    return res


@router.patch("/metrics/{mid}")
def update_metric(
    mid: str,
    payload: MetricUpdateIn,
    ctx: Annotated[Ctx, Depends(_ctx)],
) -> dict[str, Any]:
    """修改指标（params/scorer 深合并；version 自增）；数据集变更附缺失提示。"""
    res = _unwrap(tools.call_tool("engine.metrics.update", {
        "id": mid, "patch": payload.patch,
        "hypothesis_id": payload.hypothesis_id, "reason": payload.reason,
        "engine": payload.engine,
    }, ctx))
    # patch 显式改 dataset 时才检测（未改不重复提示）
    if "dataset" in payload.patch:
        missing = _data_missing_hint(ctx, [payload.patch.get("dataset")])
        if missing:
            res["data_missing"] = missing
    return res


@router.delete("/metrics/{mid}")
def remove_metric(
    mid: str,
    payload: MetricRemoveIn,
    ctx: Annotated[Ctx, Depends(_ctx)],
) -> dict[str, Any]:
    """软删指标（status=inactive；条目与 changelog 保留）。"""
    return _unwrap(tools.call_tool("engine.metrics.remove", {
        "id": mid, "hypothesis_id": payload.hypothesis_id,
        "reason": payload.reason, "engine": payload.engine,
    }, ctx))
