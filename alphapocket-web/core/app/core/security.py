"""认证安全件（T5.2，可复用）。

提供：密码哈希/校验、JWT 签发/解码、FastAPI 当前用户依赖。

JWT 规格（冻结，实施书 T5.2）：HS256，密钥 ``AP_JWT_SECRET``；
claims ``sub``=username、``uid``=users.id、``role``、``typ``∈{access,refresh}、``iat``、``exp``。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# argon2id（与 T2.1 seed 使用的方案一致）
_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# auto_error=False：缺失 Authorization 头时由本模块抛出统一错误体（401），而非 FastAPI 默认 403
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """生成 argon2id 哈希。"""
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与 argon2id 哈希；任何异常一律视为不匹配。"""
    try:
        return _pwd_context.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - 哈希损坏/格式异常一律按不匹配处理
        return False


def parse_ttl_seconds(value: str, default: int) -> int:
    """解析 TTL 文本（``15m`` / ``7d`` / ``900s`` / ``900``）为秒；非法时取默认值。"""
    text = (value or "").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            return int(float(text[:-1]) * units[text[-1]])
        except ValueError:
            return default
    try:
        return int(text)
    except ValueError:
        return default


def access_ttl_seconds() -> int:
    """access token 有效期（秒）。"""
    return parse_ttl_seconds(get_settings().jwt_access_ttl, 900)


def _create_token(user: User, typ: str, ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user.username,
        "uid": user.id,
        "role": user.role,
        "typ": typ,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGORITHM)


def create_access_token(user: User) -> str:
    """签发 access token（默认 15m）。"""
    return _create_token(user, TOKEN_TYPE_ACCESS, access_ttl_seconds())


def create_refresh_token(user: User) -> str:
    """签发 refresh token（默认 7d）。"""
    ttl = parse_ttl_seconds(get_settings().jwt_refresh_ttl, 604800)
    return _create_token(user, TOKEN_TYPE_REFRESH, ttl)


def decode_token(token: str, expected_typ: str | None = None) -> dict[str, Any]:
    """验签并解码 JWT；无效/过期/类型不符 → 401 ``UNAUTHORIZED``。"""
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效或已过期") from exc
    if expected_typ is not None and claims.get("typ") != expected_typ:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证类型不符")
    return claims


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """FastAPI 依赖：从 ``Authorization: Bearer <access>`` 解析并加载当前用户。"""
    if credentials is None or not credentials.credentials:
        raise AppError(ErrorCode.UNAUTHORIZED, "缺少访问令牌")
    claims = decode_token(credentials.credentials, TOKEN_TYPE_ACCESS)
    uid = claims.get("uid")
    user = db.get(User, uid) if isinstance(uid, int) else None
    if user is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    return user
