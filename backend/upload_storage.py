"""Filesystem operations cho resumable upload.

Module này không biết HTTP, database, video metadata hay VNData. Mỗi operation disk chạy qua
``asyncio.to_thread`` để không block event loop của FastAPI.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from uuid import UUID, uuid4

PART_FILE_PATTERN = re.compile(r"^([1-9][0-9]*)\.part$")


class UploadStorage:
    def __init__(self, root: Path):
        self._root = root.expanduser().resolve()

    async def create_workspace(self, upload_id: UUID) -> None:
        await asyncio.to_thread(self._create_workspace, upload_id)

    async def write_part(
        self,
        upload_id: UUID,
        part_number: int,
        expected_size: int,
        content: bytes,
    ) -> None:
        await asyncio.to_thread(
            self._write_part,
            upload_id,
            part_number,
            expected_size,
            content,
        )

    async def uploaded_part_numbers(self, upload_id: UUID) -> list[int]:
        return await asyncio.to_thread(self._uploaded_part_numbers, upload_id)

    async def missing_part_numbers(
        self,
        upload_id: UUID,
        total_parts: int,
    ) -> list[int]:
        if total_parts <= 0:
            raise ValueError("total_parts phải lớn hơn 0")

        uploaded = set(await self.uploaded_part_numbers(upload_id))
        return [number for number in range(1, total_parts + 1) if number not in uploaded]

    async def merge_parts(
        self,
        upload_id: UUID,
        total_parts: int,
        expected_file_size: int,
    ) -> Path:
        return await asyncio.to_thread(
            self._merge_parts,
            upload_id,
            total_parts,
            expected_file_size,
        )

    async def remove_workspace(self, upload_id: UUID) -> None:
        await asyncio.to_thread(self._remove_workspace, upload_id)

    def _workspace(self, upload_id: UUID) -> Path:
        # UUID được parse trước khi tạo path nên input không thể chèn `..` hoặc path separator.
        return self._root / str(upload_id)

    def _parts_directory(self, upload_id: UUID) -> Path:
        return self._workspace(upload_id) / "parts"

    def _part_path(self, upload_id: UUID, part_number: int) -> Path:
        if part_number <= 0:
            raise ValueError("part_number phải lớn hơn 0")
        return self._parts_directory(upload_id) / f"{part_number}.part"

    def _create_workspace(self, upload_id: UUID) -> None:
        self._parts_directory(upload_id).mkdir(parents=True, exist_ok=True)

    def _remove_workspace(self, upload_id: UUID) -> None:
        workspace = self._workspace(upload_id)
        if workspace.exists():
            shutil.rmtree(workspace)

    def _write_part(
        self,
        upload_id: UUID,
        part_number: int,
        expected_size: int,
        content: bytes,
    ) -> None:
        if expected_size <= 0:
            raise ValueError("expected_size phải lớn hơn 0")
        if len(content) != expected_size:
            raise ValueError(
                f"Part {part_number} sai kích thước: expected={expected_size}, actual={len(content)}"
            )

        part_path = self._part_path(upload_id, part_number)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = part_path.parent / f".{part_number}.{uuid4()}.tmp"

        try:
            with temporary_path.open("wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, part_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _uploaded_part_numbers(self, upload_id: UUID) -> list[int]:
        parts_directory = self._parts_directory(upload_id)
        if not parts_directory.exists():
            return []

        numbers = []
        for path in parts_directory.iterdir():
            match = PART_FILE_PATTERN.fullmatch(path.name)
            if path.is_file() and match:
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def _merge_parts(
        self,
        upload_id: UUID,
        total_parts: int,
        expected_file_size: int,
    ) -> Path:
        if total_parts <= 0:
            raise ValueError("total_parts phải lớn hơn 0")
        if expected_file_size <= 0:
            raise ValueError("expected_file_size phải lớn hơn 0")

        workspace = self._workspace(upload_id)
        workspace.mkdir(parents=True, exist_ok=True)
        destination = workspace / "original.mp4"
        temporary_path = workspace / f".original.{uuid4()}.tmp"

        try:
            with temporary_path.open("wb") as output:
                for part_number in range(1, total_parts + 1):
                    part_path = self._part_path(upload_id, part_number)
                    if not part_path.is_file():
                        raise FileNotFoundError(f"Thiếu part {part_number}")
                    with part_path.open("rb") as source:
                        shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())

            actual_size = temporary_path.stat().st_size
            if actual_size != expected_file_size:
                raise ValueError(
                    f"File merge sai kích thước: expected={expected_file_size}, actual={actual_size}"
                )

            os.replace(temporary_path, destination)
            return destination
        finally:
            temporary_path.unlink(missing_ok=True)
