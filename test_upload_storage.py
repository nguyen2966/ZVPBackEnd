"""Focused filesystem tests cho backend.upload_storage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.upload_storage import UploadStorage


class UploadStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.storage = UploadStorage(self.root)
        self.upload_id = uuid4()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_write_list_merge_and_remove(self) -> None:
        await self.storage.create_workspace(self.upload_id)

        await self.storage.write_part(self.upload_id, 1, 3, b"abc")
        await self.storage.write_part(self.upload_id, 2, 2, b"de")

        self.assertEqual(
            await self.storage.uploaded_part_numbers(self.upload_id),
            [1, 2],
        )
        self.assertEqual(
            await self.storage.missing_part_numbers(self.upload_id, total_parts=3),
            [3],
        )

        await self.storage.write_part(self.upload_id, 2, 2, b"fg")
        merged = await self.storage.merge_parts(
            self.upload_id,
            total_parts=2,
            expected_file_size=5,
        )
        self.assertEqual(merged.read_bytes(), b"abcfg")

        await self.storage.remove_workspace(self.upload_id)
        self.assertFalse((self.root / str(self.upload_id)).exists())

    async def test_incorrect_part_size_is_rejected_without_file(self) -> None:
        with self.assertRaises(ValueError):
            await self.storage.write_part(self.upload_id, 1, 4, b"abc")

        self.assertEqual(
            await self.storage.uploaded_part_numbers(self.upload_id),
            [],
        )

    async def test_invalid_part_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.storage.write_part(self.upload_id, 0, 3, b"abc")

    async def test_merge_rejects_missing_part_and_removes_temporary_file(self) -> None:
        await self.storage.write_part(self.upload_id, 1, 3, b"abc")

        with self.assertRaises(FileNotFoundError):
            await self.storage.merge_parts(
                self.upload_id,
                total_parts=2,
                expected_file_size=6,
            )

        workspace = self.root / str(self.upload_id)
        self.assertFalse((workspace / "original.mp4").exists())
        self.assertEqual(list(workspace.glob(".original.*.tmp")), [])

    async def test_merge_rejects_incorrect_final_size(self) -> None:
        await self.storage.write_part(self.upload_id, 1, 3, b"abc")

        with self.assertRaises(ValueError):
            await self.storage.merge_parts(
                self.upload_id,
                total_parts=1,
                expected_file_size=4,
            )

        workspace = self.root / str(self.upload_id)
        self.assertFalse((workspace / "original.mp4").exists())
        self.assertEqual(list(workspace.glob(".original.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
