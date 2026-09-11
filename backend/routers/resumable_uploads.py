"""Resumable MP4 upload endpoints cho single-session MVP.

Client khởi tạo session, gửi từng part và gọi complete. Complete ghép MP4 rồi trả ``202``;
backend tiếp tục xử lý nền: convert HLS, upload lên VNData và cập nhật video thành ``READY``
hoặc ``FAILED``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from starlette.requests import ClientDisconnect

from .. import db
from ..config import (
    MAX_UPLOAD_THUMBNAIL_BYTES,
    UPLOAD_PART_SIZE_BYTES,
    UPLOAD_SESSION_TTL_SECONDS,
    UPLOAD_STORAGE_DIR,
)
from ..errors import ApiError
from ..security import Principal, current_principal
from ..serializers import feed_video
from ..upload_storage import UploadStorage
from ..upload_config import load_max_upload_file_size
from ..urls import request_base_url
from ..video_processing import probe_duration_ms, process_resumable_video

router = APIRouter(prefix="/api/video-uploads", tags=["upload"])
upload_storage = UploadStorage(UPLOAD_STORAGE_DIR)

_SESSION_SQL = """
select s.id, s.video_id, s.file_size, s.part_size, s.expires_at,
       v.creator_id, v.category_id, v.title, v.caption, v.status,
       v.duration_ms, v.playback_url, v.thumbnail_url,
       v.like_count, v.dislike_count, v.bookmark_count,
       c.name as category_name,
       u.display_name, u.username, u.avatar_url
  from video_upload_sessions s
  join videos v on v.id = s.video_id
  join users u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where s.id = $1
"""


def _new_video_id() -> str:
    return f"up_{uuid.uuid4().hex[:11]}"


def _total_parts(file_size: int, part_size: int) -> int:
    if file_size <= 0 or part_size <= 0:
        raise ValueError("file_size và part_size phải lớn hơn 0")
    return (file_size + part_size - 1) // part_size


def _expected_part_size(file_size: int, part_size: int, part_number: int) -> int:
    total_parts = _total_parts(file_size, part_size)
    if part_number <= 0 or part_number > total_parts:
        raise ValueError(f"partNumber phải nằm trong khoảng 1...{total_parts}")
    if part_number < total_parts:
        return part_size
    return file_size - part_size * (total_parts - 1)


def _session_response(row, uploaded_parts: list[int] | None = None) -> dict:
    response = {
        "uploadId": str(row["id"]),
        "videoId": row["video_id"],
        "status": row["status"],
        "partSize": row["part_size"],
    }
    if uploaded_parts is not None:
        total_parts = _total_parts(row["file_size"], row["part_size"])
        accepted = [number for number in uploaded_parts if number <= total_parts]
        accepted_set = set(accepted)
        response["uploadedParts"] = accepted
        response["missingParts"] = [
            number
            for number in range(1, total_parts + 1)
            if number not in accepted_set
        ]
    return response


def _initialization_response(row, base_url: str) -> dict:
    response = _session_response(row)
    video_row = dict(row)
    video_row["id"] = row["video_id"]
    response["video"] = feed_video(video_row, {}, base_url)
    return response


def _matches_initialization(
    row,
    file_size: int,
    title: str,
    caption: str,
    category_id: int,
) -> bool:
    return (
        row["file_size"] == file_size
        and row["title"] == title
        and (row["caption"] or "") == caption
        and row["category_id"] == category_id
    )


async def _owned_session(upload_id: uuid.UUID, principal: Principal):
    row = await db.pool().fetchrow(_SESSION_SQL, upload_id)
    if (
        row is None
        or row["creator_id"] != principal.user_id
    ):
        raise ApiError(404, "NOT_FOUND", "Không tìm thấy upload")
    return row


def _ensure_uploading(row) -> None:
    if row["status"] != "UPLOADING":
        raise ApiError(
            409,
            "UPLOAD_NOT_ACTIVE",
            f"Upload đang ở trạng thái {row['status']}",
        )
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise ApiError(409, "UPLOAD_EXPIRED", "Upload đã hết hạn")


async def _read_thumbnail(thumbnail: UploadFile) -> bytes:
    if thumbnail.content_type not in {"image/jpeg", "image/jpg"}:
        raise ApiError(
            422, "INVALID_METADATA",
            "Thumbnail phải là JPEG",
            errors=[{"field": "thumbnail", "rule": "content_type", "message": "Thumbnail phải là JPEG (image/jpeg)"}],
        )

    content = await thumbnail.read(MAX_UPLOAD_THUMBNAIL_BYTES + 1)
    if not content or len(content) > MAX_UPLOAD_THUMBNAIL_BYTES:
        raise ApiError(
            413, "FILE_TOO_LARGE",
            f"Thumbnail phải nhỏ hơn hoặc bằng {MAX_UPLOAD_THUMBNAIL_BYTES // (1024 * 1024)}MB",
            details={"max_size_bytes": MAX_UPLOAD_THUMBNAIL_BYTES, "actual_size_bytes": len(content) if content else 0},
        )
    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise ApiError(
            422, "INVALID_METADATA",
            "Thumbnail JPEG không hợp lệ",
            errors=[{"field": "thumbnail", "rule": "jpeg_format", "message": "File không phải JPEG hợp lệ (thiếu SOI/EOI marker)"}],
        )
    return content


async def _read_part(request: Request, expected_size: int) -> bytes | None:
    """
    Đọc body thành bytes.

    Trả ``None`` nếu client ngắt kết nối giữa chừng (``ClientDisconnect``) — caller
    sẽ xử lý bằng cách trả 499 Client Closed Request thay vì để exception lan ra ngoài.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST", "Content-Length không hợp lệ")
        if declared_size != expected_size:
            raise ApiError(
                400,
                "INVALID_PART_SIZE",
                f"Part sai kích thước: expected={expected_size}, actual={declared_size}",
            )

    content = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > expected_size - len(content):
                raise ApiError(
                    400,
                    "INVALID_PART_SIZE",
                    f"Part vượt quá kích thước expected={expected_size}",
                )
            content.extend(chunk)
    except ClientDisconnect:
        # Client ngắt kết nối (mất mạng, tắt app, timeout proxy…) — không crash server.
        return None

    if len(content) != expected_size:
        raise ApiError(
            400,
            "INVALID_PART_SIZE",
            f"Part sai kích thước: expected={expected_size}, actual={len(content)}",
        )
    return bytes(content)


