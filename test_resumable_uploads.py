"""Focused validation tests cho resumable upload HTTP helpers."""

from __future__ import annotations

import unittest
from io import BytesIO

from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from backend.errors import ApiError
from backend.routers.resumable_uploads import (
    _expected_part_size,
    _read_part,
    _read_thumbnail,
    _total_parts,
)

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


if __name__ == "__main__":
    unittest.main()
