"""Resumable MP4 upload endpoints cho single-session MVP.

Flow dừng ở ``PROCESSING``. Convert HLS và upload media lên VNData thuộc Phase 3.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile

from .. import db
from ..config import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_THUMBNAIL_BYTES,
    UPLOAD_PART_SIZE_BYTES,
    UPLOAD_SESSION_TTL_SECONDS,
    UPLOAD_STORAGE_DIR,
)
from ..errors import ApiError
from ..security import Principal, current_principal
from ..upload_storage import UploadStorage
from ..video_processing import process_resumable_video

router = APIRouter(prefix="/api/video-uploads", tags=["upload"])
upload_storage = UploadStorage(UPLOAD_STORAGE_DIR)

_SESSION_SQL = """
select s.id, s.video_id, s.file_size, s.part_size, s.expires_at,
       v.creator_id, v.category_id, v.title, v.caption, v.status
  from video_upload_sessions s
  join videos v on v.id = s.video_id
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
        or row["status"] == "DELETED"
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
        raise ApiError(400, "INVALID_REQUEST", "Thumbnail phải là JPEG")

    content = await thumbnail.read(MAX_UPLOAD_THUMBNAIL_BYTES + 1)
    if not content or len(content) > MAX_UPLOAD_THUMBNAIL_BYTES:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"Thumbnail phải nhỏ hơn hoặc bằng {MAX_UPLOAD_THUMBNAIL_BYTES // (1024 * 1024)}MB",
        )
    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise ApiError(400, "INVALID_REQUEST", "Thumbnail JPEG không hợp lệ")
    return content


async def _read_part(request: Request, expected_size: int) -> bytes:
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
    async for chunk in request.stream():
        if len(chunk) > expected_size - len(content):
            raise ApiError(
                400,
                "INVALID_PART_SIZE",
                f"Part vượt quá kích thước expected={expected_size}",
            )
        content.extend(chunk)
    if len(content) != expected_size:
        raise ApiError(
            400,
            "INVALID_PART_SIZE",
            f"Part sai kích thước: expected={expected_size}, actual={len(content)}",
        )
    return bytes(content)


@router.post("", status_code=201)
async def initialize_upload(
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
    normalized_title = title.strip()
    normalized_caption = caption.strip()
    if not normalized_title:
        raise ApiError(400, "INVALID_REQUEST", "Thiếu title")
    if fileSize <= 0 or fileSize > MAX_UPLOAD_BYTES:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"fileSize phải nằm trong khoảng 1...{MAX_UPLOAD_BYTES}",
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
        return _session_response(existing)

    category_exists = await db.pool().fetchval(
        "select exists(select 1 from categories where id = $1)",
        categoryId,
    )
    if not category_exists:
        raise ApiError(400, "INVALID_REQUEST", f"categoryId không tồn tại: {categoryId}")

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
                    values ($1, $2, $3, $4, $5, 0, $6, $7, 'UPLOADING')
                    """,
                    video_id,
                    principal.user_id,
                    categoryId,
                    normalized_title,
                    normalized_caption,
                    urls["hls_url"],
                    urls["thumbnail_url"],
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
        return _session_response(existing)

    try:
        await upload_storage.create_workspace(uploadId)
        from vndata_s3 import upload_thumbnail

        await asyncio.to_thread(upload_thumbnail, video_id, thumbnail_content)
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
    return _session_response(created)


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
    )
    result = _session_response(row)
    result["status"] = "PROCESSING"
    return result
