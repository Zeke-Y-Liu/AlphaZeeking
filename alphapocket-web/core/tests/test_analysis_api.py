"""M10/M11 单公司分析 API 测试（档案 CRUD + 自动同步触发语义）。

- 自带 fixture（同 test_securities / test_portfolios 约定），PG 不可达 → skip；
- 不依赖外网：自动同步经 conftest ``_no_auto_sync`` 桩 runner 记录提交参数，
  绝不产生真实 ``data_sync`` job（真实链路验证见 MANUAL-TEST-CHECKLIST M11）；
- 每用例硬删本次创建的 analysis_profiles / 影子组合 / positions / audit 行，
  避免污染运行库（测试与运行共库的既有隔离缺陷下尤为必要）。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import session_scope
from app.models import AnalysisProfile, AuditLog, Portfolio, Position

DEFAULT_MODULES = {"fundamental": True, "industry": True, "technical": True, "news": True}


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    """真实 PG 可达性探测；不可达则跳过依赖它的用例。"""
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    """以临时目录覆盖 workspace_root；建档物化的影子组合 JSON 落在其中。"""
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def seeded(pg_ready, alembic_config) -> None:
    """确保 schema 已迁移且 seed（601058.SH 等 15 标的）存在（幂等重放）。"""
    from alembic import command

    from app.seed import run_seed

    try:
        command.upgrade(alembic_config, "head")
        run_seed()
    except Exception as exc:  # noqa: BLE001 - PG 未就绪/不可用时跳过
        pytest.skip(f"seed 数据不可用（PG 未迁移？）：{type(exc).__name__}: {exc}")


@pytest.fixture
def api_client(ws_root, seeded) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def tracked(pg_ready) -> Iterator[list[str]]:
    """收集本用例创建的 aid，结束后硬删档案 / 影子组合 / 审计行。"""
    aids: list[str] = []
    yield aids
    with session_scope() as db:
        for aid in aids:
            pid = f"shadow_{aid}"
            pf_ids = list(
                db.execute(select(Portfolio.id).where(Portfolio.pid == pid)).scalars()
            )
            if pf_ids:
                db.execute(delete(Position).where(Position.portfolio_id.in_(pf_ids)))
                db.execute(delete(Portfolio).where(Portfolio.id.in_(pf_ids)))
            db.execute(delete(AnalysisProfile).where(AnalysisProfile.aid == aid))
            db.execute(
                delete(AuditLog).where(
                    AuditLog.entity == "analysis_profile", AuditLog.entity_id == aid
                )
            )
            db.execute(
                delete(AuditLog).where(
                    AuditLog.entity == "portfolio", AuditLog.entity_id == pid
                )
            )


def _rand_aid() -> str:
    return f"ca_t{uuid.uuid4().hex[:8]}"


# ── 用例 ────────────────────────────────────────────────────────


def test_create_profile_triggers_bg_sync(api_client, tracked, _no_auto_sync) -> None:
    """M11：新建档案 → 自动提交一次增量同步 job（桩 runner 记录，不触外网）。"""
    aid = _rand_aid()

    resp = api_client.post(
        "/api/v1/analysis", json={"secid": "601058.SH", "aid": aid, "name": "触发测试"}
    )

    assert resp.status_code == 201, resp.text
    tracked.append(aid)
    body = resp.json()
    assert body["aid"] == aid
    assert body["secid"] == "601058.SH"
    assert body["shadow_pid"] == f"shadow_{aid}"
    assert body["modules"] == DEFAULT_MODULES
    assert len(_no_auto_sync) == 1, "新建档案应触发一次自动同步"
    assert _no_auto_sync[0]["type"] == "data_sync"
    assert _no_auto_sync[0]["params"] == {"mode": "incremental", "secid": None}
    assert _no_auto_sync[0]["lock"] == "sync"


def test_create_profile_unregistered_secid_no_sync(api_client, _no_auto_sync) -> None:
    """未注册标的 → 422 SECURITY_NOT_FOUND，且不触发任何自动同步。"""
    resp = api_client.post("/api/v1/analysis", json={"secid": "NOT_EXIST.SH"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == ErrorCode.SECURITY_NOT_FOUND.value
    assert _no_auto_sync == [], "校验失败的建档不应触发自动同步"


def test_delete_archives_profile_and_shadow(api_client, tracked, _no_auto_sync) -> None:
    """删除档案 → 档案与影子组合级联 archived；默认列表不再可见；不追加同步。"""
    aid = _rand_aid()
    create = api_client.post(
        "/api/v1/analysis", json={"secid": "601058.SH", "aid": aid, "name": "级联测试"}
    )
    assert create.status_code == 201, create.text
    tracked.append(aid)

    resp = api_client.delete(f"/api/v1/analysis/{aid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"

    listing = api_client.get("/api/v1/analysis").json()
    assert all(p["aid"] != aid for p in listing), "默认列表不应包含已归档档案"

    with session_scope() as db:
        shadow = db.execute(
            select(Portfolio).where(Portfolio.pid == f"shadow_{aid}")
        ).scalar_one()
        assert shadow.status == "archived", "影子组合应级联归档"

    assert len(_no_auto_sync) == 1, "删除档案不应再触发同步（仅创建时 1 次）"
