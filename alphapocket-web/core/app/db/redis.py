"""Redis 客户端（T5.2）。

用于登录失败计数等轻量状态。惰性连接：仅在首次执行命令时建立连接；
命令失败由调用方按 fail-open 处理（见 ``app/api/v1/auth.py``）。
"""
from __future__ import annotations

from functools import lru_cache

import redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    """进程级 Redis 客户端（``decode_responses=True``，返回 str）。"""
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
