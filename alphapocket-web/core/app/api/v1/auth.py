"""认证端点（T5.2）：登录 / 刷新 / 当前用户。

职责边界（实施书 §6.2）：**core 侧自签发，bff 校验**。core 负责密码校验（argon2id）、
JWT 签发与 ``/auth/me``；bff 负责本地验签守卫与转发。

失败锁定（冻结）：连续失败 5 次锁定 15 分钟，计数落 Redis
``ap:login:fail:<username>``（TTL 900s）。**降级策略**：Redis 不可达时 fail-open
（放行登录）并打 WARNING——锁定属纵深防御而非主认证，β 单实例下优先保证可用性。
"""
from __future__ import annotations

import logging
import math
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    access_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    verify_password,
)
from app.db.redis import get_redis
from app.db.session import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

DbDep = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

MAX_FAILS = 5           # 连续失败阈值
LOCK_TTL_SECONDS = 900  # 锁定窗口：15 分钟
_FAIL_KEY = "ap:login:fail:{username}"


class LoginIn(BaseModel):
    username: str
    password: str


class RefreshIn(BaseModel):
    refresh: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str


class TokenOut(BaseModel):
    access: str
    refresh: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _issue_tokens(user: User) -> TokenOut:
    return TokenOut(
        access=create_access_token(user),
        refresh=create_refresh_token(user),
        token_type="bearer",
        expires_in=access_ttl_seconds(),
        user=UserOut(id=user.id, username=user.username, role=user.role),
    )


def _locked_minutes(username: str) -> int | None:
    """锁定期内返回剩余分钟数；未锁定返回 None；Redis 不可达 → None（fail-open）。"""
    key = _FAIL_KEY.format(username=username)
    try:
        client = get_redis()
        count = int(client.get(key) or 0)
        ttl = int(client.ttl(key))
    except Exception as exc:  # noqa: BLE001 - Redis 不可达即降级放行
        # 降级策略（冻结）：Redis 不可达时放行登录，仅记录 WARNING。
        # 锁定是纵深防御而非主认证，β 单实例下优先保证可用性。
        logger.warning("Redis 不可达，登录锁定检查降级放行（fail-open）：%s", type(exc).__name__)
        return None
    if count >= MAX_FAILS and ttl > 0:
        return max(1, math.ceil(ttl / 60))
    return None


def _record_failure(username: str) -> None:
    """失败计数 +1，并将 TTL 重置为锁定窗口；Redis 不可达则告警（fail-open）。"""
    key = _FAIL_KEY.format(username=username)
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, LOCK_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 计数失败不阻断登录主流程
        logger.warning("Redis 不可达，登录失败计数未记录（fail-open）：%s", type(exc).__name__)


def _clear_failures(username: str) -> None:
    """登录成功后清零失败计数；Redis 不可达则告警（fail-open）。"""
    try:
        get_redis().delete(_FAIL_KEY.format(username=username))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 不可达，登录失败计数清零失败（fail-open）：%s", type(exc).__name__)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: DbDep) -> TokenOut:
    """用户名+密码登录；失败统一返回 401（不区分用户是否存在），连续失败触发 429 锁定。"""
    remaining = _locked_minutes(payload.username)
    if remaining is not None:
        raise AppError(ErrorCode.ACCOUNT_LOCKED, f"账号已锁定，请 {remaining} 分钟后重试")

    user = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        # 统一 401 且文案一致，避免用户名枚举
        _record_failure(payload.username)
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")

    _clear_failures(payload.username)
    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenOut)
def refresh(payload: RefreshIn, db: DbDep) -> TokenOut:
    """用 refresh token 换新 token 三件套；无效/过期/类型不符 → 401。"""
    claims = decode_token(payload.refresh, TOKEN_TYPE_REFRESH)
    uid = claims.get("uid")
    user = db.get(User, uid) if isinstance(uid, int) else None
    if user is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    return _issue_tokens(user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    """返回当前登录用户（需有效 access token）。"""
    return UserOut(id=user.id, username=user.username, role=user.role)
