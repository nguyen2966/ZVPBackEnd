"""Focused ownership and cleanup checks for video deletion."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from backend.video_deletion import delete_owned_video


class FakePool:
    def __init__(self, row) -> None:
        self.row = row
        self.executions: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *arguments):
        return self.row

    async def execute(self, query: str, *arguments):
        self.executions.append((query, arguments))
        return "UPDATE 1"

    async def fetchval(self, query: str, *arguments):
        self.executions.append((query, arguments))
        return arguments[0]


class VideoDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_video_is_hidden_and_assets_are_removed(self) -> None:
        upload_id = uuid4()
        pool = FakePool({
            "id": "up_video",
            "status": "READY",
            "upload_id": upload_id,
        })

        with (
            patch("backend.video_deletion.db.pool", return_value=pool),
            patch(
                "backend.video_deletion.upload_storage.remove_workspace",
                new=AsyncMock(),
            ) as remove_workspace,
            patch("backend.video_deletion.remove_local_video_assets") as remove_local,
            patch("vndata_s3.delete_video_assets") as remove_remote,
        ):
            await delete_owned_video("up_video", uuid4())

        self.assertIn("delete from videos", pool.executions[0][0])
        self.assertNotIn("DELETED", pool.executions[0][0])
        remove_workspace.assert_awaited_once_with(upload_id)
        remove_local.assert_called_once_with("up_video")
        remove_remote.assert_called_once_with("up_video")

    async def test_missing_video_is_not_reported_as_deleted(self) -> None:
        pool = FakePool(None)
        with patch("backend.video_deletion.db.pool", return_value=pool):
            await delete_owned_video("missing", uuid4())

        self.assertEqual(pool.executions, [])


if __name__ == "__main__":
    unittest.main()
