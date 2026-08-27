"""
Lắp ráp FastAPI app: API theo docs/SPEC.md + phục vụ asset HLS trong cùng một origin.

Một origin duy nhất là có chủ đích: client chỉ cần cấu hình một base URL, và URL asset trong
feed được dựng từ chính host của request nên đổi giữa localhost / IP LAN / tunnel không cần
sửa gì ở client lẫn database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, errors
from .routers import (
    assets,
    auth,
    categories,
    config as config_router,
    feed,
    reactions,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    try:
        yield
    finally:
        await db.disconnect()


def create_app() -> FastAPI:
    app = FastAPI(title="ZVideoPlus Backend", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Client đọc ETag của /api/config và Content-Range khi preload theo byte-range;
        # không expose thì trình duyệt/WebView giấu hai header này đi.
        expose_headers=["ETag", "Content-Range", "Accept-Ranges", "Retry-After"],
    )

    errors.install(app)   # ép mọi lỗi về shape { "error": { code, message } } (SPEC mục 7)

    app.include_router(auth.router)
    app.include_router(categories.router)
    app.include_router(feed.router)
    app.include_router(reactions.router)
    app.include_router(config_router.router)
    app.include_router(assets.router)

    @app.get("/health")
    async def health():
        """Kiểm tra nhanh: DB sống chưa, pool có bao nhiêu video READY."""
        row = await db.pool().fetchrow(
            """
            select (select count(*) from videos where status = 'READY') as ready_videos,
                   (select count(*) from users)  as users,
                   (select count(*) from videos) as videos
            """
        )
        hls_ready = len(list(assets.HLS_DIR.glob("*/master.m3u8"))) if assets.HLS_DIR.exists() else 0
        return {
            "status": "ok",
            "readyVideos": row["ready_videos"],
            "videos": row["videos"],
            "users": row["users"],
            "hlsAssets": hls_ready,
        }

    return app


app = create_app()
