"""
Tạo schema và nạp seed data (SPEC mục 6).

Chạy:
    python -m backend.seed            # tạo lại schema + seed
    python -m backend.seed --data     # chỉ nạp lại data, giữ schema

CẢNH BÁO: chế độ mặc định DROP toàn bộ bảng trong schema.sql rồi tạo lại. Dùng cho dev/demo.

Nguồn dữ liệu: feed_items.json (metadata thật lấy từ YouTube) + asset HLS trong public/hls
do convert.py sinh ra.

Ba điều chỉnh có chủ đích so với dữ liệu gốc, để pool thoả yêu cầu ranking của SPEC mục 6:

1. Creator gom còn CREATOR_COUNT người (dữ liệu gốc có 90 uploader khác nhau).
   SPEC cần 10-20 creator, mỗi người ~10-20 video, nếu không tiêu chí "channel xem nhiều nhất"
   của client sẽ không bao giờ kích hoạt. Vì vậy video được gán creator theo vòng tròn,
   không giữ đúng uploader gốc.

2. Pool nhân lên TARGET_VIDEO_COUNT row từ 100 asset có thật, nên mỗi asset HLS xuất hiện ở
   2 video row với id khác nhau. SPEC chốt pool 200; ta chỉ có 100 video thật.
   Muốn pool đúng bằng số video thật thì đặt TARGET_VIDEO_COUNT = 100.

3. duration_ms lấy đúng độ dài thật của media (8s-180s) chứ không ép vào khoảng 15-60s mà
   SPEC gợi ý: client tính completion rate = watchedMs / durationMs nên số này SAI sẽ làm
   hỏng mọi signal ranking. Độ chính xác quan trọng hơn việc khớp khoảng đề xuất.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

from .config import BASE_DIR, DATABASE_DSN
from .routers.config import DEFAULT_PAYLOAD
from .security import hash_password

FEED_ITEMS = BASE_DIR / "feed_items.json"
HLS_DIR = BASE_DIR / "public" / "hls"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"

TARGET_VIDEO_COUNT = 200     # SPEC mục 6
CREATOR_COUNT = 15           # SPEC mục 6: 10-20 creator

# User test để client đăng nhập ngay (SPEC mục 6).
TEST_USERS = [
    ("khoa", "Khoa Nguyen", "password123"),
    ("demo", "Demo User", "password123"),
]

VIDEO_ID_RE = re.compile(r"/([^/]+)\.m3u8$")


def load_source_items() -> list[dict]:
    """Đọc feed_items.json, chỉ giữ item thực sự có asset HLS trên đĩa."""
    if not FEED_ITEMS.exists():
        raise SystemExit(f"Không tìm thấy {FEED_ITEMS} - chạy dowload.py trước.")

    items = json.loads(FEED_ITEMS.read_text(encoding="utf-8"))
    available = {p.parent.name for p in HLS_DIR.glob("*/master.m3u8")}

    out = []
    for entry in items:
        v = entry["video"]
        match = VIDEO_ID_RE.search(v["playbackAsset"]["url"])
        if not match:
            continue
        asset_id = match.group(1)
        if asset_id not in available:
            # Không có HLS -> bỏ hẳn. Video thiếu playback_url sẽ biến mất khỏi feed của
            # client mà không có lỗi nào, nên tuyệt đối không seed row như vậy.
            continue
        out.append({
            "asset_id": asset_id,
            "title": v.get("title") or asset_id,
            "caption": v.get("caption") or "",
            "duration_ms": int(v.get("durationMs") or 0),
            "category": (v.get("category") or {}).get("name") or "Uncategorized",
            "uploader": (v.get("user") or {}).get("displayName") or "Unknown",
            "avatar_url": (v.get("user") or {}).get("avatarUrl"),
        })
    if not out:
        raise SystemExit("Không có item nào vừa có metadata vừa có asset HLS.")
    return out


async def migrate(conn: asyncpg.Connection) -> None:
    print("→ Tạo lại schema từ backend/schema.sql ...")
    await conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    print("  schema OK")


async def clear_data(conn: asyncpg.Connection) -> None:
    """Xoá data nhưng giữ schema (chế độ --data)."""
    await conn.execute("truncate reactions, videos, categories, sessions, users, app_config restart identity cascade")


async def seed(conn: asyncpg.Connection) -> None:
    items = load_source_items()
    print(f"→ Nguồn: {len(items)} video có đủ metadata + asset HLS")

    # ---- Categories ----
    names = sorted({i["category"] for i in items})
    category_id: dict[str, int] = {}
    for name in names:
        category_id[name] = await conn.fetchval(
            "insert into categories (name) values ($1) returning id", name
        )
    print(f"→ {len(category_id)} category: {', '.join(names)}")

    # ---- Creators: gom về CREATOR_COUNT người, lấy tên uploader có thật cho giống thật ----
    seen: list[dict] = []
    for item in items:
        if item["uploader"] not in [s["uploader"] for s in seen]:
            seen.append(item)
        if len(seen) == CREATOR_COUNT:
            break

    creator_ids: list[str] = []
    used_usernames: set[str] = set()
    for idx, src in enumerate(seen):
        base = re.sub(r"[^a-z0-9]+", "", src["uploader"].lower()) or "creator"
        username = base[:24]
        while username in used_usernames:            # tên uploader có thể trùng sau khi normalize
            username = f"{base[:20]}{idx}"
        used_usernames.add(username)
        creator_ids.append(await conn.fetchval(
            """
            insert into users (username, display_name, avatar_url, password_hash)
            values ($1, $2, $3, $4) returning id
            """,
            username, src["uploader"], src["avatar_url"], hash_password("password123"),
        ))
    print(f"→ {len(creator_ids)} creator")

    # ---- User test ----
    for username, display_name, password in TEST_USERS:
        await conn.execute(
            """
            insert into users (username, display_name, avatar_url, password_hash)
            values ($1, $2, $3, $4)
            """,
            username, display_name, seen[0]["avatar_url"], hash_password(password),
        )
    print(f"→ {len(TEST_USERS)} user test: {', '.join(u for u, _, _ in TEST_USERS)} (mật khẩu: password123)")

    # ---- Videos ----
    rows = []
    for i in range(TARGET_VIDEO_COUNT):
        src = items[i % len(items)]
        copy_index = i // len(items)
        # Vòng thứ 2 trở đi dùng lại asset nhưng phải có id riêng (id là khoá của reaction).
        video_id = src["asset_id"] if copy_index == 0 else f"{src['asset_id']}_{copy_index + 1}"
        rows.append((
            video_id,
            creator_ids[i % len(creator_ids)],
            category_id[src["category"]],
            src["title"],
            src["caption"],
            src["duration_ms"],
            # Lưu PATH, API ghép base URL lúc trả response - xem comment trong schema.sql
            f"/video/upload/sp_auto/{src['asset_id']}.m3u8",
            f"/video/upload/so_auto/{src['asset_id']}.jpg",
            "READY",
        ))

    await conn.executemany(
        """
        insert into videos (id, creator_id, category_id, title, caption, duration_ms,
                            playback_url, thumbnail_url, status)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        rows,
    )
    print(f"→ {len(rows)} video status=READY "
          f"({len(items)} asset thật, mỗi asset dùng {TARGET_VIDEO_COUNT // len(items)} lần)")

    # ---- Config bundle (SPEC mục 5) ----
    await conn.execute(
        "insert into app_config (version, payload, enabled) values (1, $1::jsonb, true)",
        json.dumps(DEFAULT_PAYLOAD),
    )
    print("→ app_config version=1 enabled")


async def main() -> None:
    data_only = "--data" in sys.argv
    conn = await asyncpg.connect(DATABASE_DSN)
    try:
        if data_only:
            print("→ Chế độ --data: giữ schema, xoá và nạp lại data")
            await clear_data(conn)
        else:
            await migrate(conn)
        await seed(conn)

        videos = await conn.fetchval("select count(*) from videos where status = 'READY'")
        users = await conn.fetchval("select count(*) from users")
        print(f"\nXong. {videos} video READY, {users} user.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
