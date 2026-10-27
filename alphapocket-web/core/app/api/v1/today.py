"""今日结论端点（2026-10-26 快速获取组合操作结论页）。

- ``GET /api/v1/today``：一屏聚合——服务器当前时间 + 全部 active 组合的
  highlight 建议（中文短句）与整体倾向 + 收盘自动评估状态。

数据全部来自既有 ``runs.summary``（收盘后调度器已跑完评估落库），本端点
只读聚合、无 job、无锁、无副作用。组合无 run → 该组合 ``level="stale"``、
highlights 提示「暂无评估」，仍 200。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.today import build_today

router = APIRouter(tags=["today"])

DbDep = Annotated[Session, Depends(get_db)]


@router.get("/today")
def get_today(db: DbDep) -> dict[str, Any]:
    """今日结论（快速获取组合操作结论）：当前时间 + 各组合 highlight。"""
    return build_today(db, get_settings(), datetime.now())
