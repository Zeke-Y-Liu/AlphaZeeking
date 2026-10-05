"""Alembic 迁移环境（T2.1）。

``sqlalchemy.url`` 从 ``app.core.config.settings.database_url`` 读取，不写死。
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 兜底：保证 core/ 在 sys.path（cwd 通常已是 core/）
_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

config = context.config


def _database_url() -> str:
    """运行期从 core 配置读取连接串。"""
    from app.core.config import get_settings

    return get_settings().database_url


def _target_metadata():
    """导入全部模型（触发注册）后返回 metadata。"""
    from app.models import Base

    return Base.metadata


config.set_main_option("sqlalchemy.url", _database_url())
target_metadata = _target_metadata()


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
