"""
Định dạng lỗi chung (SPEC mục 7):

    { "error": { "code": "SESSION_REVOKED", "message": "Signed in on another device" } }

FastAPI mặc định trả {"detail": ...} nên phải đăng ký exception handler để ép về shape trên.

Các mã lỗi và HTTP status:

  400 INVALID_REQUEST   - lỗi chung không phân loại
  401 TOKEN_EXPIRED    - auth thất bại
  403 FORBIDDEN        - không có quyền
  404 NOT_FOUND        - resource không tồn tại
  409 USERNAME_TAKEN / UPLOAD_* - xung đột
  413 FILE_TOO_LARGE   - file vượt giới hạn kích thước
  422 INVALID_METADATA  - metadata đầu vào không hợp lệ (sai type, rỗng, vượt length…)
  429 RATE_LIMITED     - quá rate limit
  500 INTERNAL         - lỗi server không xác định
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """
    Lỗi có mã theo SPEC mục 7.

    Thuộc tính ``details`` và ``errors`` cho phép đính kèm payload mở rộng:

    - ``details`` — dict tuỳ ý, dùng cho 413 FILE_TOO_LARGE
      (ví dụ: {"max_size_bytes": 524288000, "actual_size_bytes": 600000000})

    - ``errors`` — list of {field, rule, message}, dùng cho 422 INVALID_METADATA
      (ví dụ: [{"field": "title", "rule": "min_length", "message": "title không được rỗng"}])
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
        errors: list[dict[str, str]] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.details = details
        self.errors = errors


def error_body(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Dựng payload lỗi tuân theo spec; bỏ trống trường không dùng."""
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    if errors is not None:
        body["error"]["errors"] = errors
    return body


# Ánh xạ HTTPException (do FastAPI/Starlette tự ném) sang code của SPEC.
_STATUS_TO_CODE = {
    400: "INVALID_REQUEST",
    401: "TOKEN_EXPIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "USERNAME_TAKEN",
    413: "FILE_TOO_LARGE",
    422: "INVALID_METADATA",
    429: "RATE_LIMITED",
    500: "INTERNAL",
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details, exc.errors),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, "INTERNAL")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(p) for p in e.get("loc", ())[:0:-1]) or "unknown",
                "rule": e.get("type", "unknown"),
                "message": e.get("msg", ""),
            }
            for e in exc.errors()[:5]
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "INVALID_METADATA",
                "Metadata validation failed.",
                errors=errors,
            ),
        )
