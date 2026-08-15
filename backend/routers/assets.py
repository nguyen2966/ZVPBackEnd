"""
Phục vụ file HLS + thumbnail do convert.py sinh ra (vai trò CDN, thay Cloudinary).

Đường dẫn giữ nguyên hình dạng URL của Cloudinary
(res.cloudinary.com/<cloud>/video/upload/sp_auto/<id>.m3u8) để client không phải sửa gì.

File .mp4dv phục vụ qua FileResponse nên có sẵn HTTP Range -> 206 + Content-Range, đúng thứ
client cần để preload theo byte-range (docs/des.md §3).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..config import BASE_DIR
from ..errors import ApiError

router = APIRouter(tags=["assets"])

HLS_DIR = BASE_DIR / "public" / "hls"
THUMB_DIR = BASE_DIR / "public" / "thumbs"

# Tên rendition mà convert.py sinh ra (phải khớp LADDER trong convert.py).
VALID_TIERS = {"pg_1", "pg_2", "pg_3", "pg_4", "pg_5"}

M3U8_MEDIA_TYPE = "application/vnd.apple.mpegurl"


def safe_name(value: str) -> str:
    """Chặn path traversal: chỉ nhận đúng phần tên file."""
    name = Path(value).name
    if name != value or name in ("", ".", ".."):
        raise ApiError(400, "INVALID_REQUEST", "Tên file không hợp lệ")
    return name


def safe_tier(value: str) -> str:
    if value not in VALID_TIERS:
        raise ApiError(404, "NOT_FOUND", f"Rendition không hợp lệ: {value}")
    return value


@router.get("/video/upload/sp_auto/{video_id}.m3u8")
async def serve_master(video_id: str):
    """Master playlist multivariant - chính là URL trong playbackAsset.url của feed."""
    path = HLS_DIR / safe_name(video_id) / "master.m3u8"
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", f"Chưa có HLS cho '{video_id}' - chạy convert.py")
    return FileResponse(path, media_type=M3U8_MEDIA_TYPE, headers={"Cache-Control": "no-cache"})


@router.get("/video/upload/sp_auto/{tier}/{video_id}.m3u8")
async def serve_variant(tier: str, video_id: str):
    """Media playlist của một rendition (URI do master trỏ tới)."""
    path = HLS_DIR / safe_name(video_id) / safe_tier(tier) / "index.m3u8"
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", f"Không có rendition '{tier}' cho '{video_id}'")
    return FileResponse(path, media_type=M3U8_MEDIA_TYPE, headers={"Cache-Control": "no-cache"})


@router.get("/video/upload/sp_auto/{tier}/{video_id}.mp4dv")
async def serve_segment(tier: str, video_id: str):
    """File fMP4 chứa toàn bộ segment của một rendition; client lấy từng đoạn bằng Range."""
    vid = safe_name(video_id)
    path = HLS_DIR / vid / safe_tier(tier) / f"{vid}.mp4dv"
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", "Không tìm thấy segment")
    return FileResponse(path, media_type="video/mp4")


@router.get("/video/upload/so_auto/{video_id}.jpg")
async def serve_thumbnail(video_id: str):
    path = THUMB_DIR / f"{safe_name(video_id)}.jpg"
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", f"Chưa có thumbnail cho '{video_id}' - chạy convert.py")
    return FileResponse(path, media_type="image/jpeg")
