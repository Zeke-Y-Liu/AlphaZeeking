"""数据库引擎与会话（§5.1 / T2.1）。

导入路径（后续 T1.2/T5.2 复用）::

    from app.db.session import get_db, session_scope
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# 由配置构造引擎；pool_pre_ping 规避长连接失效
engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：``Depends(get_db)``，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：供 job 线程 / seed 复用；成功提交，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
