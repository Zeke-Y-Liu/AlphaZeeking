"""pytest 公共 fixture（T1.1 / T2.1）。

- ``settings_override``：以 tmp_path 覆盖 workspace_root，并清理 settings 缓存。
- ``client``：``TestClient``（以 context manager 启动，触发 lifespan 探测）。
- ``pg_engine`` / ``alembic_config``：T2.1 迁移与 seed 测试所用；PG 不可达时
  ``pytest.skip``，保证无 PG 环境（如纯 CI）下 ``make test`` 仍绿。
全部测试不依赖外部网络。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings

CORE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _pg_test_isolation() -> Iterator[None]:
    """PG 测试库隔离：pytest 全程指向 ``<库名>_test``，绝不触碰运行库。

    背景：test_migrations 会 ``drop_all`` + ``downgrade base``，若与运行共库
    （默认 ``ap``）会把运行数据（users/portfolios/jobs…）全部清空。此处派生
    测试库名并通过 ``AP_DATABASE_URL`` 环境变量覆盖（alembic env.py 与各
    fixture 均从 ``get_settings()`` 读 URL，环境变量生效于任何连接之前）。

    - 测试库不存在时自动创建（经 ``postgres`` 运维库，AUTOCOMMIT）；
    - PG 不可达时仍指向测试库 URL（必然连不上）→ 由 ``pg_engine`` 自行
      skip，非 PG 测试不受影响。
    """
    url = make_url(get_settings().database_url)
    test_db = f"{url.database or 'ap'}_test"
    # 注意：str(MakeURL) 会把密码掩码为 "***"，必须 render_as_string(hide_password=False)
    test_url = url.set(database=test_db).render_as_string(hide_password=False)
    try:
        admin = create_engine(
            url.set(database="postgres").render_as_string(hide_password=False),
            connect_args={"connect_timeout": 3},
        )
        try:
            with admin.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT")
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :d"),
                    {"d": test_db},
                ).scalar()
                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{test_db}"'))
        finally:
            admin.dispose()
    except Exception:  # noqa: BLE001 - PG 不可达：统一指向测试库，由 pg_engine skip
        pass
    os.environ["AP_DATABASE_URL"] = test_url
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings_override(tmp_path, monkeypatch) -> Iterator[Settings]:
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client(settings_override: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    """Alembic 配置；sqlalchemy.url 由 migrations/env.py 从 core 配置注入。"""
    cfg = Config(str(CORE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(CORE_DIR / "migrations"))
    return cfg


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """真实 PG 引擎；不可达时跳过依赖它的测试（不使整体测试失败）。"""
    url = get_settings().database_url
    engine = create_engine(
        url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        engine.dispose()
        pytest.skip(f"PostgreSQL 不可达（{url}）：{type(exc).__name__}: {exc}")
    yield engine
    engine.dispose()
