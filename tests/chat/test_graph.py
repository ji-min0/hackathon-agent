"""멀티 에이전트 그래프 테스트. 실제 LLM 대신 정해진 답을 내는 가짜 모델을 쓴다."""

from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.chat import agent


def fake_model(*answers: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter(AIMessage(content=a) for a in answers))


def build_test_graph(monkeypatch, main_model, summary_model, **overrides):
    values = {
        "main_model_provider": "main",
        "summary_model_provider": "summary",
        "summary_trigger_tokens": 8000,
        "summary_keep_tokens": 4000,
        **overrides,
    }
    monkeypatch.setattr(agent, "settings", SimpleNamespace(**values))
    models = {"main": main_model, "summary": summary_model}
    monkeypatch.setattr(agent, "build_model", lambda provider: models[provider])
    return agent.build_agent(InMemorySaver())


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


@pytest.mark.asyncio
async def test_general_route_answers_and_records_route(monkeypatch) -> None:
    graph = build_test_graph(monkeypatch, fake_model("안녕하세요"), fake_model())

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "안녕"}]}, config=thread_config("t1")
    )

    assert result["route"] == "general"
    assert result["messages"][-1].content == "안녕하세요"


@pytest.mark.asyncio
async def test_checkpointer_keeps_history_per_thread(monkeypatch) -> None:
    graph = build_test_graph(monkeypatch, fake_model("답1", "답2", "다른 방 답"), fake_model())

    await graph.ainvoke({"messages": [{"role": "user", "content": "질문1"}]}, thread_config("a"))
    second = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "질문2"}]}, thread_config("a")
    )
    other = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "새 질문"}]}, thread_config("b")
    )

    # 같은 thread 는 이전 대화가 복구되고, 다른 thread 와는 섞이지 않는다.
    assert [m.content for m in second["messages"]] == ["질문1", "답1", "질문2", "답2"]
    assert [m.content for m in other["messages"]] == ["새 질문", "다른 방 답"]


@pytest.mark.asyncio
async def test_summarized_history_is_saved_to_parent_state(monkeypatch) -> None:
    # 토큰 한도를 아주 작게 잡아 두 번째 턴에서 요약이 일어나게 한다.
    graph = build_test_graph(
        monkeypatch,
        fake_model("첫 답변 " * 20, "두 번째 답변"),
        fake_model("요약본"),
        summary_trigger_tokens=30,
        summary_keep_tokens=5,
    )

    await graph.ainvoke({"messages": [{"role": "user", "content": "질문1"}]}, thread_config("s"))
    await graph.ainvoke({"messages": [{"role": "user", "content": "질문2"}]}, thread_config("s"))

    state = await graph.aget_state(thread_config("s"))
    contents = [m.content for m in state.values["messages"]]
    # 요약 전 원본 메시지는 사라지고, 요약 메시지가 남아야 한다.
    assert "질문1" not in contents
    assert any("요약본" in c for c in contents)
    assert contents[-1] == "두 번째 답변"


@pytest.mark.asyncio
async def test_message_names_are_removed_before_model_call(monkeypatch) -> None:
    # 게이트웨이는 메시지 name 필드를 거부한다. 예전 에이전트가 저장한 name 도 지워야 한다.
    graph = build_test_graph(monkeypatch, fake_model("답2"), fake_model())
    config = thread_config("n")
    old_history = [HumanMessage(content="질문1"), AIMessage(content="답1", name="main_agent")]
    await graph.aupdate_state(config, {"messages": old_history})

    result = await graph.ainvoke({"messages": [{"role": "user", "content": "질문2"}]}, config)

    assert [m.name for m in result["messages"]] == [None, None, None, None]
