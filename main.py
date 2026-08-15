# main.py
"""
Điểm khởi chạy server: API (docs/SPEC.md) + phục vụ asset HLS trong cùng một origin.

Toàn bộ logic nằm trong package `backend/`:
    backend/app.py          lắp ráp FastAPI (CORS, error shape, router, lifespan DB pool)
    backend/routers/        auth, feed, reactions, config, assets
    backend/schema.sql      DDL
    backend/seed.py         tạo schema + nạp seed data

Chuẩn bị trước khi chạy lần đầu:
    pip install fastapi uvicorn asyncpg bcrypt pyjwt python-dotenv
    python convert.py            # sinh HLS + thumbnail vào public/
    python -m backend.seed       # tạo schema + seed 200 video, user test khoa/password123

Cách chạy:
    python main.py               -> http://localhost:3000
    Đặt PUBLIC_BASE_URL nếu chạy sau tunnel/proxy; bỏ trống thì URL asset trong response
    tự bám theo host mà client gọi tới (tiện khi test bằng điện thoại trong LAN - xem SERVING.md).
"""

from __future__ import annotations

import uvicorn

from backend.app import app  # noqa: F401  (uvicorn nạp qua chuỗi "main:app")
from backend.routers.assets import HLS_DIR, THUMB_DIR


def lan_ip() -> str:
    """
    Địa chỉ LAN của máy này để điện thoại cùng WiFi gọi thẳng vào - KHÔNG cần tunnel.
    Đây là cách tránh giới hạn rate của ngrok free (xem SERVING.md).
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # không gửi gói nào, chỉ để OS chọn interface ra ngoài
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


if __name__ == "__main__":
    print(f"[*] HLS   : {HLS_DIR}")
    print(f"[*] Thumbs: {THUMB_DIR}")
    print(f"[*] LAN   : http://{lan_ip()}:3000  (dùng base URL này cho app trên điện thoại)")
    uvicorn.run("main:app", host="0.0.0.0", port=3000, reload=False)
