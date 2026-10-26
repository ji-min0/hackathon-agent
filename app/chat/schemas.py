"""메시지 요청/응답 스키마."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from app.core.config import settings


def clean_message(value: str) -> str:
    # 공백만 입력하는 경우를 막는다. 과제 요구사항의 입력 검증에 해당한다.
    stripped = value.strip()
    if not stripped:
        raise ValueError("메시지를 입력해 주세요.")
    if len(stripped) > settings.max_message_length:
        raise ValueError(f"메시지는 {settings.max_message_length}자를 넘을 수 없습니다.")
    return stripped


class MessageRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        return clean_message(value)


class ResumeRequest(BaseModel):
    """휴먼인더루프: 멈춘 툴 실행을 승인하거나 거절한다."""

    decision: Literal["approve", "reject"]


class ChatRequest(BaseModel):
    """POST /chat 단일 진입점. message(새 질문)와 resume(승인·거절) 중 하나만 보낸다.

    thread_id 를 비우고 message 를 보내면 새 대화방을 만든다.
    """

    thread_id: UUID | None = None
    message: str | None = None
    resume: Literal["approve", "reject"] | None = None

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str | None) -> str | None:
        return None if value is None else clean_message(value)

    @model_validator(mode="after")
    def one_of_message_or_resume(self) -> "ChatRequest":
        if (self.message is None) == (self.resume is None):
            raise ValueError("message 와 resume 중 하나만 보내 주세요.")
        if self.resume is not None and self.thread_id is None:
            raise ValueError("resume 에는 thread_id 가 필요합니다.")
        return self


class MessageResponse(BaseModel):
    thread_id: UUID
    answer: str
    # 답한 경로(general | search | rag, 여러 담당을 쓰면 "rag+search"처럼 합친다).
    # 디버깅·대시보드용이며 웹 화면은 쓰지 않는다.
    route: str
    # done: 답변 완료 / interrupted: 툴 실행 승인을 기다리는 중
    status: Literal["done", "interrupted"] = "done"
    # 승인을 기다리는 툴 호출 목록({"name", "args"}). status=interrupted 일 때만 채운다.
    pending: list[dict] = []


class ChatLogResponse(BaseModel):
    id: int
    question: str
    answer: str | None
    status: str
    created_at: datetime
