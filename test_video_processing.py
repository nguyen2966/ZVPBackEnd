"""Focused Phase 3 tests: processing state and HLS-only upload plan."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import convert_v2
import vndata_s3
from backend.video_processing import process_resumable_video


class FakePool:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *arguments):
        self.executions.append((query, arguments))
        return "UPDATE 1"


class VideoProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_marks_ready_and_cleans_local_files(self) -> None:
        pool = FakePool()
        upload_id = uuid4()
        source = Path("/temporary/original.mp4")

        with (
            patch("backend.video_processing.db.pool", return_value=pool),
            patch("backend.video_processing.probe_duration_ms", return_value=12_345),
            patch("convert_v2.convert_one", return_value=(True, "converted")) as convert,
            patch("vndata_s3.upload_hls_assets") as upload_hls,
            patch("vndata_s3.verify_video") as verify,
            patch(
                "backend.video_processing.upload_storage.remove_workspace",
                new=AsyncMock(),
            ) as remove_workspace,
            patch("backend.video_processing._remove_generated_assets") as remove_assets,
        ):
            await process_resumable_video(upload_id, "up_video", source)

        convert.assert_called_once_with(
            source,
            True,
            video_id="up_video",
            create_thumbnail=False,
        )
        upload_hls.assert_called_once_with("up_video")
        verify.assert_called_once_with("up_video")
        self.assertIn("status = 'READY'", pool.executions[0][0])
        self.assertEqual(pool.executions[0][1], ("up_video", 12_345))
        remove_workspace.assert_awaited_once_with(upload_id)
        remove_assets.assert_called_once_with("up_video")

    async def test_invalid_video_marks_failed_and_still_cleans(self) -> None:
        pool = FakePool()
        upload_id = uuid4()

        with (
            patch("backend.video_processing.db.pool", return_value=pool),
            patch("backend.video_processing.probe_duration_ms", return_value=0),
            patch(
                "backend.video_processing.upload_storage.remove_workspace",
                new=AsyncMock(),
            ) as remove_workspace,
            patch("backend.video_processing._remove_generated_assets") as remove_assets,
        ):
            await process_resumable_video(
                upload_id,
                "up_invalid",
                Path("/temporary/original.mp4"),
            )

        self.assertIn("status = 'FAILED'", pool.executions[0][0])
        remove_workspace.assert_awaited_once_with(upload_id)
        remove_assets.assert_called_once_with("up_invalid")


class HLSUploadPlanTests(unittest.TestCase):
    def test_resumable_plan_excludes_thumbnail_and_keeps_master_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hls_root = root / "hls"
            video_dir = hls_root / "up_video"
            variant = video_dir / "pg_3"
            variant.mkdir(parents=True)
            (variant / "up_video.mp4dv").write_bytes(b"segment")
            (variant / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            (video_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

            with patch.object(vndata_s3, "HLS_DIR", hls_root):
                plan = vndata_s3.hls_upload_plan("up_video")

        keys = [key for _, key, _ in plan]
        self.assertFalse(any(key.startswith("thumbnails/") for key in keys))
        self.assertEqual(keys[-1], "hls/up_video/master.m3u8")

    def test_legacy_plan_still_contains_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hls_root = root / "hls"
            thumb_root = root / "thumbnails"
            video_dir = hls_root / "legacy"
            variant = video_dir / "pg_3"
            variant.mkdir(parents=True)
            thumb_root.mkdir()
            (variant / "legacy.mp4dv").write_bytes(b"segment")
            (variant / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            (video_dir / "master.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            (thumb_root / "legacy.jpg").write_bytes(b"thumbnail")

            with (
                patch.object(vndata_s3, "HLS_DIR", hls_root),
                patch.object(vndata_s3, "THUMB_DIR", thumb_root),
            ):
                plan = vndata_s3.video_upload_plan("legacy")

        keys = [key for _, key, _ in plan]
        self.assertIn("thumbnails/legacy.jpg", keys)
        self.assertEqual(keys[-1], "hls/legacy/master.m3u8")


if __name__ == "__main__":
    unittest.main()
