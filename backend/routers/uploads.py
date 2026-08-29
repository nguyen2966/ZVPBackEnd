"""
POST /api/videos  - user upload file .mp4, server convert HLS rồi đẩy lên VNDATA S3.
GET  /api/videos/{videoId} - tra trạng thái xử lý của video vừa upload.

Vì sao KHÔNG xử lý đồng bộ trong request:
    convert 5 rendition + upload ~12 object mất 15-30s. Giữ HTTP request mở suốt thời gian đó
    sẽ đụng timeout của client/proxy, và người dùng không biết chuyện gì đang xảy ra.
    Bảng videos đã có sẵn status 'PROCESSING'/'FAILED' đúng cho luồng này (SPEC mục 10.3),
    nên: nhận file -> trả 202 ngay -> xử lý nền -> client hỏi lại bằng GET /api/videos/{id}.

Luồng đầy đủ:
    1. Nhận multipart, kiểm tra nhanh (đuôi file, dung lượng, category, ffprobe đọc được)
    2. Ghi file vào downloads_v2/<video_id>.mp4
    3. Insert row status='PROCESSING' (playback_url tính sẵn được vì key S3 suy từ video_id)
    4. Trả 202 { videoId, status: "PROCESSING" }
    5. Nền: convert_v2.convert_one -> vndata_s3.upload_video_assets -> verify_video
       -> update status='READY' (hoặc 'FAILED' nếu hỏng)

Video chỉ vào feed khi status='READY', nên bước 3 không hề làm lộ video chưa upload xong.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile

from .. import db
from ..config import BASE_DIR, MAX_UPLOAD_BYTES
from ..errors import ApiError
from ..security import Principal, current_principal
from ..serializers import feed_video
from ..urls import request_base_url
from ..video_processing import probe_duration_ms

router = APIRouter(prefix="/api", tags=["upload"])

SOURCE_DIR = BASE_DIR / "downloads_v2"

# Đọc/ghi theo khối để file lớn không nằm hết trong RAM.
CHUNK_BYTES = 1024 * 1024


def _new_video_id() -> str:
    """
    Id cho video do user upload. Tiền tố 'up_' để phân biệt với id YouTube trong pool seed,
    và chỉ dùng [a-z0-9_] nên an toàn khi làm tên file lẫn key S3.
    """
    return f"up_{uuid.uuid4().hex[:11]}"


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    """Ghi file lên đĩa theo khối, huỷ ngay khi vượt MAX_UPLOAD_BYTES."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(CHUNK_BYTES):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise ApiError(
                    400, "INVALID_REQUEST",
                    f"File vượt quá {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
                )
            out.write(chunk)
    return written


async def _process(video_id: str, source: Path) -> None:
    """
    Chạy nền: convert -> upload S3 -> verify -> đánh dấu READY.

    Mọi lỗi đều kết thúc bằng status='FAILED' chứ không để row treo mãi ở 'PROCESSING' -
    client cần phân biệt được "đang xử lý" và "hỏng rồi, đừng chờ nữa".
    Import ở trong hàm để thiếu ffmpeg/boto3 không làm sập cả app lúc khởi động.
    """
    from convert_v2 import convert_one
    from vndata_s3 import upload_video_assets, verify_video

    try:
        ok, note = await asyncio.to_thread(convert_one, source, False)
        if not ok:
            raise RuntimeError(f"convert thất bại: {note}")

        urls = await asyncio.to_thread(upload_video_assets, video_id)
        await asyncio.to_thread(verify_video, video_id)

        await db.pool().execute(
            """
            update videos
               set status = 'READY', playback_url = $2, thumbnail_url = $3
             where id = $1
            """,
            video_id, urls["hls_url"], urls["thumbnail_url"],
        )
    except Exception as exc:  # noqa: BLE001 - lỗi nào cũng phải ghi lại thành FAILED
        await db.pool().execute("update videos set status = 'FAILED' where id = $1", video_id)
        print(f"[upload] {video_id} FAILED: {exc}")


