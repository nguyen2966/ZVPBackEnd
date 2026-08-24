"""Connection pool asyncpg dùng chung cho toàn bộ API."""

from __future__ import annotations

import asyncpg

from .config import DATABASE_DSN

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """
    Ép search_path cho từng connection trong pool.

    Cần thiết khi chạy trên Neon qua endpoint `-pooler` (PgBouncer, transaction pooling):
    nhiều client dùng chung một backend, nên một `SET` do client khác để lại có thể dính sang
    session của mình. Thực tế đã gặp: pg_dump đặt `search_path = ''` lúc restore và mọi
    connection sau đó qua pooler đều thấy search_path rỗng -> mọi query không ghi rõ schema
    đều lỗi `relation "videos" does not exist`.

    Đặt ở đây thì không phụ thuộc vào việc pooler có reset session hay không.
    """
    await conn.execute('set search_path to "$user", public')


async def connect() -> asyncpg.Pool:
    """Tạo pool khi app khởi động."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_DSN,
            min_size=1,
            max_size=10,
            init=_init_connection,
            server_settings={"search_path": '"$user", public'},
        )
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
