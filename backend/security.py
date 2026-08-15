"""
Mật khẩu, access/refresh token và dependency xác thực.

Điểm quan trọng nhất (SPEC mục 7): 401 phải phân biệt được hai trường hợp
    TOKEN_EXPIRED   -> client refresh rồi thử lại
    SESSION_REVOKED -> session đã chết vì login ở thiết bị khác; client phải dừng retry
Gộp hai cái này lại thì client đốt hết số lần retry vào một session không bao giờ sống lại.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Request

from . import db
from .config import ACCESS_TOKEN_TTL_SECONDS, JWT_ALGORITHM, JWT_SECRET
from .errors import ApiError

# bcrypt chỉ dùng 72 byte đầu của mật khẩu; cắt trước cho tường minh thay vì để thư viện tự xử.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], password_hash.encode())
    except (ValueError, TypeError):
        return False


def new_refresh_token() -> tuple[str, str]:
    """Sinh refresh token; trả (token gốc cho client, hash để lưu DB)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),          # để kiểm tra session còn sống hay đã bị revoke
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID


async def current_principal(request: Request) -> Principal:
    """Dependency: xác thực Bearer token cho mọi endpoint trừ /api/auth/*."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "TOKEN_EXPIRED", "Missing bearer token")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "TOKEN_EXPIRED", "Access token expired")
    except jwt.PyJWTError:
        # Token hỏng/chữ ký sai: coi như hết hạn để client đi đường refresh thay vì kẹt cứng.
        raise ApiError(401, "TOKEN_EXPIRED", "Invalid access token")

    try:
        user_id = uuid.UUID(payload["sub"])
        session_id = uuid.UUID(payload["sid"])
    except (KeyError, ValueError):
        raise ApiError(401, "TOKEN_EXPIRED", "Malformed access token")

    # Kiểm tra session + chạm last_seen_at trong đúng một round trip.
    row = await db.pool().fetchrow(
        """
        update sessions
           set last_seen_at = now()
         where id = $1 and user_id = $2 and revoked_at is null
        returning id
        """,
        session_id, user_id,
    )
    if row is None:
        # Session không còn active -> đã bị revoke bởi lần login khác (bất biến 8).
        raise ApiError(401, "SESSION_REVOKED", "Signed in on another device")

    return Principal(user_id=user_id, session_id=session_id)


CurrentUser = Depends(current_principal)
