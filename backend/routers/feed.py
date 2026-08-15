"""GET /api/feed (SPEC mục 3.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import db
from ..config import FEED_SIZE
from ..security import Principal, current_principal
from ..serializers import feed_video
from ..urls import request_base_url

router = APIRouter(prefix="/api", tags=["feed"])

# `order by random()` sort toàn bộ tập status='READY'. Ở pool ~200 row thì miễn phí và không
# index nào giúp được (SPEC 4.5). Nếu pool lên vài nghìn thì đổi sang `tablesample bernoulli`
# hoặc random-offset - đây là chỗ cần sửa, không phải chỗ khác.
_FEED_SQL = """
select v.id, v.title, v.caption, v.duration_ms, v.playback_url, v.thumbnail_url,
       v.like_count, v.dislike_count, v.bookmark_count,
       c.name as category_name,
       u.id as creator_id, u.display_name, u.username, u.avatar_url
  from videos v
  join users u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where v.status = 'READY'
 order by random()
 limit $1
"""

# Viewer state của chính người gọi cho đúng 10 video vừa bốc (bất biến 4: chỉ row active).
_VIEWER_SQL = """
select video_id, type
  from reactions
 where user_id = $1 and video_id = any($2::text[]) and active
"""


@router.get("/feed")
async def get_feed(request: Request, principal: Principal = Depends(current_principal)):
    pool = db.pool()
    rows = await pool.fetch(_FEED_SQL, FEED_SIZE)

    video_ids = [r["id"] for r in rows]
    viewer: dict[str, dict] = {vid: {} for vid in video_ids}
    if video_ids:
        for r in await pool.fetch(_VIEWER_SQL, principal.user_id, video_ids):
            state = viewer[r["video_id"]]
            if r["type"] == "BOOKMARK":
                state["isBookmarked"] = True
            else:
                # LIKE và DISLIKE loại trừ nhau (bất biến 6) nên tối đa một cái active.
                state["reaction"] = r["type"]

    base = request_base_url(request)
    return {
        "items": [
            # position là chỉ số 0-based TRONG array trả về, không phải thứ hạng toàn cục.
            {"position": i, "video": feed_video(row, viewer[row["id"]], base)}
            for i, row in enumerate(rows)
        ]
    }
