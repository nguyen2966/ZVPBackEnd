"""Tạo table session tối thiểu cho resumable video upload.

Chạy:
    python -m backend.migrate_video_upload_sessions
    python -m backend.migrate_video_upload_sessions --apply

Lệnh mặc định chỉ xem trạng thái. Thêm ``--apply`` mới thay đổi database.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from .config import DATABASE_DSN

CREATE_TABLE = """
create table if not exists video_upload_sessions (
  id         uuid primary key,
  video_id   text not null unique references videos(id) on delete cascade,
  file_size  bigint not null check (file_size > 0),
  part_size  integer not null check (part_size > 0),
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
)
"""


async def main() -> None:
    apply = "--apply" in sys.argv
    conn = await asyncpg.connect(DATABASE_DSN)
    try:
        exists = await conn.fetchval(
            """
            select exists (
                select 1
                  from information_schema.tables
                 where table_schema = 'public'
                   and table_name = 'video_upload_sessions'
            )
            """
        )
        print(f"video_upload_sessions tồn tại: {exists}")

        if exists:
            print("Không cần migration.")
            return

        if not apply:
            print("Chưa ghi gì - thêm --apply để tạo table.")
            return

        await conn.execute(CREATE_TABLE)
        print("Đã tạo video_upload_sessions.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
