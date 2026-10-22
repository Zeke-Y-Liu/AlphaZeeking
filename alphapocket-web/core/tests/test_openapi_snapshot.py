"""T7.2 OpenAPI 契约快照（§6「契约不漂移」）。

core 的 OpenAPI 文档是 BFF 原样透传与前端 SDK 生成的唯一事实源。本用例把
``create_app().openapi()`` 的全量文档冻结为 ``snapshots/openapi.json``：任何
端点增删、路径/参数/schema 变更都会使快照不匹配而失败，须显式更新快照后方可合入。

离线可跑（不触达 PG/Redis）：``create_app()`` 仅装配路由，lifespan 探测在
``TestClient`` 上下文中才触发，本用例不进入 lifespan。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app

SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi.json"

# 本阶段（β）已交付端点；缺失即视为契约回退（§6.2）。
IMPLEMENTED_PATHS = {
    "/healthz",
    "/api/v1/_ping",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/me",
    "/api/v1/securities/search",
    "/api/v1/securities/register",
    "/api/v1/data/sync",
    "/api/v1/data/sync-secid",
    "/api/v1/data/status",
    "/api/v1/data/verifications",
    "/api/v1/portfolios",
    "/api/v1/portfolios/{pid}",
    "/api/v1/portfolios/{pid}/review",
    "/api/v1/portfolios/{pid}/backtest",
    "/api/v1/portfolios/{pid}/rebalance-plan",
    "/api/v1/portfolios/{pid}/plans",
    "/api/v1/plans/{plan_id}",
    "/api/v1/plans/{plan_id}/exec-orders",
    "/api/v1/exec-orders/{exec_id}",
    "/api/v1/exec-orders/{exec_id}/confirm",
    "/api/v1/exec-orders/{exec_id}/fill",
    "/api/v1/exec-orders/{exec_id}/cancel",
    "/api/v1/portfolios/{pid}/exec-orders",
    "/api/v1/portfolios/{pid}/rebalance-history",
    "/api/v1/audit",
    "/api/v1/runs",
    "/api/v1/runs/{run_uid}/artifacts/{file}",
    "/api/v1/jobs/{job_id}",
    # V4-β 多市场：实时行情 + 港股通名单
    "/api/v1/market/quotes",
    "/api/v1/market/ggt",
    # 引擎板块：指标注册表 CRUD + 数据集联动
    "/api/v1/engines/metrics",
    "/api/v1/engines/metrics/{mid}",
    "/api/v1/engines/describe",
    "/api/v1/engines/datasets",
    # 单公司分析（M10）：档案 CRUD + 四模块 job + 消息 + 影子回测
    "/api/v1/analysis",
    "/api/v1/analysis/{aid}",
    "/api/v1/analysis/{aid}/run",
    "/api/v1/analysis/{aid}/news",
    "/api/v1/analysis/{aid}/news/refresh",
    "/api/v1/analysis/{aid}/backtest",
}


def test_openapi_snapshot_matches() -> None:
    """全量文档与冻结快照逐位一致。"""
    assert SNAPSHOT.exists(), f"缺少契约快照：{SNAPSHOT}（请先冻结）"
    spec = create_app().openapi()
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert spec == frozen


def test_openapi_covers_implemented_paths() -> None:
    """已交付端点全部在文档中（防止路由被误删/改名）。"""
    paths = set(create_app().openapi()["paths"])
    assert IMPLEMENTED_PATHS <= paths, f"契约缺失端点：{sorted(IMPLEMENTED_PATHS - paths)}"
