"""Validate, convert và publish một resumable video sau khi nhận đủ part."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

from . import db
from .config import UPLOAD_STORAGE_DIR
from .upload_storage import UploadStorage

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


def _remove_generated_assets(video_id: str) -> None:
    from convert_v2 import HLS_DIR, THUMB_DIR

    shutil.rmtree(HLS_DIR / video_id, ignore_errors=True)
    (THUMB_DIR / f"{video_id}.jpg").unlink(missing_ok=True)


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

            await db.pool().execute(
                """
                update videos
                   set status = 'READY', duration_ms = $2
                 where id = $1 and status = 'PROCESSING'
                """,
                video_id,
                duration_ms,
            )
            print(f"[resumable-upload] {video_id} READY: durationMs={duration_ms}")
        except Exception as error:  # mọi lỗi kết thúc rõ ràng bằng FAILED
            await db.pool().execute(
                "update videos set status = 'FAILED' where id = $1 and status = 'PROCESSING'",
                video_id,
            )
            print(f"[resumable-upload] {video_id} FAILED: {error}")
        finally:
            await upload_storage.remove_workspace(upload_id)
            await asyncio.to_thread(_remove_generated_assets, video_id)
