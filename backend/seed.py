"""
Tạo schema và nạp seed data (SPEC mục 6).

Chạy:
    python -m backend.seed            # tạo lại schema + seed
    python -m backend.seed --data     # chỉ nạp lại data, giữ schema

CẢNH BÁO: chế độ mặc định DROP toàn bộ bảng trong schema.sql rồi tạo lại. Dùng cho dev/demo.

Nguồn dữ liệu: feed_items_v2.json - 200 video thật đã convert và upload lên VNDATA S3 bằng
pipeline_v2.py. playback_url/thumbnail_url là URL S3 TUYỆT ĐỐI, không phải path local.

Hai điều chỉnh có chủ đích so với dữ liệu gốc, để pool thoả yêu cầu ranking của SPEC mục 6:

1. Creator gom còn CREATOR_COUNT người (dữ liệu gốc có ~180 uploader khác nhau).
   SPEC cần 10-20 creator, mỗi người ~10-20 video, nếu không tiêu chí "channel xem nhiều nhất"
   của client sẽ không bao giờ kích hoạt. Vì vậy video được gán creator theo vòng tròn,
   không giữ đúng uploader gốc.

2. duration_ms lấy đúng độ dài thật của media (8s-180s) chứ không ép vào khoảng 15-60s mà
   SPEC gợi ý: client tính completion rate = watchedMs / durationMs nên số này SAI sẽ làm
   hỏng mọi signal ranking. Độ chính xác quan trọng hơn việc khớp khoảng đề xuất.

Muốn đổi pool đang chạy mà KHÔNG mất users/sessions/reactions thì dùng
`python -m backend.migrate_v2 --apply` thay vì seed lại.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from pathlib import Path

import asyncpg

from .config import BASE_DIR, DATABASE_DSN, AVATAR_POOL
from .config_payload import flatten
from .routers.config import DEFAULT_PAYLOAD
from .seed_engagement import counts_for
from .security import hash_password

FEED_ITEMS = BASE_DIR / "feed_items_v2.json"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"

CREATOR_COUNT = 15           # SPEC mục 6: 10-20 creator

# User test để client đăng nhập ngay (SPEC mục 6).
TEST_USERS = [
    ("khoa", "Khoa Nguyen", "password123"),
    ("demo", "Demo User", "password123"),
]

# video_id nằm trong key S3: .../hls/<video_id>/master.m3u8
VIDEO_ID_RE = re.compile(r"/hls/([^/]+)/master\.m3u8")


def load_source_items() -> list[dict]:
    """Đọc feed_items_v2.json; mỗi entry là một video thật đã nằm trên S3."""
    if not FEED_ITEMS.exists():
        raise SystemExit(f"Không tìm thấy {FEED_ITEMS} - chạy pipeline_v2.py trước.")

    out, seen = [], set()
    for entry in json.loads(FEED_ITEMS.read_text(encoding="utf-8")):
        v = entry["video"]
        playback = v["playbackAsset"]["url"]
        match = VIDEO_ID_RE.search(playback)
        if not match:
            # Không suy được video_id -> bỏ hẳn. Video thiếu playback_url hợp lệ sẽ biến mất
            # khỏi feed của client mà không có lỗi nào, nên tuyệt đối không seed row như vậy.
            continue
        video_id = match.group(1)
        if video_id in seen:
            raise SystemExit(f"Trùng video_id trong {FEED_ITEMS.name}: {video_id}")
        seen.add(video_id)
        out.append({
            "asset_id": video_id,
            "title": v.get("title") or video_id,
            "caption": v.get("caption") or "",
            "duration_ms": int(v.get("durationMs") or 0),
            "category": (v.get("category") or {}).get("name") or "Uncategorized",
            "uploader": (v.get("user") or {}).get("displayName") or "Unknown",
            "avatar_url": (v.get("user") or {}).get("avatarUrl"),
            "playback_url": playback,
            "thumbnail_url": v["thumbnailAsset"]["url"],
            "like_count": int((v.get("engagement") or {}).get("likeCount") or 0),
        })
    if not out:
        raise SystemExit(f"Không đọc được video nào từ {FEED_ITEMS.name}.")
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
    random.shuffle(AVATAR_POOL)
    for idx, src in enumerate(seen):
        base = re.sub(r"[^a-z0-9]+", "", src["uploader"].lower()) or "creator"
        username = base[:24]
        while username in used_usernames:            # tên uploader có thể trùng sau khi normalize
            username = f"{base[:20]}{idx}"
        used_usernames.add(username)
        avatar_url = AVATAR_POOL[idx % len(AVATAR_POOL)]
        creator_ids.append(await conn.fetchval(
            """
            insert into users (username, display_name, avatar_url, password_hash)
            values ($1, $2, $3, $4) returning id
            """,
            username, src["uploader"], avatar_url, hash_password("password123"),
        ))
    print(f"→ {len(creator_ids)} creator")

    # ---- User test ----
    for i, (username, display_name, password) in enumerate(TEST_USERS):
        avatar_url = AVATAR_POOL[(len(creator_ids) + i + 1) % len(AVATAR_POOL)]
        await conn.execute(
            """
            insert into users (username, display_name, avatar_url, password_hash)
            values ($1, $2, $3, $4)
            """,
            username, display_name, avatar_url, hash_password(password),
        )
    print(f"→ {len(TEST_USERS)} user test: {', '.join(u for u, _, _ in TEST_USERS)} (mật khẩu: password123)")

    # ---- Videos: một row cho mỗi video thật, không nhân bản ----
    # Counter engagement được nạp luôn ở đây (xem seed_engagement.py để biết cách suy số):
    # nếu để 0, feed sẽ trông như app chưa có ai dùng.
    rows = [
        (
            src["asset_id"],
            creator_ids[i % len(creator_ids)],
            category_id[src["category"]],
            src["title"],
            src["caption"],
            src["duration_ms"],
            # URL S3 tuyệt đối; serializer cho URL tuyệt đối đi thẳng, không ghép base URL.
            src["playback_url"],
            src["thumbnail_url"],
            "READY",
            *counts_for(src["like_count"]),
        )
        for i, src in enumerate(items)
    ]

    await conn.executemany(
        """
        insert into videos (id, creator_id, category_id, title, caption, duration_ms,
                            playback_url, thumbnail_url, status,
                            like_count, dislike_count, bookmark_count)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        rows,
    )
    likes = sorted(r[9] for r in rows)
    print(f"→ {len(rows)} video status=READY (mỗi video là một asset riêng trên S3)")
    print(f"   engagement: like median={likes[len(likes)//2]:,} max={likes[-1]:,}")

    # ---- Config bundle (SPEC mục 5): mỗi setting một row key-value ----
    config_id = await conn.fetchval(
        "insert into app_config (version, enabled) values (1, true) returning id"
    )
    entries = flatten(DEFAULT_PAYLOAD)
    await conn.executemany(
        "insert into app_config_entries (config_id, key, value) values ($1, $2, $3::jsonb)",
        [(config_id, key, json.dumps(value)) for key, value in entries.items()],
    )
    # Trigger app_config_entries_bump đã +1 version khi ghi entries -> đặt lại về 1 cho gọn.
    await conn.execute("update app_config set version = 1 where id = $1", config_id)
    print(f"→ app_config version=1 enabled ({len(entries)} setting dạng key-value)")


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
