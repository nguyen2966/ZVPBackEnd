"""GET /api/config (SPEC mục 3.7)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response

from .. import db
from ..config import CONFIG_TTL_SECONDS
from ..security import Principal, current_principal

router = APIRouter(prefix="/api", tags=["config"])

# SPEC mục 5. Server coi payload là opaque, nhưng bundle default phải đúng shape này vì
# client đọc theo đúng các key ở đây (thiếu key nào thì client rơi về default compile-in).
DEFAULT_PAYLOAD = {
    "feed": {"fallbackTimeoutMs": 1200, "pageSize": 10, "maxWindow": 100},
    "ranking": {
        "positiveCompletionRate": 0.6,
        "minPlaybackMsForSession": 0,
        "enabled": ["likedChannel", "dislikedChannel", "mostWatchedChannel",
                    "likedCategory", "dislikedCategory", "mostWatchedCategory"],
        "weights": {"likedChannel": 1.0, "dislikedChannel": -1.5, "mostWatchedChannel": 0.8,
                    "likedCategory": 0.6, "dislikedCategory": -0.8, "mostWatchedCategory": 0.5},
    },
    "sync": {"batchSize": 50, "debounceMs": 400, "maxAttempts": 8},
    "cache": {"videoTtlHours": 72, "maxCachedVideos": 200,
              "sessionTtlDays": 90, "maxSessions": 5000},
}


@router.get("/config")
async def get_config(
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
):
    row = await db.pool().fetchrow(
        "select version, payload from app_config where enabled limit 1"
    )

    # Không bao giờ trả 404 (SPEC 3.7): client cold start có timeout ngắn cho endpoint này,
    # 404 khiến nó phải chờ hết timeout một cách vô nghĩa. Chưa có bundle -> default, version 0.
    if row is None:
        version, payload = 0, DEFAULT_PAYLOAD
    else:
        version = int(row["version"])
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]

    etag = f'W/"config-v{version}"'   # ETag derive từ version theo SPEC
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    return {"version": version, "ttlSeconds": CONFIG_TTL_SECONDS, "payload": payload}
