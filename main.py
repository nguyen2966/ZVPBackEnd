# main.py
"""
Server thay thế Cloudinary: phục vụ HLS + thumbnail local và cấp API feed.

Endpoint:
    GET /api/feed
        Trả về 10 feed_item ngẫu nhiên (đổi bằng ?limit=) từ feed_items.json, bọc trong
        envelope feedKey/generationId/generatedAt/expiresAt/items. Những trường không có
        trong feed_items.json (serverScore, engagement, viewerState...) được mock ngẫu nhiên.

    GET /video/upload/sp_auto/<video_id>.m3u8           -> master playlist (multivariant)
    GET /video/upload/sp_auto/<tier>/<video_id>.m3u8    -> media playlist của 1 rendition
    GET /video/upload/sp_auto/<tier>/<video_id>.mp4dv   -> file fMP4 của rendition đó
    GET /video/upload/so_auto/<video_id>.jpg            -> thumbnail
        Đường dẫn cố tình giữ nguyên hình dạng URL của Cloudinary
        (res.cloudinary.com/<cloud>/video/upload/sp_auto/<id>.m3u8) để client không phải
        sửa gì khi chuyển từ Cloudinary sang server này.

        File .mp4dv được phục vụ qua FileResponse nên hỗ trợ sẵn HTTP Range -> trả 206 kèm
        Content-Range (des.md §3), đúng thứ client cần để preload theo byte-range.

Chạy convert.py trước để sinh asset trong public/hls và public/thumbs.

Cài đặt trước khi chạy:
    pip install fastapi uvicorn

Cách chạy:
    python main.py            -> http://localhost:3000
    Đặt biến môi trường PUBLIC_BASE_URL nếu muốn ép base URL trong response
    (mặc định tự lấy theo host mà client gọi tới - tiện khi test bằng điện thoại trong LAN).
"""

from __future__ import annotations

import json
import os
import random
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent
HLS_DIR = BASE_DIR / "public" / "hls"
THUMB_DIR = BASE_DIR / "public" / "thumbs"
FEED_FILE = BASE_DIR / "feed_items.json"

# Ép base URL trong response (vd: "https://abc.ngrok.io"). Bỏ trống -> suy ra từ request.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

DEFAULT_FEED_SIZE = 10
FEED_TTL = timedelta(hours=1)

# Tên các rendition mà convert.py sinh ra (phải khớp LADDER trong convert.py).
VALID_TIERS = {"pg_1", "pg_2", "pg_3", "pg_4", "pg_5"}

# video_id nằm ở cuối URL playbackAsset của Cloudinary: .../sp_auto/<video_id>.m3u8
VIDEO_ID_FROM_URL = re.compile(r"/([^/]+)\.m3u8$")

