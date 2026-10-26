"""공통 예외와 응답 형식.

사용자에게는 안내 메시지만 보여 주고, 원인은 서버 로그와 chat_logs 에만 남긴다.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import log_event, request_id_ctx


class AppError(Exception):
    """서비스가 의도적으로 발생시키는 오류."""

    status_code = 500
    error_code = "INTERNAL_ERROR"
    message = "요청을 처리하지 못했습니다."

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        # detail 은 로그용 원문이다. 응답에는 절대 포함하지 않는다.
        self.detail = detail
        if message:
            self.message = message
        super().__init__(self.message)


class Unauthorized(AppError):
    status_code = 401
    error_code = "UNAUTHORIZED"
    message = "로그인이 필요합니다."


class Forbidden(AppError):
    status_code = 403
    error_code = "FORBIDDEN"
    message = "권한이 없습니다."


class NotFound(AppError):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "대상을 찾을 수 없습니다."


class InvalidInput(AppError):
    status_code = 422
    error_code = "INVALID_INPUT"
    message = "입력값을 확인해 주세요."


class Conflict(AppError):
    status_code = 409
    error_code = "CONFLICT"
    message = "이미 존재합니다."


class TooManyRequests(AppError):
    status_code = 429
    error_code = "TOO_MANY_REQUESTS"
    message = "요청이 많습니다. 잠시 후 다시 시도해 주세요."


class AITimeout(AppError):
    status_code = 504
    error_code = "AI_TIMEOUT"
    message = "현재 응답이 지연되고 있어요. 잠시 후 다시 시도해 주세요."


class AIUpstreamError(AppError):
    status_code = 502
    error_code = "AI_UPSTREAM_ERROR"
    message = "AI 응답을 받지 못했어요. 잠시 후 다시 시도해 주세요."


# 검증 오류 메시지에 쓸 필드 이름.
_FIELD_LABELS = {
    "email": "이메일",
    "password": "비밀번호",
    "message": "메시지",
    "title": "제목",
}

_VALUE_ERROR_PREFIX = "Value error, "


def _humanize(error: dict) -> str:
    """Pydantic 검증 오류를 사용자에게 보여 줄 한글 문장으로 바꾼다."""
    location = [part for part in error.get("loc", ()) if part != "body"]
    field = str(location[-1]) if location else ""
    label = _FIELD_LABELS.get(field, field)
    error_type = error.get("type", "")
    raw = str(error.get("msg", ""))
    ctx = error.get("ctx") or {}

    # 스키마의 커스텀 검증기가 만든 한글 문장은 그대로 쓴다.
    if raw.startswith(_VALUE_ERROR_PREFIX):
        return raw[len(_VALUE_ERROR_PREFIX) :]

    if error_type == "missing":
        return f"{label}을(를) 입력해 주세요."
    if error_type in ("string_too_short", "too_short"):
        limit = ctx.get("min_length")
        return f"{label} 길이는 {limit}자 이상이어야 합니다."
    if error_type in ("string_too_long", "too_long"):
        limit = ctx.get("max_length")
        return f"{label} 길이는 {limit}자를 넘을 수 없습니다."
    if field == "email":
        return "올바른 이메일 형식이 아닙니다."

    return f"{label} 값을 확인해 주세요." if label else InvalidInput.message


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        log_event(
            "request_failed",
            level=logging.WARNING,
            error_code=exc.error_code,
            detail=exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error_code": exc.error_code,
                "message": exc.message,
                "request_id": request_id_ctx.get(),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        """Pydantic 검증 실패도 다른 오류와 같은 형식으로 돌려준다.

        FastAPI 기본 형식은 {"detail": [...]} 라 프론트가 따로 처리해야 하고,
        스키마에 적어 둔 안내 문구도 묻힌다.
        """
        errors = exc.errors()
        message = _humanize(errors[0]) if errors else InvalidInput.message

        log_event(
            "request_failed",
            level=logging.WARNING,
            error_code=InvalidInput.error_code,
            detail=errors[0].get("loc") if errors else None,
        )
        return JSONResponse(
            status_code=InvalidInput.status_code,
            content={
                "error_code": InvalidInput.error_code,
                "message": message,
                "request_id": request_id_ctx.get(),
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        # 예상하지 못한 오류로 서버가 죽지 않게 하고, 원인은 스택 트레이스로 남긴다.
        log_event("unhandled_error", level=logging.ERROR, error=type(exc).__name__)
        logging.getLogger("chatbot").exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "INTERNAL_ERROR",
                "message": "요청을 처리하지 못했습니다.",
                "request_id": request_id_ctx.get(),
            },
        )
