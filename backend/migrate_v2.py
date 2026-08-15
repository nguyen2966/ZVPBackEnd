"""
Chuyển pool video trong DB sang bộ 200 video thật trên VNDATA S3 (feed_items_v2.json).

Chạy:
    python -m backend.migrate_v2            # xem trước, KHÔNG ghi gì
    python -m backend.migrate_v2 --apply    # thực sự ghi

Script làm 3 việc, trong một transaction:
    1. Cập nhật 100 video sẵn có: playback_url/thumbnail_url trỏ sang URL S3 tuyệt đối
    2. Thêm 100 video mới (batch_2) và gán creator sao cho số video mỗi creator vẫn đều
    3. Xoá các row không nằm trong bộ 200 - tức 100 row nhân bản `<id>_2` của lần seed trước

Vì sao không seed lại từ đầu:
    seed lại sẽ xoá luôn users/sessions/reactions. Script này giữ nguyên chúng, chỉ những
    reaction trỏ tới row nhân bản bị mất (do FK on delete cascade) - đúng như mong muốn,
    vì bản thân các row đó là dữ liệu rác cần bỏ.

Sau khi chạy, API không cần sửa gì: serializer đã cho URL tuyệt đối đi thẳng, chỉ path
tương đối mới được ghép base URL của request.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

import asyncpg

from .config import BASE_DIR, DATABASE_DSN

FEED_V2 = BASE_DIR / "feed_items_v2.json"

# video_id nằm trong key S3: .../hls/<video_id>/master.m3u8
VIDEO_ID_FROM_S3 = re.compile(r"/hls/([^/]+)/master\.m3u8")


def load_v2_items() -> list[dict[str, Any]]:
    if not FEED_V2.exists():
        raise SystemExit(f"Không tìm thấy {FEED_V2} - chạy pipeline_v2.py trước.")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in json.loads(FEED_V2.read_text(encoding="utf-8")):
        v = entry["video"]
        playback = v["playbackAsset"]["url"]
        match = VIDEO_ID_FROM_S3.search(playback)
        if not match:
            raise SystemExit(f"URL không đúng dạng S3, không suy được video_id: {playback}")
        video_id = match.group(1)
        if video_id in seen:
            raise SystemExit(f"Trùng video_id trong {FEED_V2.name}: {video_id}")
        seen.add(video_id)
        out.append({
            "id": video_id,
            "title": v.get("title") or video_id,
            "caption": v.get("caption") or "",
            "duration_ms": int(v.get("durationMs") or 0),
            "category": (v.get("category") or {}).get("name") or "Uncategorized",
            "playback_url": playback,
            "thumbnail_url": v["thumbnailAsset"]["url"],
        })
    return out


async def run(conn: asyncpg.Connection, items: list[dict[str, Any]], apply: bool) -> None:
    target_ids = [i["id"] for i in items]

    # ---- Category: dùng lại theo tên, thêm mới nếu xuất hiện category lạ ----
    category_id: dict[str, int] = {
        r["name"]: r["id"] for r in await conn.fetch("select id, name from categories")
    }
    for name in sorted({i["category"] for i in items}):
        if name not in category_id:
            if apply:
                category_id[name] = await conn.fetchval(
                    "insert into categories (name) values ($1) returning id", name
                )
            print(f"  + category mới: {name}")

    # ---- Creator: giữ nguyên bộ creator đang có (đã gom còn 15 để ranking client hoạt động) ----
    creators = [r["id"] for r in await conn.fetch(
        "select id from users where id in (select distinct creator_id from videos) order by id"
    )]
    if not creators:
        raise SystemExit("DB chưa có creator nào - chạy `python -m backend.seed` trước.")

    existing = {r["id"] for r in await conn.fetch("select id from videos")}
    to_update = [i for i in items if i["id"] in existing]
    to_insert = [i for i in items if i["id"] not in existing]
    to_delete = sorted(existing - set(target_ids))

    print(f"  cập nhật URL sang S3 : {len(to_update)}")
    print(f"  thêm video mới       : {len(to_insert)}")
    print(f"  xoá row thừa         : {len(to_delete)}  (vd: {to_delete[:3]})")

    lost = await conn.fetchval(
        "select count(*) from reactions where video_id = any($1::text[])", to_delete
    ) if to_delete else 0
    print(f"  reaction bị xoá theo : {lost}")

    if not apply:
        print("\n(chưa ghi gì - thêm --apply để thực hiện)")
        return

    # Gán creator cho video mới theo kiểu "ai đang ít video nhất thì nhận trước", để pool giữ
    # được mật độ ~13-14 video/creator mà SPEC mục 6 cần cho tiêu chí "channel xem nhiều nhất".
    load = {cid: 0 for cid in creators}
    for r in await conn.fetch(
        "select creator_id, count(*) n from videos where id = any($1::text[]) group by 1", target_ids
    ):
        if r["creator_id"] in load:
            load[r["creator_id"]] = r["n"]

    await conn.executemany(
        """
        update videos
           set playback_url = $2, thumbnail_url = $3, title = $4, caption = $5,
               duration_ms = $6, category_id = $7, status = 'READY'
         where id = $1
        """,
        [(i["id"], i["playback_url"], i["thumbnail_url"], i["title"], i["caption"],
          i["duration_ms"], category_id[i["category"]]) for i in to_update],
    )

    rows = []
    for item in to_insert:
        creator = min(load, key=lambda c: load[c])
        load[creator] += 1
        rows.append((item["id"], creator, category_id[item["category"]], item["title"],
                     item["caption"], item["duration_ms"], item["playback_url"],
                     item["thumbnail_url"], "READY"))
    if rows:
        await conn.executemany(
            """
            insert into videos (id, creator_id, category_id, title, caption, duration_ms,
                                playback_url, thumbnail_url, status)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            rows,
        )

    if to_delete:
        await conn.execute("delete from videos where id = any($1::text[])", to_delete)


async def main() -> None:
    apply = "--apply" in sys.argv
    items = load_v2_items()
    print(f"Nguồn: {len(items)} video trong {FEED_V2.name}\n")

    conn = await asyncpg.connect(DATABASE_DSN)
    try:
        async with conn.transaction():
            await run(conn, items, apply)

        if apply:
            total = await conn.fetchval("select count(*) from videos")
            ready = await conn.fetchval("select count(*) from videos where status = 'READY'")
            s3 = await conn.fetchval("select count(*) from videos where playback_url like 'https://%'")
            print(f"\nXong. {total} video ({ready} READY), {s3} video trỏ tới S3.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
