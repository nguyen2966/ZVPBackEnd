"""
GET /api/config (SPEC mục 3.7).

Payload lưu trong DB dưới dạng từng row key-value (bảng app_config_entries) nhưng response
vẫn là JSON lồng nhau đúng SPEC mục 5 - client không thấy khác biệt. Xem config_payload.py.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response

from .. import db
from ..config import CONFIG_TTL_SECONDS
from ..security import Principal, current_principal

router = APIRouter(prefix="/api", tags=["config"])

# Dữ liệu mẫu để seed môi trường dev. Endpoint không trả trực tiếp object này: payload live
# luôn được lắp từ app_config_entries của app_config đang enabled.
DEFAULT_PAYLOAD = {
    "feed": {"fallbackTimeoutMs": 1200, "pageSize": 10, "maxWindow": 100},
    "ranking": {
        "positiveCompletionRate": 0.6,
        "minPlaybackMsForSession": 0,
        "enabled": ["likedChannel", "dislikedChannel", "positiveChannel",
                    "likedCategory", "dislikedCategory", "positiveCategory"],
        "weights": {"likedChannel": 1.0, "dislikedChannel": -1.5, "positiveChannel": 0.8,
                    "likedCategory": 0.6, "dislikedCategory": -0.8, "positiveCategory": 0.5},
    },
    "sync": {"batchSize": 50, "debounceMs": 400, "maxAttempts": 8},
    "cache": {"videoTtlHours": 72, "maxCachedVideos": 200,
              "sessionTtlDays": 90, "maxSessions": 5000},
}


def _decode_jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def _set_dotted_key(payload: dict, dotted_key: str, value) -> None:
    parts = dotted_key.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError(f"Invalid app config key: {dotted_key!r}")

    target = payload
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = _decode_jsonb(value)


@router.get("/config")
async def get_config(
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
):
    rows = await db.pool().fetch(
        """
        select c.version, e.key, e.value
          from app_config c
          left join app_config_entries e on e.config_id = c.id
         where c.enabled
         order by e.key
        """
    )

    # Không bao giờ trả 404. Chưa có config live -> bundle rỗng, version 0; client sẽ dùng
    # default compile-in. Không lấy DEFAULT_PAYLOAD ở đây vì DB là source of truth duy nhất.
    if not rows:
        version, payload = 0, {}
    else:
        version, payload = int(rows[0]["version"]), {}
        for row in rows:
            if row["key"] is not None:
                _set_dotted_key(payload, row["key"], row["value"])

    etag = f'W/"config-v{version}"'   # ETag derive từ version theo SPEC
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    return {"version": version, "ttlSeconds": CONFIG_TTL_SECONDS, "payload": payload}