@router.post("", status_code=201)
async def initialize_upload(
    request: Request,
    response: Response,
    uploadId: uuid.UUID = Form(...),
    title: str = Form(...),
    categoryId: int = Form(...),
    fileSize: int = Form(...),
    caption: str = Form(""),
    thumbnail: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
):
    """Tạo video UPLOADING và một session dùng chung cho các part request."""
    max_file_size_bytes = await load_max_upload_file_size()
    normalized_title = title.strip()
    normalized_caption = caption.strip()
    if not normalized_title:
        raise ApiError(
            422, "INVALID_METADATA",
            "Thiếu title",
            errors=[{"field": "title", "rule": "min_length", "message": "title không được rỗng"}],
        )
    if fileSize <= 0:
        raise ApiError(
            422, "INVALID_METADATA",
            "fileSize phải lớn hơn 0",
            errors=[{"field": "fileSize", "rule": "min", "message": "fileSize phải >= 1"}],
        )
    if fileSize > max_file_size_bytes:
        raise ApiError(
            413, "FILE_TOO_LARGE",
            "fileSize vượt quá giới hạn upload",
            details={
                "max_size_bytes": max_file_size_bytes,
                "actual_size_bytes": fileSize,
            },
        )

    existing = await db.pool().fetchrow(_SESSION_SQL, uploadId)
    if existing is not None:
        if existing["creator_id"] != principal.user_id:
            raise ApiError(404, "NOT_FOUND", "Không tìm thấy upload")
        if not _matches_initialization(
            existing,
            fileSize,
            normalized_title,
            normalized_caption,
            categoryId,
        ):
            raise ApiError(409, "UPLOAD_CONFLICT", "uploadId đã được dùng cho nội dung khác")
        response.status_code = 200
        return _initialization_response(existing, request_base_url(request))

    category_exists = await db.pool().fetchval(
        "select exists(select 1 from categories where id = $1)",
        categoryId,
    )
    if not category_exists:
        raise ApiError(
            422, "INVALID_METADATA",
            f"categoryId không tồn tại: {categoryId}",
            errors=[{"field": "categoryId", "rule": "exists", "message": f"categoryId={categoryId} không tồn tại"}],
        )

    thumbnail_content = await _read_thumbnail(thumbnail)
    video_id = _new_video_id()

    from vndata_s3 import S3Settings, build_asset_urls

    urls = build_asset_urls(S3Settings.from_env(), video_id)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=UPLOAD_SESSION_TTL_SECONDS
    )

    try:
        async with db.pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    insert into videos (
                        id, creator_id, category_id, title, caption, duration_ms,
                        playback_url, thumbnail_url, status
                    )
                    values ($1, $2, $3, $4, $5, 0, $6, null, 'UPLOADING')
                    """,
                    video_id,
                    principal.user_id,
                    categoryId,
                    normalized_title,
                    normalized_caption,
                    urls["hls_url"],
                )
                await connection.execute(
                    """
                    insert into video_upload_sessions (
                        id, video_id, file_size, part_size, expires_at
                    )
                    values ($1, $2, $3, $4, $5)
                    """,
                    uploadId,
                    video_id,
                    fileSize,
                    UPLOAD_PART_SIZE_BYTES,
                    expires_at,
                )
    except asyncpg.UniqueViolationError:
        existing = await db.pool().fetchrow(_SESSION_SQL, uploadId)
        if existing is None or existing["creator_id"] != principal.user_id:
            raise ApiError(409, "UPLOAD_CONFLICT", "Không thể tạo upload")
        if not _matches_initialization(
            existing,
            fileSize,
            normalized_title,
            normalized_caption,
            categoryId,
        ):
            raise ApiError(409, "UPLOAD_CONFLICT", "uploadId đã được dùng cho nội dung khác")
        response.status_code = 200
        return _initialization_response(existing, request_base_url(request))

    try:
        await upload_storage.create_workspace(uploadId)
        from vndata_s3 import upload_thumbnail

        await asyncio.to_thread(upload_thumbnail, video_id, thumbnail_content)
        await db.pool().execute(
            """
            update videos
               set thumbnail_url = $2
             where id = $1 and creator_id = $3 and status = 'UPLOADING'
            """,
            video_id,
            urls["thumbnail_url"],
            principal.user_id,
        )
    except Exception as error:
        await db.pool().execute(
            "delete from videos where id = $1 and creator_id = $2 and status = 'UPLOADING'",
            video_id,
            principal.user_id,
        )
        await upload_storage.remove_workspace(uploadId)
        raise ApiError(500, "UPLOAD_INITIALIZATION_FAILED", "Không thể khởi tạo upload") from error

    created = await db.pool().fetchrow(_SESSION_SQL, uploadId)
    response.status_code = 201
    return _initialization_response(created, request_base_url(request))


@router.put("/{upload_id}/parts/{part_number}", status_code=204)
async def upload_part(
    upload_id: uuid.UUID,
    part_number: int,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    row = await _owned_session(upload_id, principal)
    _ensure_uploading(row)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/octet-stream":
        raise ApiError(400, "INVALID_REQUEST", "Part phải dùng application/octet-stream")

    try:
        expected_size = _expected_part_size(
            row["file_size"],
            row["part_size"],
            part_number,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_REQUEST", str(error)) from error

    content = await _read_part(request, expected_size)
    if content is None:
        # Client ngắt kết nối — trả 499 Client Closed Request. Không crash server.
        return Response(status_code=499)
    await upload_storage.write_part(upload_id, part_number, expected_size, content)
    return Response(status_code=204)


@router.get("/{upload_id}")
async def inspect_upload(
    upload_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
):
    row = await _owned_session(upload_id, principal)
    if row["status"] != "UPLOADING":
        result = _session_response(row)
        result["uploadedParts"] = []
        result["missingParts"] = []
        return result

    uploaded = await upload_storage.uploaded_part_numbers(upload_id)
    return _session_response(row, uploaded)


@router.post("/{upload_id}/complete", status_code=202)
async def complete_upload(
    upload_id: uuid.UUID,
    background: BackgroundTasks,
    response: Response,
    principal: Principal = Depends(current_principal),
):
    row = await _owned_session(upload_id, principal)
    if row["status"] != "UPLOADING":
        response.status_code = 202 if row["status"] == "PROCESSING" else 200
        return _session_response(row)

    _ensure_uploading(row)
    total_parts = _total_parts(row["file_size"], row["part_size"])
    missing = await upload_storage.missing_part_numbers(upload_id, total_parts)
    if missing:
        raise ApiError(
            409,
            "UPLOAD_INCOMPLETE",
            f"Upload còn thiếu part: {missing}",
        )

    try:
        source = await upload_storage.merge_parts(
            upload_id,
            total_parts,
            row["file_size"],
        )
    except (FileNotFoundError, ValueError) as error:
        raise ApiError(409, "UPLOAD_INCOMPLETE", str(error)) from error

    duration_ms = await asyncio.to_thread(probe_duration_ms, source)
    if duration_ms <= 0:
        raise ApiError(
            422,
            "INVALID_METADATA",
            "File không phải video hợp lệ hoặc không đọc được",
            errors=[{
                "field": "file",
                "rule": "readable",
                "message": "ffprobe không đọc được thông tin video",
            }],
        )

    updated = await db.pool().fetchrow(
        """
        update videos
           set status = 'PROCESSING'
         where id = $1 and creator_id = $2 and status = 'UPLOADING'
        returning status
        """,
        row["video_id"],
        principal.user_id,
    )
    if updated is None:
        row = await _owned_session(upload_id, principal)
        response.status_code = 202 if row["status"] == "PROCESSING" else 200
        return _session_response(row)

    response.status_code = 202
    background.add_task(
        process_resumable_video,
        upload_id,
        row["video_id"],
        source,
        duration_ms,
    )
    result = _session_response(row)
    result["status"] = "PROCESSING"
    return result
