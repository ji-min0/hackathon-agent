from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.chat.schemas import ChatRequest, MessageRequest


def test_message_request_strips_surrounding_whitespace() -> None:
    request = MessageRequest(message="  이전 대화를 기억해?  ")

    assert request.message == "이전 대화를 기억해?"


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_message_request_rejects_blank_input(message: str) -> None:
    with pytest.raises(ValidationError):
        MessageRequest(message=message)


def test_chat_request_needs_exactly_one_of_message_or_resume() -> None:
    assert ChatRequest(message=" 안녕 ").message == "안녕"
    assert ChatRequest(thread_id=uuid4(), resume="approve").resume == "approve"
    for body in [{}, {"message": "안녕", "resume": "approve"}, {"resume": "reject"}]:
        with pytest.raises(ValidationError):
            ChatRequest(**body)
