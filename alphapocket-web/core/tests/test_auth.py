"""T5.2 认证端点测试（离线、确定性）。

- 本文件内自定义 fixture，不修改 ``conftest.py``；
- PG 或 Redis 任一不可达 → 整体 ``pytest.skip``（不使全量测试失败）；
- 依赖真实 PG（demo 用户已 seed）与真实 Redis（失败计数），无外部网络。
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.redis import get_redis

DEMO_USER = "z"
DEMO_PASSWORD = "Alpha618"
UNKNOWN_USER = "no_such_user"
FAIL_KEY = f"ap:login:fail:{DEMO_USER}"
UNKNOWN_FAIL_KEY = f"ap:login:fail:{UNKNOWN_USER}"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
ME_URL = "/api/v1/auth/me"


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    """真实 PG 引擎；不可达或 demo 用户缺失则跳过本模块。"""
    url = get_settings().database_url
    engine = create_engine(
        url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
            demo = conn.execute(
                text("select 1 from users where username = :u"), {"u": DEMO_USER}
            ).first()
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        engine.dispose()
        pytest.skip(f"PostgreSQL 不可达（{url}）：{type(exc).__name__}: {exc}")
    if demo is None:
        engine.dispose()
        pytest.skip("demo 用户未 seed（请先运行 `python -m app.seed`）")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def rds():
    """真实 Redis 客户端；不可达则跳过本模块。"""
    client = get_redis()
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达：{type(exc).__name__}: {exc}")
    client.delete(FAIL_KEY, UNKNOWN_FAIL_KEY)
    yield client
    client.delete(FAIL_KEY, UNKNOWN_FAIL_KEY)


@pytest.fixture()
def client(pg_engine: Engine, rds) -> Iterator[TestClient]:
    """TestClient；每个用例前后清零失败计数，保证用例间隔离。"""
    from app.main import create_app

    rds.delete(FAIL_KEY, UNKNOWN_FAIL_KEY)
    with TestClient(create_app()) as test_client:
        yield test_client
    rds.delete(FAIL_KEY, UNKNOWN_FAIL_KEY)


def _login(client: TestClient, password: str) -> object:
    return client.post(LOGIN_URL, json={"username": DEMO_USER, "password": password})


def test_login_success_issues_decodable_tokens(client: TestClient) -> None:
    resp = _login(client, DEMO_PASSWORD)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"access", "refresh", "token_type", "expires_in", "user"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"]["username"] == DEMO_USER
    assert body["user"]["role"] == "owner"
    assert isinstance(body["user"]["id"], int)

    access = decode_token(body["access"], "access")
    refresh = decode_token(body["refresh"], "refresh")
    assert access["sub"] == DEMO_USER
    assert access["uid"] == body["user"]["id"]
    assert access["typ"] == "access"
    assert access["exp"] - access["iat"] == 900  # access TTL ≈ 15m
    assert refresh["typ"] == "refresh"


def test_wrong_password_and_unknown_user_are_indistinguishable(client: TestClient) -> None:
    wrong = _login(client, "wrong-password")
    unknown = client.post(
        LOGIN_URL, json={"username": UNKNOWN_USER, "password": "whatever"}
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json() == unknown.json()  # 不得泄漏「用户是否存在」
    assert wrong.json()["code"] == "UNAUTHORIZED"
    assert set(wrong.json()) == {"code", "message", "detail"}


def test_five_failures_then_locked_even_with_correct_password(client: TestClient, rds) -> None:
    for i in range(5):
        assert _login(client, "wrong-password").status_code == 401, f"第 {i + 1} 次应为 401"

    # 计数落 Redis 且 TTL 有效
    assert rds.exists(FAIL_KEY) == 1
    assert rds.get(FAIL_KEY) == "5"
    assert rds.ttl(FAIL_KEY) > 0

    locked = _login(client, DEMO_PASSWORD)  # 第 6 次：即使密码正确也拒绝
    assert locked.status_code == 429
    body = locked.json()
    assert body["code"] == "ACCOUNT_LOCKED"
    assert "分钟" in body["message"]
    assert set(body) == {"code", "message", "detail"}


def test_success_resets_failure_counter(client: TestClient, rds) -> None:
    for _ in range(3):
        assert _login(client, "wrong-password").status_code == 401
    assert rds.exists(FAIL_KEY) == 1

    ok = _login(client, DEMO_PASSWORD)
    assert ok.status_code == 200
    assert rds.exists(FAIL_KEY) == 0  # 成功后清零

    # 再失败 1 次不应触发锁定
    assert _login(client, "wrong-password").status_code == 401


def test_me_accepts_access_rejects_refresh_and_garbage(client: TestClient) -> None:
    tokens = _login(client, DEMO_PASSWORD).json()

    ok = client.get(ME_URL, headers={"Authorization": f"Bearer {tokens['access']}"})
    assert ok.status_code == 200
    assert ok.json() == tokens["user"]

    by_refresh = client.get(
        ME_URL, headers={"Authorization": f"Bearer {tokens['refresh']}"}
    )
    assert by_refresh.status_code == 401
    assert by_refresh.json()["code"] == "UNAUTHORIZED"

    garbage = client.get(ME_URL, headers={"Authorization": "Bearer not-a-jwt"})
    assert garbage.status_code == 401
    assert garbage.json()["code"] == "UNAUTHORIZED"


def test_refresh_accepts_refresh_rejects_access(client: TestClient) -> None:
    tokens = _login(client, DEMO_PASSWORD).json()

    refreshed = client.post(REFRESH_URL, json={"refresh": tokens["refresh"]})
    assert refreshed.status_code == 200
    new = refreshed.json()
    assert decode_token(new["access"], "access")["typ"] == "access"
    assert decode_token(new["refresh"], "refresh")["typ"] == "refresh"
    assert new["user"]["username"] == DEMO_USER

    by_access = client.post(REFRESH_URL, json={"refresh": tokens["access"]})
    assert by_access.status_code == 401
    assert by_access.json()["code"] == "UNAUTHORIZED"
