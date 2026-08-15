"""
POST /api/reactions và GET /api/reactions (SPEC mục 3.5, 3.6, 4.1-4.3).

Đây là phần dễ sai nhất của cả API. Ba bất biến phải giữ:
    - bất biến 3: bỏ reaction là set active=false, KHÔNG DELETE row (cần timestamp cho LWW)
    - bất biến 5: xung đột giải bằng last-write-wins theo clientUpdatedAt (thời điểm user bấm)
    - bất biến 7: luôn trả 200, lỗi báo theo từng item; một item hỏng không được làm hỏng batch
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from .. import db
from ..config import MAX_MUTATIONS_PER_BATCH
from ..errors import ApiError
from ..models import ReactionsRequest, parse_client_timestamp
from ..security import Principal, current_principal
from ..serializers import iso, reaction_item, video_counters
from ..urls import request_base_url

router = APIRouter(prefix="/api", tags=["reactions"])

VALID_TYPES = {"LIKE", "DISLIKE", "BOOKMARK"}

# SPEC 4.2: lệch quá 1 năm so với now() -> INVALID_TIMESTAMP.
MAX_CLOCK_SKEW = timedelta(days=365)

# SPEC 4.1: nơi DUY NHẤT quyết định LWW. Không nhân bản logic này ra chỗ khác.
#   - trả 1 row  -> APPLIED
#   - trả 0 row  -> đã có row với timestamp mới hơn hoặc bằng -> STALE
# Idempotency có sẵn: gửi lại đúng mutation cũ thì excluded.client_updated_at bằng giá trị
# đang lưu, mệnh đề where không thoả, không gì thay đổi -> counter không bị đếm đôi.
_UPSERT_SQL = """
insert into reactions (user_id, video_id, type, active, client_updated_at, client_mutation_id)
values ($1, $2, $3::reaction_type, $4,
        -- Không tin đồng hồ client chạy nhanh: kẹp về mốc server + 2 phút.
        least($5::timestamptz, now() + interval '2 minutes'), $6)
on conflict (user_id, video_id, type) do update
   set active             = excluded.active,
       client_updated_at  = excluded.client_updated_at,
       client_mutation_id = excluded.client_mutation_id,
       server_updated_at  = now()
 where excluded.client_updated_at > reactions.client_updated_at
returning active, client_updated_at
"""

_CURRENT_SQL = """
select active, client_updated_at
  from reactions
 where user_id = $1 and video_id = $2 and type = $3::reaction_type
"""

# SPEC 4.3: set LIKE thì tắt DISLIKE và ngược lại, cùng transaction, cùng client_updated_at.
# Client hiện tại cũng gửi mutation tắt row kia một cách tường minh; áp lần hai với cùng
# timestamp thì where không thoả nên thành no-op - hai bên không đánh nhau.
_OPPOSITE_SQL = """
insert into reactions (user_id, video_id, type, active, client_updated_at, client_mutation_id)
values ($1, $2, $3::reaction_type, false,
        least($4::timestamptz, now() + interval '2 minutes'), $5)
on conflict (user_id, video_id, type) do update
   set active            = false,
       client_updated_at = excluded.client_updated_at,
       server_updated_at = now()
 where excluded.client_updated_at > reactions.client_updated_at