@router.post("/videos", status_code=202)
async def upload_video(
    background: BackgroundTasks,
    response: Response,
    file: UploadFile = File(...),
    title: str = Form(...),
    categoryId: int = Form(...),
    caption: str = Form(""),
    principal: Principal = Depends(current_principal),
):
    """Nhận file .mp4, trả 202 ngay; việc convert/upload chạy nền."""
    filename = file.filename or ""
    if not filename.lower().endswith(".mp4"):
        raise ApiError(400, "INVALID_REQUEST", "Chỉ nhận file .mp4")
    if not title.strip():
        raise ApiError(400, "INVALID_REQUEST", "Thiếu title")

    category = await db.pool().fetchrow("select id from categories where id = $1", categoryId)
    if category is None:
        raise ApiError(400, "INVALID_REQUEST", f"categoryId không tồn tại: {categoryId}")

    video_id = _new_video_id()
    source = SOURCE_DIR / f"{video_id}.mp4"
    await _save_upload(file, source)

    # Kiểm tra ngay tại request: file hỏng thì báo lỗi luôn thay vì để user chờ rồi nhận FAILED.
    duration_ms = await asyncio.to_thread(probe_duration_ms, source)
    if duration_ms <= 0:
        source.unlink(missing_ok=True)
        raise ApiError(400, "INVALID_REQUEST", "File không phải video hợp lệ hoặc không đọc được")

    # Key trên S3 suy được từ video_id nên biết trước URL cuối cùng, không cần cập nhật 2 lần.
    from vndata_s3 import S3Settings, build_asset_urls
    urls = build_asset_urls(S3Settings.from_env(), video_id)

    await db.pool().execute(
        """
        insert into videos (id, creator_id, category_id, title, caption, duration_ms,
                            playback_url, thumbnail_url, status)
        values ($1, $2, $3, $4, $5, $6, $7, $8, 'PROCESSING')
        """,
        video_id, principal.user_id, categoryId, title.strip(), caption.strip(),
        duration_ms, urls["hls_url"], urls["thumbnail_url"],
    )

    background.add_task(_process, video_id, source)
    response.status_code = 202
    return {"videoId": video_id, "status": "PROCESSING", "durationMs": duration_ms}


_VIDEO_SQL = """
select v.id, v.title, v.caption, v.duration_ms, v.playback_url, v.thumbnail_url,
       v.like_count, v.dislike_count, v.bookmark_count, v.status,
       c.name as category_name,
       u.id as creator_id, u.display_name, u.username, u.avatar_url
  from videos v
  join users u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where v.id = $1
"""

# Video của một creator, mới nhất lên đầu.
# $2 = người gọi có phải chính chủ không. Chính chủ thấy cả PROCESSING/FAILED (để theo dõi
# video mình vừa upload); người khác chỉ thấy READY - trạng thái xử lý dở/hỏng là việc riêng
# của người upload, không nên lộ ra ngoài.
_USER_VIDEOS_SQL = """
select v.id, v.title, v.caption, v.duration_ms, v.playback_url, v.thumbnail_url,
       v.like_count, v.dislike_count, v.bookmark_count, v.status,
       c.name as category_name,
       u.id as creator_id, u.display_name, u.username, u.avatar_url
  from videos v
  join users u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where v.creator_id = $1
   and v.status <> 'DELETED'
   and ($2::boolean or v.status = 'READY')
 order by v.created_at desc
"""


@router.get("/users/{user_id}/videos")
async def list_user_videos(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """
    Danh sách video do user đó upload, cùng shape với /api/feed nhưng mỗi item có thêm
    `status` - client dùng chung parser, đọc thêm `status` nếu cần.

    Khác /api/users/{id}/bookmarks (chỉ xem được của chính mình): video upload vốn là nội
    dung công khai, ai cũng xem được trang của người khác. Chỉ giấu phần riêng tư: video
    đang PROCESSING hoặc đã FAILED thì chỉ chính chủ mới thấy.
    """
    is_self = user_id == principal.user_id
    rows = await db.pool().fetch(_USER_VIDEOS_SQL, user_id, is_self)

    viewer: dict[str, dict] = {r["id"]: {} for r in rows}
    if rows:
        for r in await db.pool().fetch(
            "select video_id, type from reactions where user_id = $1 and video_id = any($2::text[]) and active",
            principal.user_id, [r["id"] for r in rows],
        ):
            state = viewer[r["video_id"]]
            if r["type"] == "BOOKMARK":
                state["isBookmarked"] = True
            else:
                state["reaction"] = r["type"]

    base = request_base_url(request)
    return {
        "items": [
            {
                "position": i,
                "status": row["status"],
                "video": feed_video(row, viewer[row["id"]], base),
            }
            for i, row in enumerate(rows)
        ]
    }


@router.get("/videos/{video_id}")
async def get_video(
    video_id: str,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """
    Trạng thái xử lý + object `video` đúng shape của /api/feed.

    Client upload xong thì hỏi lại endpoint này cho tới khi `status` khác 'PROCESSING':
    'READY' là phát được, 'FAILED' là bỏ cuộc (đừng chờ tiếp).
    """
    row = await db.pool().fetchrow(_VIDEO_SQL, video_id)
    if row is None or row["status"] == "DELETED":
        raise ApiError(404, "NOT_FOUND", f"Không có video '{video_id}'")

    viewer: dict = {}
    for r in await db.pool().fetch(
        "select type from reactions where user_id = $1 and video_id = $2 and active",
        principal.user_id, video_id,
    ):
        if r["type"] == "BOOKMARK":
            viewer["isBookmarked"] = True
        else:
            viewer["reaction"] = r["type"]

    return {
        "status": row["status"],
        "video": feed_video(row, viewer, request_base_url(request)),
    }
