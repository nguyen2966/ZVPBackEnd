"""Suy ra base URL tuyệt đối để ghép vào đường dẫn asset."""

from __future__ import annotations

from fastapi import Request

from .config import PUBLIC_BASE_URL


def request_base_url(request: Request) -> str:
    """
    Ưu tiên PUBLIC_BASE_URL (đặt khi chạy sau tunnel/proxy), không có thì lấy theo host mà
    client gọi tới - nhờ vậy đổi giữa localhost / IP LAN / tunnel không phải re-seed database.
    """
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
