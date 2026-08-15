"""Connection pool asyncpg dùng chung cho toàn bộ API."""

from __future__ import annotations

import asyncpg

from .config import DATABASE_DSN

_pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    """Tạo pool khi app khởi động."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_DSN, min_size=1, max_size=10)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool chưa được khởi tạo - gọi connect() trong lifespan của app")
    return _pool
