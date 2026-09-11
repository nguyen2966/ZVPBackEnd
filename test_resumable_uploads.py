"""Focused validation tests cho resumable upload HTTP helpers."""

from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from io import BytesIO

from fastapi import BackgroundTasks, Response, UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from backend.errors import ApiError
from backend.routers.resumable_uploads import (
    _expected_part_size,
    _initialization_response,
    _read_part,
    _read_thumbnail,
    _total_parts,
    complete_upload,
    initialize_upload,
)

from backend.routers.uploads import _save_upload, upload_video
from backend.upload_config import load_max_upload_file_size

MIB = 1024 * 1024


class ResumableUploadTests(unittest.TestCase):
    def test_total_parts_rounds_up(self) -> None:
        self.assertEqual(_total_parts(20 * MIB, 8 * MIB), 3)
        self.assertEqual(_total_parts(8 * MIB, 8 * MIB), 1)

    def test_expected_size_for_regular_and_final_parts(self) -> None:
        self.assertEqual(_expected_part_size(20 * MIB, 8 * MIB, 1), 8 * MIB)
        self.assertEqual(_expected_part_size(20 * MIB, 8 * MIB, 2), 8 * MIB)
        self.assertEqual(_expected_part_size(20 * MIB, 8 * MIB, 3), 4 * MIB)

    def test_invalid_part_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _expected_part_size(20 * MIB, 8 * MIB, 0)
        with self.assertRaises(ValueError):
            _expected_part_size(20 * MIB, 8 * MIB, 4)

    def test_initialization_returns_session_and_video(self) -> None:
        row = {
            "id": "58bb0a14-a26b-4c9f-bae4-645e509eef3f",
            "video_id": "up_123",
            "status": "UPLOADING",
            "part_size": 8 * MIB,
            "title": "Video",
            "caption": "",
            "duration_ms": 0,
            "playback_url": "https://cdn.example/hls/up_123/master.m3u8",
            "thumbnail_url": "https://cdn.example/thumbnails/up_123.jpg",
            "like_count": 0,
            "dislike_count": 0,
            "bookmark_count": 0,
            "creator_id": "creator-1",
            "display_name": "Creator",
            "username": "creator",
            "avatar_url": None,
            "category_name": "Music",
        }

        response = _initialization_response(row, "https://api.example")

        self.assertEqual(response["uploadId"], row["id"])
        self.assertEqual(response["videoId"], row["video_id"])
        self.assertEqual(response["video"]["id"], row["video_id"])
        self.assertEqual(
            response["video"]["thumbnailAsset"]["url"],
            row["thumbnail_url"],
        )


class PartBodyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(*chunks: bytes) -> Request:
        messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]

        async def receive():
            return messages.pop(0)

        return Request(
            {
                "type": "http",
                "method": "PUT",
                "path": "/",
                "headers": [],
            },
            receive,
        )

    async def test_part_body_accepts_multiple_network_chunks(self) -> None:
        content = await _read_part(self.request(b"ab", b"cd"), expected_size=4)
        self.assertEqual(content, b"abcd")

    async def test_part_body_rejects_oversized_chunk(self) -> None:
        with self.assertRaises(ApiError) as context:
            await _read_part(self.request(b"abcde"), expected_size=4)
        self.assertEqual(context.exception.code, "INVALID_PART_SIZE")

    async def test_thumbnail_accepts_jpeg_bytes(self) -> None:
        thumbnail = UploadFile(
            BytesIO(b"\xff\xd8image\xff\xd9"),
            filename="thumbnail.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        self.assertEqual(await _read_thumbnail(thumbnail), b"\xff\xd8image\xff\xd9")

    async def test_thumbnail_rejects_non_jpeg_content(self) -> None:
        thumbnail = UploadFile(
            BytesIO(b"not-a-jpeg"),
            filename="thumbnail.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with self.assertRaises(ApiError):
            await _read_thumbnail(thumbnail)



class UploadFileSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_size_config_does_not_require_camera_settings(self) -> None:
        pool = SimpleNamespace(fetchval=AsyncMock(return_value="524288000"))
        with patch("backend.upload_config.db.pool", return_value=pool):
            self.assertEqual(await load_max_upload_file_size(), 500 * MIB)

    async def test_missing_or_invalid_file_size_config_fails(self) -> None:
        for value in (None, 0, -1, True, 1.5):
            with self.subTest(value=value):
                pool = SimpleNamespace(fetchval=AsyncMock(return_value=value))
                with patch("backend.upload_config.db.pool", return_value=pool):
                    with self.assertRaises(RuntimeError):
                        await load_max_upload_file_size()

    async def test_direct_upload_accepts_size_boundary_and_removes_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "original.mp4"
            upload = UploadFile(BytesIO(b"abcd"), filename="original.mp4")
            self.assertEqual(await _save_upload(upload, destination, 4), 4)
            self.assertEqual(destination.read_bytes(), b"abcd")
            oversized = UploadFile(BytesIO(b"abcde"), filename="original.mp4")
            with self.assertRaises(ApiError) as context:
                await _save_upload(oversized, destination, 4)
            self.assertEqual(context.exception.code, "FILE_TOO_LARGE")
            self.assertFalse(destination.exists())

    async def test_resumable_initialization_rejects_oversized_file(self) -> None:
        with patch("backend.routers.resumable_uploads.load_max_upload_file_size", new=AsyncMock(return_value=4)):
            with self.assertRaises(ApiError) as context:
                await initialize_upload(
                    request=None, response=Response(),
                    uploadId=uuid4(), title="Video", categoryId=1, fileSize=5,
                    caption="", thumbnail=None, principal=SimpleNamespace(user_id=uuid4()),
                )
        self.assertEqual(context.exception.code, "FILE_TOO_LARGE")

    async def test_direct_upload_validates_metadata_without_enforcing_duration_limit(self) -> None:
        for duration_ms in (0, 600_000):
            with self.subTest(duration_ms=duration_ms), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                background = BackgroundTasks()
                response = Response()
                pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"id": 1}), execute=AsyncMock())
                with (
                    patch("backend.routers.uploads.SOURCE_DIR", root),
                    patch("backend.routers.uploads.load_max_upload_file_size", new=AsyncMock(return_value=4)),
                    patch("backend.routers.uploads.db.pool", return_value=pool),
                    patch("backend.routers.uploads.probe_duration_ms", return_value=duration_ms),
                    patch("vndata_s3.S3Settings.from_env", return_value=None),
                    patch("vndata_s3.build_asset_urls", return_value={"hls_url": "hls", "thumbnail_url": "thumbnail"}),
                ):
                    arguments = dict(
                        background=background, response=response,
                        file=UploadFile(BytesIO(b"abcd"), filename="original.mp4"),
                        title="Video", categoryId=1, caption="",
                        principal=SimpleNamespace(user_id=uuid4()),
                    )
                    if duration_ms == 0:
                        with self.assertRaises(ApiError) as context:
                            await upload_video(**arguments)
                        self.assertEqual(context.exception.status_code, 422)
                        self.assertEqual(context.exception.code, "INVALID_METADATA")
                        self.assertEqual(list(root.iterdir()), [])
                        self.assertEqual(background.tasks, [])
                        pool.execute.assert_not_awaited()
                    else:
                        result = await upload_video(**arguments)
                        self.assertEqual(response.status_code, 202)
                        self.assertEqual(result["durationMs"], duration_ms)
                        self.assertEqual(pool.execute.await_args.args[6], duration_ms)
                        self.assertEqual(len(background.tasks), 1)

    async def test_complete_validates_metadata_without_enforcing_duration_limit(self) -> None:
        for duration_ms in (0, 600_000):
            with self.subTest(duration_ms=duration_ms):
                upload_id = uuid4()
                row = {
                    "id": upload_id, "video_id": "up_video", "status": "UPLOADING",
                    "file_size": 4, "part_size": 4,
                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                }
                source = Path("/temporary/original.mp4")
                background = BackgroundTasks()
                response = Response()
                pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"status": "PROCESSING"}))
                with (
                    patch("backend.routers.resumable_uploads._owned_session", new=AsyncMock(return_value=row)),
                    patch("backend.routers.resumable_uploads.db.pool", return_value=pool),
                    patch("backend.routers.resumable_uploads.upload_storage.missing_part_numbers", new=AsyncMock(return_value=[])),
                    patch("backend.routers.resumable_uploads.upload_storage.merge_parts", new=AsyncMock(return_value=source)),
                    patch("backend.routers.resumable_uploads.probe_duration_ms", return_value=duration_ms),
                ):
                    if duration_ms == 0:
                        with self.assertRaises(ApiError) as context:
                            await complete_upload(upload_id, background, response, SimpleNamespace(user_id=uuid4()))
                        self.assertEqual(context.exception.status_code, 422)
                        self.assertEqual(context.exception.code, "INVALID_METADATA")
                        self.assertEqual(background.tasks, [])
                        pool.fetchrow.assert_not_awaited()
                    else:
                        result = await complete_upload(upload_id, background, response, SimpleNamespace(user_id=uuid4()))
                        self.assertEqual(response.status_code, 202)
                        self.assertEqual(result["status"], "PROCESSING")
                        self.assertEqual(len(background.tasks), 1)
                        self.assertEqual(background.tasks[0].args, (upload_id, "up_video", source, duration_ms))


if __name__ == "__main__":
    unittest.main()