"""

_OPPOSITE = {"LIKE": "DISLIKE", "DISLIKE": "LIKE"}


@router.post("/reactions")
async def post_reactions(
    body: ReactionsRequest,
    principal: Principal = Depends(current_principal),
):
    if len(body.mutations) > MAX_MUTATIONS_PER_BATCH:
        raise ApiError(400, "BATCH_TOO_LARGE", f"Tối đa {MAX_MUTATIONS_PER_BATCH} mutation mỗi request")

    results: list[dict] = []
    touched: list[str] = []
    now = datetime.now(timezone.utc)

    async with db.pool().acquire() as conn:
        # Áp TẤT CẢ mutation trong MỘT transaction, theo đúng thứ tự trong array (SPEC 3.5).
        async with conn.transaction():
            for m in body.mutations:
                # ---- 4.2 Validate theo thứ tự, item lỗi không rollback item khác ----
                video = await conn.fetchrow(
                    "select id, status from videos where id = $1", m.videoId
                )
                if video is None or video["status"] == "DELETED":
                    results.append({"mutationId": m.mutationId, "status": "REJECTED",
                                    "reason": "VIDEO_NOT_FOUND"})
                    continue

                if m.type not in VALID_TYPES:
                    results.append({"mutationId": m.mutationId, "status": "REJECTED",
                                    "reason": "INVALID_TYPE"})
                    continue

                client_ts = parse_client_timestamp(m.clientUpdatedAt)
                if client_ts is None or abs(client_ts - now) > MAX_CLOCK_SKEW:
                    results.append({"mutationId": m.mutationId, "status": "REJECTED",
                                    "reason": "INVALID_TIMESTAMP"})
                    continue

                # client_mutation_id là uuid trong DB nhưng chỉ dùng để trace. Nếu client gửi
                # chuỗi không phải UUID thì thay bằng uuid sinh tại chỗ, KHÔNG reject: SPEC 4.2
                # chỉ có 3 lý do reject, và để lỗi này bay vào DB sẽ abort cả transaction
                # (asyncpg huỷ transaction ngay khi có lỗi) -> hỏng luôn các item sau trong batch.
                try:
                    trace_id = str(uuid.UUID(m.mutationId))
                except (ValueError, AttributeError, TypeError):
                    trace_id = str(uuid.uuid4())

                row = await conn.fetchrow(
                    _UPSERT_SQL, principal.user_id, m.videoId, m.type,
                    m.active, client_ts, trace_id,
                )

                touched.append(m.videoId)

                if row is None:
                    current = await conn.fetchrow(
                        _CURRENT_SQL, principal.user_id, m.videoId, m.type
                    )
                    results.append({
                        "mutationId": m.mutationId,
                        "status": "STALE",
                        "current": {
                            "active": current["active"],
                            "clientUpdatedAt": iso(current["client_updated_at"]),
                        },
                    })
                    continue

                # ---- 4.3 Loại trừ LIKE/DISLIKE ----
                if m.active and m.type in _OPPOSITE:
                    await conn.execute(
                        _OPPOSITE_SQL, principal.user_id, m.videoId,
                        _OPPOSITE[m.type], client_ts, trace_id,
                    )

                results.append({"mutationId": m.mutationId, "status": "APPLIED"})

    # `videos` phải là counter SAU KHI COMMIT của mọi video bị chạm tới (SPEC 3.5) - client
    # dùng nó thay số optimistic đang hiển thị. Query ngoài transaction cho đúng nghĩa "sau commit".
    videos: list[dict] = []
    if touched:
        rows = await db.pool().fetch(
            """
            select id, like_count, dislike_count, bookmark_count
              from videos where id = any($1::text[])
            """,
            list(dict.fromkeys(touched)),
        )
        videos = [video_counters(r) for r in rows]

    return {"results": results, "videos": videos}


# Bất biến 4: chỉ trả row active=true, tombstone là chuyện nội bộ của server.
# Video đã xoá (status='DELETED') bị loại hẳn khỏi response (SPEC 3.6).
_LIST_SQL = """
select r.video_id, r.type::text as type, r.client_updated_at,
       v.title, v.thumbnail_url, v.duration_ms,
       v.like_count, v.dislike_count, v.bookmark_count,
       c.name as category_name,
       u.id as creator_id, u.display_name, u.username, u.avatar_url
  from reactions r
  join videos v on v.id = r.video_id
  join users  u on u.id = v.creator_id
  left join categories c on c.id = v.category_id
 where r.user_id = $1 and r.active and v.status <> 'DELETED'
 order by r.client_updated_at desc
"""


@router.get("/reactions")
async def get_reactions(request: Request, principal: Principal = Depends(current_principal)):
    rows = await db.pool().fetch(_LIST_SQL, principal.user_id)
    base = request_base_url(request)
    return {"items": [reaction_item(r, base) for r in rows]}
