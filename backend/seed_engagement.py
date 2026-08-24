"""
Nạp số liệu engagement khởi tạo cho bảng videos.

Chạy:
    python -m backend.seed_engagement            # xem trước, KHÔNG ghi gì
    python -m backend.seed_engagement --apply    # thực sự ghi
    python -m backend.seed_engagement --apply --seed 42   # cố định để tái lập kết quả

Vì sao cần: counter trong DB do trigger cập nhật từ bảng reactions, mà DB mới seed thì chưa
có reaction nào -> mọi video hiện 0 like/dislike/bookmark, feed trông như app chưa ai dùng.

Nguồn số liệu:
    likeCount     - lấy ĐÚNG số like thật từ YouTube trong feed_items_v2.json (median ~32K,
                    cao nhất ~12 triệu). Thật hơn nhiều so với random thuần.
    dislikeCount  - YouTube đã ẩn dislike nên không có số thật -> suy ra ngẫu nhiên theo
                    tỉ lệ DISLIKE_RATIO của like.
    bookmarkCount - không có khái niệm tương đương trên YouTube -> suy ra theo BOOKMARK_RATIO.

Video nào không có số like thật (6 video) thì bốc log-uniform trong FALLBACK_LIKE_RANGE:
log-uniform chứ không uniform, vì phân bố lượt xem thực tế là đuôi dài - uniform sẽ cho ra
một đống video cùng cỡ vài trăm nghìn like, nhìn là biết giả.

LƯU Ý QUAN TRỌNG:
    Đây là giá trị NỀN, không phải số đếm từ reactions thật. Trigger ở SPEC 4.4 sẽ cộng/trừ
    từ nền này khi user react, nên hành vi vẫn đúng. Nhưng query rebuild counter ở cuối
    SPEC 4.4 sẽ ĐẶT LẠI các số này về đúng số reaction đang active (tức gần 0) - chỉ chạy
    query đó khi thật sự muốn audit, hoặc chạy lại script này ngay sau đó.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import sys

import asyncpg

from .config import BASE_DIR, DATABASE_DSN

FEED_V2 = BASE_DIR / "feed_items_v2.json"
VIDEO_ID_FROM_S3 = re.compile(r"/hls/([^/]+)/master\.m3u8")

# Tỉ lệ so với số like. Khoảng lấy theo cảm quan số liệu công khai của video ngắn.
DISLIKE_RATIO = (0.004, 0.05)
BOOKMARK_RATIO = (0.02, 0.15)

# Dùng cho video không có số like thật.
FALLBACK_LIKE_RANGE = (1_000, 500_000)


def real_like_counts() -> dict[str, int]:
    """video_id -> số like thật từ YouTube (0 nếu không có)."""
    if not FEED_V2.exists():
        return {}
    out: dict[str, int] = {}
    for entry in json.loads(FEED_V2.read_text(encoding="utf-8")):
        v = entry["video"]
        match = VIDEO_ID_FROM_S3.search(v["playbackAsset"]["url"])
        if match:
            out[match.group(1)] = int((v.get("engagement") or {}).get("likeCount") or 0)
    return out


def log_uniform(lo: int, hi: int) -> int:
    """Bốc số theo log-uniform để giữ dạng phân bố đuôi dài."""
    return int(math.exp(random.uniform(math.log(lo), math.log(hi))))


def counts_for(likes: int) -> tuple[int, int, int]:
    if likes <= 0:
        likes = log_uniform(*FALLBACK_LIKE_RANGE)
    dislikes = int(likes * random.uniform(*DISLIKE_RATIO))
    bookmarks = int(likes * random.uniform(*BOOKMARK_RATIO))
    # Video ít like vẫn nên có vài tương tác lẻ thay vì 0 tuyệt đối.
    return likes, max(dislikes, random.randint(0, 3)), max(bookmarks, random.randint(0, 5))


async def main() -> None:
    apply = "--apply" in sys.argv
    if "--seed" in sys.argv:
        random.seed(int(sys.argv[sys.argv.index("--seed") + 1]))

    likes_by_id = real_like_counts()
    conn = await asyncpg.connect(DATABASE_DSN)
    try:
        rows = await conn.fetch("select id from videos order by id")
        updates = []
        for r in rows:
            like, dislike, bookmark = counts_for(likes_by_id.get(r["id"], 0))
            updates.append((r["id"], like, dislike, bookmark))

        real = sum(1 for r in rows if likes_by_id.get(r["id"], 0) > 0)
        likes_only = sorted(u[1] for u in updates)
        print(f"video                 : {len(updates)}")
        print(f"  dùng like thật      : {real}")
        print(f"  bốc ngẫu nhiên      : {len(updates) - real}")
        print(f"  like min/median/max : {likes_only[0]:,} / {likes_only[len(likes_only)//2]:,} / {likes_only[-1]:,}")
        print(f"  video >= 1 triệu    : {sum(1 for x in likes_only if x >= 1_000_000)}")
        print("\n  ví dụ:")
        for vid, li, di, bo in updates[:5]:
            print(f"    {vid:<16} like={li:>10,}  dislike={di:>8,}  bookmark={bo:>8,}")

        if not apply:
            print("\n(chưa ghi gì - thêm --apply để thực hiện)")
            return

        await conn.executemany(
            """
            update videos
               set like_count = $2, dislike_count = $3, bookmark_count = $4
             where id = $1
            """,
            updates,
        )
        total = await conn.fetchrow(
            """
            select sum(like_count) likes, sum(dislike_count) dislikes,
                   sum(bookmark_count) bookmarks, count(*) filter (where like_count = 0) zero
              from videos
            """
        )
        print(f"\nXong. Tổng like={total['likes']:,}  dislike={total['dislikes']:,}  "
              f"bookmark={total['bookmarks']:,}  (video còn 0 like: {total['zero']})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
