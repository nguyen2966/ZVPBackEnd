"""Delete owned videos and their persisted upload assets."""

from __future__ import annotations

import asyncio
import shutil
from uuid import UUID

from . import db
from .config import BASE_DIR, UPLOAD_STORAGE_DIR
from .upload_storage import UploadStorage

upload_storage = UploadStorage(UPLOAD_STORAGE_DIR)

_VIDEO_FOR_DELETE_SQL = """
select v.id, v.status, s.id as upload_id
  from videos v
  left join video_upload_sessions s on s.video_id = v.id
 where v.id = $1 and v.creator_id = $2
"""


def remove_local_video_assets(video_id: str) -> None:
    from convert_v2 import HLS_DIR, THUMB_DIR

    shutil.rmtree(HLS_DIR / video_id, ignore_errors=True)
    (THUMB_DIR / f"{video_id}.jpg").unlink(missing_ok=True)
    (BASE_DIR / "downloads_v2" / f"{video_id}.mp4").unlink(missing_ok=True)


async def _remove_assets(row) -> None:
    video_id = row["id"]
    upload_id = row["upload_id"]

    if upload_id is not None:
        await upload_storage.remove_workspace(upload_id)

    await asyncio.to_thread(remove_local_video_assets, video_id)

    from vndata_s3 import delete_video_assets

    await asyncio.to_thread(delete_video_assets, video_id)


async def delete_owned_video(video_id: str, user_id: UUID) -> None:
    row = await db.pool().fetchrow(
        _VIDEO_FOR_DELETE_SQL,
        video_id,
        user_id,
    )
    if row is None:
        return

    # Keep the row available for an authenticated retry until the first cleanup
    # succeeds. Deleting it then cascades to its upload session and reactions.
    await _remove_assets(row)
    deleted_id = await db.pool().fetchval(
        "delete from videos where id = $1 and creator_id = $2 returning id",
        video_id,
        user_id,
    )
    if deleted_id is None:
        return

    # A processor that was already publishing may have raced with the first cleanup.
    if row["status"] == "PROCESSING":
        await _remove_assets(row)
