"""T2.2 组合 CRUD 与物化测试。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- PG 不可达时整体 ``pytest.skip``；不依赖外网（仅读本地 α 参考 JSON）；
- 测试自行清理本次新增的 portfolio/positions/audit_log 行；
- pid 使用随机后缀，避免与 seed 的 ``multi5`` / ``cn4`` 冲突。

物化顺序说明：**按传入顺序**写入与读出（``positions.id`` 递增即入参顺序），
与 α ``remote/repo/portfolio/multi5.json`` 的 secid 顺序一致。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import session_scope
from app.models import AuditLog, Portfolio, Position

# α 参考仓库根（仅本地文件；可由 AP_ALPHA_REPO_DIR 覆盖）
ALPHA_REPO = Path(os.environ.get("AP_ALPHA_REPO_DIR", "/workspace/alphapocket"))
ALPHA_MULTI5 = ALPHA_REPO / "remote" / "repo" / "portfolio" / "multi5.json"

MULTI5_SECID_ORDER = ["510300.SH", "501018.SH", "518880.SH", "513100.SH", "511260.SH"]


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    """真实 PG 可达性探测；不可达则跳过（纯 CI 无 PG 时 make test 仍绿）。"""
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    """以临时目录覆盖 workspace_root；物化文件落在 ``<root>/repo/portfolio/``。"""
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def seeded(pg_ready, alembic_config) -> None:
    """确保 PG schema 已迁移且 seed 数据（九标的 + multi5）存在。

    ``test_migrations`` 会 drop/upgrade 清空数据，且按文件名字典序先于本文件执行；
    为兼容「单独运行本文件」与「全量运行」两种顺序，此处先幂等 ``upgrade head``
    再幂等重放 seed（仅调用，不修改 seed.py）。
    """
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
    """收集本用例创建的 pid，结束后清理 portfolio/positions/audit_log。"""
    pids: list[str] = []
    yield pids
    if pids:
        with session_scope() as db:
            ids = [
                row for (row,) in db.execute(
                    select(Portfolio.id).where(Portfolio.pid.in_(pids))
                ).all()
            ]
            if ids:
                db.execute(delete(Position).where(Position.portfolio_id.in_(ids)))
                db.execute(delete(Portfolio).where(Portfolio.id.in_(ids)))
            db.execute(
                delete(AuditLog).where(
                    AuditLog.entity == "portfolio", AuditLog.entity_id.in_(pids)
                )
            )


# ── helpers ─────────────────────────────────────────────────────


def _rand_pid(prefix: str = "pf") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _materialized_path(ws_root: Path, pid: str) -> Path:
    return ws_root / "repo" / "portfolio" / f"{pid}.json"


def _alpha_multi5() -> dict:
    if not ALPHA_MULTI5.exists():
        pytest.skip(f"α 参考文件缺失：{ALPHA_MULTI5}")
    return json.loads(ALPHA_MULTI5.read_text(encoding="utf-8"))


# ── 用例 ────────────────────────────────────────────────────────


def test_create_multi5_replica_materializes_like_alpha(api_client, ws_root, tracked) -> None:
    """用例 1：multi5 复刻（5×20）物化 JSON 与 α 参考字段级一致（仅 id 为随机 pid）。

    V4-β：物化新增 ``currency``/``hk_connect`` 字段（多市场扩展），α 参考的既有字段
    必须逐一相等，新字段允许额外出现。
    """
    alpha = _alpha_multi5()
    pid = _rand_pid("pf")

    payload = {
        "name": alpha["name"],
        "pid": pid,
        "note": alpha["note"],
        "positions": [{"secid": p["secid"], "weight": p["weight"]} for p in alpha["positions"]],
    }
    resp = api_client.post("/api/v1/portfolios", json=payload)
    assert resp.status_code == 201, resp.text
    tracked.append(pid)
    assert resp.json()["pid"] == pid

    materialized = json.loads(_materialized_path(ws_root, pid).read_text(encoding="utf-8"))
    assert materialized["id"] == pid
    for key, value in alpha.items():
        if key == "id":
            continue
        if key == "positions":
            assert len(materialized["positions"]) == len(value)
            for got_pos, exp_pos in zip(materialized["positions"], value, strict=True):
                for k, v in exp_pos.items():
                    assert got_pos.get(k) == v, (
                        f"position 字段 {k} 与 α 参考不一致：{got_pos.get(k)} != {v}"
                    )
        else:
            assert materialized[key] == value, (
                f"字段 {key} 与 α 参考不一致：{materialized[key]} != {value}"
            )
    # V4-β 新增字段存在性（A股 currency=CNY / hk_connect=None）
    for got_pos in materialized["positions"]:
        assert got_pos["currency"] == "CNY"
        assert got_pos["hk_connect"] is None


def test_create_portfolio_triggers_bg_sync(api_client, tracked, _no_auto_sync) -> None:
    """M11：新建组合 → 自动提交一次增量同步 job（conftest 桩 runner 记录，不触外网）。"""
    pid = _rand_pid("pf")
    resp = api_client.post(
        "/api/v1/portfolios",
        json={"name": "自动同步触发", "pid": pid,
              "positions": [{"secid": "510300.SH", "weight": 60},
                            {"secid": "518880.SH", "weight": 40}]},
    )
    assert resp.status_code == 201, resp.text
    tracked.append(pid)
    assert len(_no_auto_sync) == 1, "新建组合应触发一次自动同步"
    assert _no_auto_sync[0]["type"] == "data_sync"
    assert _no_auto_sync[0]["params"] == {"mode": "incremental", "secid": None}


def test_weight_window_boundaries_rejected_with_suggestion(api_client) -> None:
    """用例 2：权重和 99 / 101 → 422 WEIGHT_NOT_NORMALIZED，detail 含归一建议。"""
    for weights in ([{"secid": "510300.SH", "weight": 50}, {"secid": "518880.SH", "weight": 49}],
                    [{"secid": "510300.SH", "weight": 51}, {"secid": "518880.SH", "weight": 50}]):
        resp = api_client.post(
            "/api/v1/portfolios",
            json={"name": "边界", "pid": _rand_pid("pf"), "positions": weights},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == ErrorCode.WEIGHT_NOT_NORMALIZED.value
        suggestion = body["detail"]["suggestion"]
        assert [item["secid"] for item in suggestion] == [w["secid"] for w in weights]
        assert abs(sum(item["weight"] for item in suggestion) - 100.0) < 0.01


def test_unregistered_security_rejected(api_client) -> None:
    """用例 3：未注册标的 → 422 SECURITY_NOT_FOUND（detail 列出缺失 secid）。"""
    resp = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "未注册",
            "pid": _rand_pid("pf"),
            "positions": [{"secid": "510300.SH", "weight": 50},
                          {"secid": "999999.SH", "weight": 50}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == ErrorCode.SECURITY_NOT_FOUND.value
    assert "999999.SH" in body["detail"]["missing_secids"]


def test_equal_weight_splits_evenly(api_client, ws_root, tracked) -> None:
    """用例 4：equal_weight=true → 忽略入参权重，按 100/n 等分（5×20.0）。"""
    pid = _rand_pid("pf")
    resp = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "等权五标的",
            "pid": pid,
            "positions": [{"secid": s} for s in MULTI5_SECID_ORDER],
            "equal_weight": True,
        },
    )
    assert resp.status_code == 201, resp.text
    tracked.append(pid)
    positions = resp.json()["positions"]
    assert [p["weight"] for p in positions] == [20.0] * 5

    materialized = json.loads(_materialized_path(ws_root, pid).read_text(encoding="utf-8"))
    assert [p["weight"] for p in materialized["positions"]] == [20.0] * 5
    assert [p["secid"] for p in materialized["positions"]] == MULTI5_SECID_ORDER


def test_duplicate_secid_rejected(api_client) -> None:
    """用例 5：组合内 secid 重复 → 422（detail.duplicate_secids）。"""
    resp = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "重复",
            "pid": _rand_pid("pf"),
            "positions": [{"secid": "510300.SH", "weight": 50},
                          {"secid": "510300.SH", "weight": 50}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert body["detail"]["duplicate_secids"] == ["510300.SH"]


def test_explicit_pid_conflict_rejected(api_client) -> None:
    """用例 6：显式 pid 与既有组合（seed multi5）冲突 → 422，detail 说明冲突。"""
    resp = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "冲突",
            "pid": "multi5",
            "positions": [{"secid": "510300.SH", "weight": 100}],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert body["detail"]["conflict"] == "multi5"


def test_delete_archives_and_removes_from_list(api_client, tracked) -> None:
    """用例 7：DELETE → status=archived、列表不再出现；GET 详情仍可见（软删）。"""
    pid = _rand_pid("pf")
    created = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "待归档",
            "pid": pid,
            "positions": [{"secid": "510300.SH", "weight": 60},
                          {"secid": "518880.SH", "weight": 40}],
        },
    )
    assert created.status_code == 201, created.text
    tracked.append(pid)

    listed = api_client.get("/api/v1/portfolios")
    assert listed.status_code == 200
    assert pid in [pf["pid"] for pf in listed.json()]

    deleted = api_client.delete(f"/api/v1/portfolios/{pid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "archived"

    listed_after = api_client.get("/api/v1/portfolios")
    assert pid not in [pf["pid"] for pf in listed_after.json()]

    detail = api_client.get(f"/api/v1/portfolios/{pid}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "archived"

    # 归档审计已落库
    with session_scope() as db:
        actions = [
            row for (row,) in db.execute(
                select(AuditLog.action).where(
                    AuditLog.entity == "portfolio", AuditLog.entity_id == pid
                )
            ).all()
        ]
    assert "create" in actions and "delete" in actions


def test_auto_pid_generated_when_absent(api_client, tracked) -> None:
    """用例 9：未提供 pid → 自动生成唯一 ``pf_<n>``；详情可读。"""
    created = api_client.post(
        "/api/v1/portfolios",
        json={
            "name": "自动 pid",
            "positions": [{"secid": "510300.SH", "weight": 100}],
        },
    )
    assert created.status_code == 201, created.text
    pid = created.json()["pid"]
    tracked.append(pid)
    assert re.fullmatch(r"pf_\d+", pid), pid

    detail = api_client.get(f"/api/v1/portfolios/{pid}")
    assert detail.status_code == 200
    assert detail.json()["pid"] == pid


def test_get_missing_portfolio_404(api_client) -> None:
    """用例 10：GET/DELETE 不存在组合 → 404 NOT_FOUND（统一错误体）。"""
    for method in (api_client.get, api_client.delete):
        resp = method("/api/v1/portfolios/pf_not_exist_xyz")
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == ErrorCode.NOT_FOUND.value
