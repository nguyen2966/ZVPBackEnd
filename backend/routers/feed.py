"""
GET /api/feed (SPEC mục 3.4) và GET /api/users/{userId}/bookmarks.

Hai endpoint trả về CÙNG một shape `{"items": [{"position", "video"}]}` để client dùng
chung một parser và chung một màn hình player.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from .. import db
from ..config import FEED_SIZE
from ..errors import ApiError
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


# Bookmark của một user, trả về ĐÚNG shape của /api/feed.
# Sắp xếp theo client_updated_at giảm dần: bookmark mới bấm nằm trên cùng - khác /api/feed
# (random) vì đây là danh sách người dùng tự tạo, thứ tự ngẫu nhiên sẽ rất khó dùng.
# Lọc status <> 'DELETED' (không phải = 'READY') cho khớp GET /api/reactions: video bị xoá
# phải biến mất khỏi bookmark, còn video đang PROCESSING thì vẫn nên hiện trong danh sách
# đã lưu của user thay vì im lặng mất tích.
_BOOKMARKS_SQL = """
select v.id, v.title, v.caption, v.duration_ms, v.playback_url, v.thumbnail_url,
       v.like_count, v.dislike_count, v.bookmark_count,
       c.name as category_name,
       u.id as creator_id, u.display_name, u.username, u.avatar_url
  from reactions r
  join videos v on v.id = r.video_id
  join users  u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where r.user_id = $1 and r.type = 'BOOKMARK' and r.active
   and v.status <> 'DELETED'
 order by r.client_updated_at desc
"""


async def _viewer_state(user_id: uuid.UUID, video_ids: list[str]) -> dict[str, dict]:
    """Trạng thái reaction của chính người gọi cho một danh sách video (bất biến 4: chỉ active)."""
    viewer: dict[str, dict] = {vid: {} for vid in video_ids}
    if not video_ids:
        return viewer
    for r in await db.pool().fetch(_VIEWER_SQL, user_id, video_ids):
        state = viewer[r["video_id"]]
        if r["type"] == "BOOKMARK":
            state["isBookmarked"] = True
        else:
            # LIKE và DISLIKE loại trừ nhau (bất biến 6) nên tối đa một cái active.
            state["reaction"] = r["type"]
    return viewer


def _as_items(rows, viewer: dict[str, dict], base: str) -> dict:
    return {
        "items": [
            # position là chỉ số 0-based TRONG array trả về, không phải thứ hạng toàn cục.
            {"position": i, "video": feed_video(row, viewer[row["id"]], base)}
            for i, row in enumerate(rows)
        ]
    }


@router.get("/feed")
async def get_feed(request: Request, principal: Principal = Depends(current_principal)):
    rows = await db.pool().fetch(_FEED_SQL, FEED_SIZE)
    viewer = await _viewer_state(principal.user_id, [r["id"] for r in rows])
    return _as_items(rows, viewer, request_base_url(request))


@router.get("/users/{user_id}/bookmarks")
async def get_user_bookmarks(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """
    Toàn bộ video user đó đã bookmark, cùng shape với /api/feed. Không phân trang (tập
    bookmark chỉ cỡ vài trăm row, giống lý do ở GET /api/reactions).

    Chỉ cho phép xem bookmark của CHÍNH MÌNH: bookmark là dữ liệu riêng tư, nhận userId
    tuỳ ý từ client mà không kiểm tra là lỗ IDOR - ai cũng đọc được danh sách đã lưu của
    người khác. Muốn mở cho hồ sơ công khai thì bỏ đoạn kiểm tra này một cách có chủ đích.
    """
    if user_id != principal.user_id:
        raise ApiError(403, "FORBIDDEN", "Chỉ xem được bookmark của chính mình")

    rows = await db.pool().fetch(_BOOKMARKS_SQL, user_id)
    viewer = await _viewer_state(user_id, [r["id"] for r in rows])
    return _as_items(rows, viewer, request_base_url(request))
