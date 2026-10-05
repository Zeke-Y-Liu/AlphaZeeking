"""pytest 公共 fixture（T1.1 / T2.1）。

- ``settings_override``：以 tmp_path 覆盖 workspace_root，并清理 settings 缓存。
- ``client``：``TestClient``（以 context manager 启动，触发 lifespan 探测）。
- ``pg_engine`` / ``alembic_config``：T2.1 迁移与 seed 测试所用；PG 不可达时
  ``pytest.skip``，保证无 PG 环境（如纯 CI）下 ``make test`` 仍绿。
全部测试不依赖外部网络。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings, get_settings

CORE_DIR = Path(__file__).resolve().parents[1]


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
