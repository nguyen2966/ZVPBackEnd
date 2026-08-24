"""
Chuyển app_config từ một cột `payload jsonb` sang từng row key-value.

Chạy:
    python -m backend.migrate_config_kv            # xem trước, KHÔNG ghi gì
    python -m backend.migrate_config_kv --apply    # thực sự ghi

Chỉ đụng vào app_config; videos/users/sessions/reactions giữ nguyên.

Các bước trong một transaction:
    1. Đọc payload hiện có (nếu cột payload còn tồn tại), không có thì dùng bundle default
    2. Tạo bảng app_config_entries + trigger tự tăng version
    3. Trải payload thành từng row key-value
    4. Bỏ cột payload

Sau khi chạy, sửa một setting chỉ cần:
    update app_config_entries set value = '0.9'::jsonb
     where key = 'ranking.weights.likedChannel';
Trigger sẽ tự tăng app_config.version nên ETag đổi và client biết đường tải lại.
"""

from __future__ import annotations

import asyncio
import json
import sys

import asyncpg

from .config import DATABASE_DSN
from .config_payload import flatten
from .routers.config import DEFAULT_PAYLOAD

CREATE_ENTRIES = """
create table if not exists app_config_entries (
  config_id int   not null references app_config(id) on delete cascade,
  key       text  not null,
  value     jsonb not null,
  primary key (config_id, key)
);

create or replace function app_config_bump_version() returns trigger as $$
declare target int;
begin
  target := coalesce(NEW.config_id, OLD.config_id);
  update app_config
     set version = version + 1, updated_at = now()
   where id = target and updated_at < now();
  return null;
end;
$$ language plpgsql;

drop trigger if exists app_config_entries_bump on app_config_entries;
create trigger app_config_entries_bump
after insert or update or delete on app_config_entries
for each row execute function app_config_bump_version();
"""


async def main() -> None:
    apply = "--apply" in sys.argv
    conn = await asyncpg.connect(DATABASE_DSN)
    try:
        has_payload = await conn.fetchval("""
            select count(*) > 0 from information_schema.columns
             where table_name = 'app_config' and column_name = 'payload'
        """)
        has_entries = await conn.fetchval("""
            select count(*) > 0 from information_schema.tables
             where table_name = 'app_config_entries'
        """)
        print(f"cột app_config.payload tồn tại : {has_payload}")
        print(f"bảng app_config_entries tồn tại: {has_entries}")

        bundles = await conn.fetch("select id, version, enabled from app_config order by id")
        if not bundles:
            raise SystemExit("Chưa có bundle nào trong app_config - chạy `python -m backend.seed` trước.")

        plan: list[tuple[int, dict]] = []
        for b in bundles:
            if has_payload:
                raw = await conn.fetchval("select payload from app_config where id = $1", b["id"])
                payload = json.loads(raw) if isinstance(raw, str) else raw
            else:
                payload = DEFAULT_PAYLOAD
            plan.append((b["id"], flatten(payload)))
            print(f"  bundle id={b['id']} version={b['version']} enabled={b['enabled']} "
                  f"-> {len(plan[-1][1])} setting")

        for _, entries in plan[:1]:
            print("\n  ví dụ key sẽ tạo:")
            for key in list(entries)[:5]:
                print(f"    {key:<34} = {json.dumps(entries[key])}")

        if not apply:
            print("\n(chưa ghi gì - thêm --apply để thực hiện)")
            return

        async with conn.transaction():
            await conn.execute(CREATE_ENTRIES)
            for config_id, entries in plan:
                await conn.execute("delete from app_config_entries where config_id = $1", config_id)
                await conn.executemany(
                    "insert into app_config_entries (config_id, key, value) values ($1, $2, $3::jsonb)",
                    [(config_id, k, json.dumps(v)) for k, v in entries.items()],
                )
            if has_payload:
                await conn.execute("alter table app_config drop column payload")
            # Trigger đã cộng version khi ghi entries; đưa về đúng version cũ để client không
            # bị coi là "config đã đổi" chỉ vì ta đổi cách lưu.
            for b in bundles:
                await conn.execute("update app_config set version = $2 where id = $1",
                                   b["id"], b["version"])

        total = await conn.fetchval("select count(*) from app_config_entries")
        print(f"\nXong. {total} row trong app_config_entries; cột payload đã bỏ.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
