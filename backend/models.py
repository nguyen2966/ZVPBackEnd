"""
Pydantic model cho request/response.

Response được dựng bằng dict thuần (xem serializers.py) thay vì model, để giữ chính xác
từng tên field trong SPEC mục 3 - sai một tên là client parse ra rỗng mà không báo lỗi.
Model ở đây chủ yếu để validate request đầu vào.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    displayName: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    deviceId: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=1)


class Mutation(BaseModel):
    """
    Một mutation reaction. Cố ý KHÔNG ràng buộc `type` vào Enum và để `clientUpdatedAt`
    là str: SPEC mục 4.2 yêu cầu type sai -> REJECTED/INVALID_TYPE và timestamp sai ->
    REJECTED/INVALID_TIMESTAMP ở mức từng item, chứ không phải 400 cho cả batch (bất biến 7).
    """
    mutationId: str
    videoId: str
    type: str
    active: bool
    clientUpdatedAt: str


class ReactionsRequest(BaseModel):
    mutations: list[Mutation]


def parse_client_timestamp(value: str) -> datetime | None:
    """
    Parse ISO-8601 của client. Trả None nếu không parse được (-> INVALID_TIMESTAMP).
    Chuỗi kết thúc bằng 'Z' không được fromisoformat của Python < 3.11 chấp nhận nên đổi sang +00:00.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Không có offset -> coi như UTC (SPEC yêu cầu timestamp luôn là UTC).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
