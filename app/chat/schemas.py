"""메시지 요청/응답 스키마."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.core.config import settings


class MessageRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        # 공백만 입력하는 경우를 막는다. 과제 요구사항의 입력 검증에 해당한다.
        stripped = value.strip()
        if not stripped:
            raise ValueError("메시지를 입력해 주세요.")
        if len(stripped) > settings.max_message_length:
            raise ValueError(f"메시지는 {settings.max_message_length}자를 넘을 수 없습니다.")
        return stripped


class MessageResponse(BaseModel):
    thread_id: UUID
    answer: str


class ChatLogResponse(BaseModel):
    id: int
    question: str
    answer: str | None
    status: str
    created_at: datetime
