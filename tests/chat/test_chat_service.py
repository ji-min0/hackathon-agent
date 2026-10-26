"""메시지 전송 파이프라인 테스트. DB 와 에이전트는 가짜로 바꾼다."""

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command, Interrupt

from app.chat import repository, service
from app.core.errors import InvalidInput
from app.threads import service as threads_service


class FakeAgent:
    def __init__(self, result: dict, interrupts: list | None = None) -> None:
        self.result = result
        self.interrupts = interrupts or []
        self.calls: list[tuple[dict, dict]] = []

    async def aget_state(self, config: dict):
        return type("Snapshot", (), {"interrupts": self.interrupts})()

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
    assert saved[0]["route"] == "search"


@pytest.mark.asyncio
async def test_send_message_defaults_route_to_general(saved) -> None:
    agent = FakeAgent({"messages": [AIMessage("안녕하세요")]})

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), question="안녕"
    )

    assert reply.route == "general"


def approval_interrupt(*queries: str) -> Interrupt:
    requests = [{"name": "tavily_search", "args": {"query": q}} for q in queries]
    return Interrupt(value={"action_requests": requests, "review_configs": []}, id="i1")


@pytest.mark.asyncio
async def test_interrupted_result_asks_for_approval(saved) -> None:
    agent = FakeAgent(
        {"messages": [HumanMessage("날씨")], "__interrupt__": [approval_interrupt("서울 날씨")]}
    )

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), question="날씨"
    )

    assert reply.status == "interrupted"
    assert reply.route == "search"
    assert reply.pending == [{"name": "tavily_search", "args": {"query": "서울 날씨"}}]
    assert "웹에서 '서울 날씨'을(를) 검색" in reply.answer
    assert saved[0]["answer"] == reply.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected", "logged"),
    [
        ("approve", {"type": "approve"}, "[승인]"),
        ("reject", {"type": "reject", "message": service.REJECT_MESSAGE}, "[거절]"),
    ],
)
async def test_resume_sends_one_decision_per_pending_call(
    saved, decision, expected, logged
) -> None:
    agent = FakeAgent(
        {"messages": [AIMessage("검색 결과로 답해요")], "route": "search"},
        interrupts=[approval_interrupt("서울 날씨", "부산 날씨")],
    )

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), decision=decision
    )

    command, _ = agent.calls[0]
    assert isinstance(command, Command)
    assert command.resume == {"i1": {"decisions": [expected, expected]}}
    assert reply == service.ChatReply(answer="검색 결과로 답해요", route="search")
    assert saved[0]["question"] == logged


@pytest.mark.asyncio
async def test_resume_without_pending_action_is_rejected(saved) -> None:
    with pytest.raises(InvalidInput):
        await service.send_message(
            pool=None, agent=FakeAgent({}), thread_id=uuid4(), user_id=uuid4(), decision="approve"
        )


def test_approval_text_describes_unknown_tools_without_raw_data() -> None:
    text = service.approval_text([{"name": "send_email", "args": {"to": "a@b.c"}}])

    assert "'네' 또는 '아니요'로 답해 주세요" in text
    assert "- 'send_email' 작업을 실행\n  · to: a@b.c" in text
    assert "{" not in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("네", "approve"),
        ("네 해주세요!", "approve"),
        ("네, 진행해 주세요", "approve"),
        ("응응", "approve"),
        ("ㅇㅇ", "approve"),
        ("좋아요~", "approve"),
        ("OK", "approve"),
        ("아니요", "reject"),
        ("아니 하지 마", "reject"),
        ("취소해줘", "reject"),
        ("ㄴㄴ", "reject"),
        ("아니 그냥 환율 알려줘", None),
        ("오늘 날씨 어때?", None),
        ("", None),
    ],
)
def test_parse_decision(text: str, expected: str | None) -> None:
    assert service.parse_decision(text) == expected


@pytest.mark.asyncio
async def test_short_chat_reply_resumes_pending_action(saved) -> None:
    agent = FakeAgent(
        {"messages": [AIMessage("검색해서 답해요")], "route": "search"},
        interrupts=[approval_interrupt("서울 날씨")],
    )

    reply = await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), question="네"
    )

    command, _ = agent.calls[0]
    assert command.resume == {"i1": {"decisions": [{"type": "approve"}]}}
    assert reply.answer == "검색해서 답해요"
    # 채팅으로 답했으면 사용자가 쓴 말을 그대로 기록한다.
    assert saved[0]["question"] == "네"


@pytest.mark.asyncio
async def test_yes_without_pending_action_is_a_normal_question(saved) -> None:
    agent = FakeAgent({"messages": [AIMessage("무엇을 도와드릴까요?")]})

    await service.send_message(
        pool=None, agent=agent, thread_id=uuid4(), user_id=uuid4(), question="네"
    )

    payload, _ = agent.calls[0]
    assert payload == {"messages": [{"role": "user", "content": "네"}]}
