"""메시지 전송 파이프라인 테스트. DB 와 에이전트는 가짜로 바꾼다."""

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.chat import repository, service
from app.threads import service as threads_service


class FakeAgent:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, payload: dict, config: dict) -> dict:
        self.calls.append((payload, config))
        return self.result


@pytest.fixture
def saved(monkeypatch) -> list[dict]:
    rows: list[dict] = []

    async def require_owned(pool, thread_id, user_id):
        return {"id": thread_id}

    async def save_success(pool, **fields):
        rows.append(fields)
        return len(rows)

    monkeypatch.setattr(threads_service, "require_owned", require_owned)
    monkeypatch.setattr(repository, "save_success", save_success)
    return rows


@pytest.mark.asyncio
async def test_send_message_returns_answer_and_route(saved) -> None:
    thread_id = uuid4()
    agent = FakeAgent(
        {"messages": [HumanMessage("오늘 날씨"), AIMessage("맑아요.")], "route": "search"}
    )

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=thread_id, user_id=uuid4(), question="오늘 날씨"
    )

    assert reply == service.ChatReply(answer="맑아요.", route="search")
    # 현재 질문만 넘기고, 과거 대화는 thread_id 로 체크포인터가 복구한다.
    payload, config = agent.calls[0]
    assert payload == {"messages": [{"role": "user", "content": "오늘 날씨"}]}
    assert config == {"configurable": {"thread_id": str(thread_id)}}
    assert saved[0]["answer"] == "맑아요."


@pytest.mark.asyncio
async def test_send_message_defaults_route_to_general(saved) -> None:
    agent = FakeAgent({"messages": [AIMessage("안녕하세요")]})

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), question="안녕"
    )

    assert reply.route == "general"