app = FastAPI(title="Local HLS + Feed Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- feed_items.json (cache theo mtime để sửa file xong không cần restart) ----
_feed_cache: list[dict[str, Any]] = []
_feed_mtime: float | None = None


def load_feed_items() -> list[dict[str, Any]]:
    global _feed_cache, _feed_mtime
    if not FEED_FILE.exists():
        raise HTTPException(status_code=503, detail=f"Chưa có {FEED_FILE.name} - chạy dowload.py trước.")
    mtime = FEED_FILE.stat().st_mtime
    if mtime != _feed_mtime:
        _feed_cache = json.loads(FEED_FILE.read_text(encoding="utf-8"))
        _feed_mtime = mtime
    return _feed_cache


def iso_ms(dt: datetime) -> str:
    """ISO-8601 kèm mili giây + hậu tố Z, vd: 2026-08-12T07:47:40.246Z."""
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def public_base(request: Request) -> str:
    """Base URL để dựng link asset: ưu tiên PUBLIC_BASE_URL, không có thì theo host client gọi."""
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def safe_name(value: str) -> str:
    """Chặn path traversal: chỉ lấy phần tên file, từ chối nếu khác giá trị gốc."""
    name = Path(value).name
    if name != value or name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    return name


def safe_tier(value: str) -> str:
    """Chỉ chấp nhận tên rendition hợp lệ (pg_1..pg_5) - chặn luôn path traversal."""
    if value not in VALID_TIERS:
        raise HTTPException(status_code=404, detail=f"Rendition không hợp lệ: {value}")
    return value


def localize_video(video: dict[str, Any], base: str) -> dict[str, Any]:
    """
    Đổi URL asset từ Cloudinary sang server này (giữ nguyên hình dạng đường dẫn), và mock
    những trường mà feed_items.json không có sẵn.
    """
    video = deepcopy(video)

    playback = video.get("playbackAsset") or {}
    match = VIDEO_ID_FROM_URL.search(playback.get("url", ""))
    if match:
        video_id = match.group(1)
        playback["url"] = f"{base}/video/upload/sp_auto/{video_id}.m3u8"
        video["playbackAsset"] = playback
        thumbnail = video.get("thumbnailAsset") or {}
        thumbnail["url"] = f"{base}/video/upload/so_auto/{video_id}.jpg"
        video["thumbnailAsset"] = thumbnail

    video.setdefault("engagement", {})
    engagement = video["engagement"]
    engagement.setdefault("likeCount", random.randint(0, 500_000))
    engagement.setdefault("dislikeCount", random.randint(0, 300))
    engagement.setdefault("bookmarkCount", random.randint(0, 1000))

    video.setdefault("viewerState", {})
    viewer = video["viewerState"]
    viewer.setdefault("isBookmarked", random.choice([True, False]))
    viewer.setdefault("bookmarkServerVersion", random.randint(1, 6))
    viewer.setdefault("reaction", random.choice(["LIKE", "DISLIKE", "NONE"]))
    viewer.setdefault("reactionServerVersion", random.randint(1, 6))

    return video


@app.get("/api/feed")
async def get_feed(request: Request, limit: int = DEFAULT_FEED_SIZE, feedKey: str = "for_you"):
    items = load_feed_items()
    if not items:
        raise HTTPException(status_code=503, detail="feed_items.json rỗng")

    limit = max(1, min(limit, len(items)))
    sample = random.sample(items, limit)

    now = datetime.now(timezone.utc)
    base = public_base(request)

    return {
        "feedKey": feedKey,
        "generationId": f"gen_{now:%Y%m%d%H%M}",
        "generatedAt": iso_ms(now),
        "expiresAt": iso_ms(now + FEED_TTL),
        "items": [
            {
                "position": position,
                "serverScore": round(random.uniform(7.5, 10.0), 2),  # không có nguồn thật -> mock
                "video": localize_video(entry["video"], base),
            }
            for position, entry in enumerate(sample)
        ],
    }


@app.get("/video/upload/sp_auto/{video_id}.m3u8")
async def serve_master(video_id: str):
    """Master playlist multivariant - đây là URL nằm trong playbackAsset.url của feed."""
    path = HLS_DIR / safe_name(video_id) / "master.m3u8"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Chưa có HLS cho '{video_id}' - chạy convert.py")
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/video/upload/sp_auto/{tier}/{video_id}.m3u8")
async def serve_variant(tier: str, video_id: str):
    """Media playlist của 1 rendition (URI do master trỏ tới)."""
    path = HLS_DIR / safe_name(video_id) / safe_tier(tier) / "index.m3u8"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Không có rendition '{tier}' cho '{video_id}'")
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/video/upload/sp_auto/{tier}/{video_id}.mp4dv")
async def serve_segment(tier: str, video_id: str):
    """
    File fMP4 chứa toàn bộ segment của 1 rendition; client lấy từng đoạn bằng HTTP Range.
    FileResponse tự xử lý Range -> 206 + Content-Range.
    """
    vid = safe_name(video_id)
    path = HLS_DIR / vid / safe_tier(tier) / f"{vid}.mp4dv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy segment")
    return FileResponse(path, media_type="video/mp4")


@app.get("/video/upload/so_auto/{video_id}.jpg")
async def serve_thumbnail(video_id: str):
    path = THUMB_DIR / f"{safe_name(video_id)}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Chưa có thumbnail cho '{video_id}' - chạy convert.py")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/")
async def root():
    """Trang kiểm tra nhanh: bao nhiêu video đã sẵn sàng phục vụ."""
    hls_ready = len(list(HLS_DIR.glob("*/master.m3u8"))) if HLS_DIR.exists() else 0
    thumbs_ready = len(list(THUMB_DIR.glob("*.jpg"))) if THUMB_DIR.exists() else 0
    return {
        "status": "ok",
        "feedItems": len(load_feed_items()),
        "hlsReady": hls_ready,
        "thumbnailsReady": thumbs_ready,
        "endpoints": ["/api/feed", "/video/upload/sp_auto/{id}.m3u8", "/video/upload/so_auto/{id}.jpg"],
    }


def lan_ip() -> str:
    """
    Địa chỉ LAN của máy này, để điện thoại trong cùng WiFi gọi thẳng vào - KHÔNG cần tunnel.
    Đây là cách tránh giới hạn rate của ngrok free (des.md §6): không tunnel thì không có
    giới hạn, không tốn TLS handshake cho mỗi request.
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
    print(f"[*] LAN    : http://{lan_ip()}:3000/api/feed  (dùng URL này cho app trên điện thoại)")
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=3000, 
        reload=True,
        timeout_keep_alive=65, # Giữ kết nối HTTP Keep-Alive lâu hơn
        limit_concurrency=100  # Tránh tràn socket khi client prefetch dồn dập
    )
