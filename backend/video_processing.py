"""Validate, convert và publish một resumable video sau khi nhận đủ part."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID

from . import db
from .config import UPLOAD_STORAGE_DIR
from .upload_storage import UploadStorage
from .video_deletion import remove_local_video_assets

upload_storage = UploadStorage(UPLOAD_STORAGE_DIR)
_processing_lock = asyncio.Lock()


def probe_duration_ms(source: Path) -> int:
    """Trả duration milliseconds, hoặc 0 nếu source không có video stream hợp lệ."""
    process = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-show_entries", "format=duration",
            "-of", "json",
            str(source),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        return 0
    try:
        data = json.loads(process.stdout)
        has_video = any(
            stream.get("codec_type") == "video"
            for stream in data.get("streams", [])
        )
        if not has_video:
            return 0
        return int(float(data["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError):
        return 0


async def remove_published_assets_if_video_missing(video_id: str) -> None:
    exists = await db.pool().fetchval(
        "select exists(select 1 from videos where id = $1)",
        video_id,
    )
    if not exists:
        from vndata_s3 import delete_video_assets

        await asyncio.to_thread(delete_video_assets, video_id)


async def process_resumable_video(
    upload_id: UUID,
    video_id: str,
    source: Path,
) -> None:
    """Chạy sau HTTP 202; chỉ một resumable video được convert/upload tại một thời điểm."""
    async with _processing_lock:
        try:
            duration_ms = await asyncio.to_thread(probe_duration_ms, source)
            if duration_ms <= 0:
                raise RuntimeError("File MP4 không có video stream hợp lệ")

            from convert_v2 import convert_one

            converted, note = await asyncio.to_thread(
                convert_one,
                source,
                True,
                video_id=video_id,
                create_thumbnail=False,
            )
            if not converted:
                raise RuntimeError(f"Convert HLS thất bại: {note}")

            from vndata_s3 import upload_hls_assets, verify_video

            await asyncio.to_thread(upload_hls_assets, video_id)
            await asyncio.to_thread(verify_video, video_id)

            updated = await db.pool().execute(
                """
                update videos
                   set status = 'READY', duration_ms = $2
                 where id = $1 and status = 'PROCESSING'
                """,
                video_id,
                duration_ms,
            )
            if updated == "UPDATE 1":
                print(f"[resumable-upload] {video_id} READY: durationMs={duration_ms}")
        except Exception as error:  # mọi lỗi kết thúc rõ ràng bằng FAILED
            await db.pool().execute(
                "update videos set status = 'FAILED' where id = $1 and status = 'PROCESSING'",
                video_id,
            )
            print(f"[resumable-upload] {video_id} FAILED: {error}")
        finally:
            await upload_storage.remove_workspace(upload_id)
            await asyncio.to_thread(remove_local_video_assets, video_id)
            await remove_published_assets_if_video_missing(video_id)
