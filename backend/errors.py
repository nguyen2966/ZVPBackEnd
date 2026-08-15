"""
Định dạng lỗi chung (SPEC mục 7):

    { "error": { "code": "SESSION_REVOKED", "message": "Signed in on another device" } }

FastAPI mặc định trả {"detail": ...} nên phải đăng ký exception handler để ép về shape trên.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """Lỗi có mã theo SPEC mục 7."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


# Ánh xạ HTTPException (do FastAPI/Starlette tự ném) sang code của SPEC.
_STATUS_TO_CODE = {
    400: "INVALID_REQUEST",
    401: "TOKEN_EXPIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "USERNAME_TAKEN",
    429: "RATE_LIMITED",
    500: "INTERNAL",
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message),
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
        return JSONResponse(
            status_code=400,
            content=error_body("INVALID_REQUEST", str(exc.errors()[:3])),
        )
