"""
Dựng JSON response đúng từng tên field trong SPEC mục 3.

Tất cả shape trả về client tập trung ở đây để chỉ có MỘT chỗ phải soi khi đối chiếu hợp đồng.
Sai một tên field là client parse ra rỗng mà không báo lỗi - đây là chế độ lỗi tệ nhất.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 UTC kèm mili giây và hậu tố Z: 2026-08-14T09:12:03.412Z (SPEC mục 0)."""
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def absolute_url(path_or_url: str | None, base: str) -> str | None:
    """
    videos.playback_url/thumbnail_url lưu dạng path ('/video/upload/...'), API ghép base URL
    của request để trả absolute URL - xem comment trong schema.sql. Nếu DB đã lưu absolute
    URL sẵn thì giữ nguyên.
    """
    if not path_or_url:
        return None
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return f"{base}{path_or_url}"


def feed_video(row: asyncpg.Record, viewer: dict[str, Any], base: str) -> dict[str, Any]:
    """Một phần tử `video` trong GET /api/feed (SPEC mục 3.4)."""
    return {
        "id": row["id"],
        "user": {
            "id": str(row["creator_id"]),
            "displayName": row["display_name"],
            "username": row["username"],
            "avatarUrl": row["avatar_url"],
        },
        # category.name rỗng -> client tự thay bằng "Uncategorized"
        "category": {"name": row["category_name"]},
        "title": row["title"],
        "caption": row["caption"] or "",
        "durationMs": int(row["duration_ms"]),
        "playbackAsset": {"url": absolute_url(row["playback_url"], base)},
        "thumbnailAsset": {"url": absolute_url(row["thumbnail_url"], base)},
        "engagement": {
            # clamp về 0: SPEC nói số âm bị client clamp, trả sẵn số đúng thì hơn
            "likeCount": max(0, row["like_count"]),
            "dislikeCount": max(0, row["dislike_count"]),
            "bookmarkCount": max(0, row["bookmark_count"]),
        },
        "viewerState": {
            "isBookmarked": viewer.get("isBookmarked", False),
            # "LIKE" | "DISLIKE" | null
            "reaction": viewer.get("reaction"),
        },
    }


def reaction_item(row: asyncpg.Record, base: str) -> dict[str, Any]:
    """
    Một phần tử trong GET /api/reactions (SPEC mục 3.6).

    `video` nhúng kèm là bắt buộc kể cả với LIKE/DISLIKE: client dùng chính response này để
    vẽ màn hình bookmark trên thiết bị vừa login (cache local trống) mà không phải bắn N request.
    """
    return {
        "videoId": row["video_id"],
        "type": row["type"],
        "clientUpdatedAt": iso(row["client_updated_at"]),
        "video": {
            "id": row["video_id"],
            "title": row["title"],
            "thumbnailUrl": absolute_url(row["thumbnail_url"], base),
            "durationMs": int(row["duration_ms"]),
            "category": row["category_name"],
            "creator": {
                "id": str(row["creator_id"]),
                "displayName": row["display_name"],
                "username": row["username"],
                "avatarUrl": row["avatar_url"],
            },
            "engagement": {
                "likeCount": max(0, row["like_count"]),
                "dislikeCount": max(0, row["dislike_count"]),
                "bookmarkCount": max(0, row["bookmark_count"]),
            },
        },
    }


def video_counters(row: asyncpg.Record) -> dict[str, Any]:
    """Phần tử trong mảng `videos` của POST /api/reactions (SPEC mục 3.5)."""
    return {
        "id": row["id"],
        "likeCount": max(0, row["like_count"]),
        "dislikeCount": max(0, row["dislike_count"]),
        "bookmarkCount": max(0, row["bookmark_count"]),
    }
