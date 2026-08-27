"""Cấu hình đọc từ .env cho backend."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise SystemExit(f"Thiếu biến môi trường '{key}' trong .env")
    return value


def database_dsn() -> str:
    """
    DSN cho asyncpg.

    Chỉ bỏ tham số `schema` (kiểu Prisma, libpq không hiểu) và giữ lại phần còn lại của query
    string. Trước đây hàm này cắt sạch mọi thứ sau dấu '?', nhưng DSN của Neon mang theo
    `sslmode=require&channel_binding=require` - cắt đi là mất luôn yêu cầu TLS.
    """
    raw = _require("DB_URL")
    base, sep, query = raw.partition("?")
    if not sep:
        return base
    kept = [p for p in query.split("&") if p and not p.lower().startswith("schema=")]
    return f"{base}?{'&'.join(kept)}" if kept else base


DATABASE_DSN = database_dsn()

# Ký access token. Dev để mặc định cho tiện; production PHẢI đặt JWT_SECRET thật trong .env.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-insecure-secret-change-me")
JWT_ALGORITHM = "HS256"

# SPEC mục 10.1: 1 giờ là hợp lý.
ACCESS_TOKEN_TTL_SECONDS = int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "3600"))

# Ép base URL tuyệt đối cho asset (vd khi chạy sau tunnel). Rỗng -> suy từ request.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# SPEC mục 3.4: feed luôn trả 10 item random.
FEED_SIZE = 10

# SPEC mục 3.5: giới hạn mềm 200 mutation/request.
MAX_MUTATIONS_PER_BATCH = 200

# SPEC mục 3.7: TTL gợi ý cho client cache config bundle.
CONFIG_TTL_SECONDS = 900

# Giới hạn dung lượng file user upload. Chặn ngay lúc ghi từng khối chứ không đọc hết vào RAM.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
