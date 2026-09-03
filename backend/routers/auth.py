"""POST /api/auth/register | login | refresh (SPEC mục 3.1-3.3)."""

from __future__ import annotations

import random

import asyncpg
from fastapi import APIRouter, Response

from .. import db
from ..config import ACCESS_TOKEN_TTL_SECONDS, AVATAR_POOL
from ..errors import ApiError
from ..models import LoginRequest, RefreshRequest, RegisterRequest
from ..security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, response: Response):
    avatar_url = random.choice(AVATAR_POOL)
    try:
        row = await db.pool().fetchrow(
            """
            insert into users (username, display_name, avatar_url, password_hash)
            values ($1, $2, $3, $4)
            returning id, username, display_name
            """,
            body.username, body.displayName, avatar_url, hash_password(body.password),
        )
    except asyncpg.UniqueViolationError:
        raise ApiError(409, "USERNAME_TAKEN", "Username already exists")

    response.status_code = 201
    return {
        "userId": str(row["id"]),
        "username": row["username"],
        "displayName": row["display_name"],
    }


@router.post("/login")
async def login(body: LoginRequest):
    user = await db.pool().fetchrow(
        "select id, password_hash from users where username = $1", body.username
    )
    if user is None or not verify_password(body.password, user["password_hash"]):
        raise ApiError(401, "TOKEN_EXPIRED", "Invalid username or password")

    refresh_token, refresh_hash = new_refresh_token()

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            # Thứ tự bắt buộc (SPEC 3.2): revoke session cũ TRƯỚC rồi mới insert session mới,
            # nếu không partial unique index sessions_one_active_per_user sẽ chặn (bất biến 8).
            await conn.execute(
                """
                update sessions
                   set revoked_at = now(), revoked_reason = 'NEW_LOGIN'
                 where user_id = $1 and revoked_at is null
                """,
                user["id"],
            )
            session = await conn.fetchrow(
                """
                insert into sessions (user_id, device_id, refresh_token_hash)
                values ($1, $2, $3)
                returning id
                """,
                user["id"], body.deviceId, refresh_hash,
            )

    return {
        "userId": str(user["id"]),
        "accessToken": create_access_token(user["id"], session["id"]),
        "refreshToken": refresh_token,
        "expiresInSeconds": ACCESS_TOKEN_TTL_SECONDS,
    }


@router.post("/refresh")
async def refresh(body: RefreshRequest):
    row = await db.pool().fetchrow(
        """
        select id, user_id, revoked_at
          from sessions
         where refresh_token_hash = $1
        """,
        hash_refresh_token(body.refreshToken),
    )
    if row is None:
        raise ApiError(401, "SESSION_REVOKED", "Unknown refresh token")
    if row["revoked_at"] is not None:
        # SPEC 3.3: session đã revoke -> phải là SESSION_REVOKED, không phải TOKEN_EXPIRED,
        # để client dừng hẳn vòng retry và bắt user đăng nhập lại.
        raise ApiError(401, "SESSION_REVOKED", "Signed in on another device")

    return {
        "accessToken": create_access_token(row["user_id"], row["id"]),
        "expiresInSeconds": ACCESS_TOKEN_TTL_SECONDS,
    }
