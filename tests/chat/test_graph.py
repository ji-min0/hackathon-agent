"""멀티 에이전트 그래프 테스트. 실제 LLM 대신 정해진 답을 내는 가짜 모델을 쓴다."""

from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.chat import agent


def fake_model(*answers: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter(AIMessage(content=a) for a in answers))


class FakeToolModel(GenericFakeChatModel):
    """툴을 묶을 수 있는 가짜 모델. 정해 둔 tool call 이나 답을 순서대로 낸다."""

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(query: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "web_search", "args": {"query": query}, "id": call_id}]
    )


def fake_search_tool(calls: list[str]):
    @tool
    def web_search(query: str) -> str:
        """Search the web."""
        calls.append(query)
        return f"{query} 결과: 맑음 18도"

    return web_search


def fake_router(route: str = "general") -> RunnableLambda:
    return RunnableLambda(lambda _: agent.RouteDecision(route=route, reason="test"))


def failing_router() -> RunnableLambda:
    def fail(_):
        raise RuntimeError("router down")

    return RunnableLambda(fail)


def build_test_graph(monkeypatch, main_model, summary_model, router=None, **overrides):
    values = {
        "main_model_provider": "main",
        "summary_model_provider": "summary",
        "summary_trigger_tokens": 8000,
        "summary_keep_tokens": 4000,
        "router_model_provider": None,
        "search_model_provider": None,
        "tavily_api_key": "",
        "tavily_max_results": 5,
        **overrides,
    }
    monkeypatch.setattr(agent, "settings", SimpleNamespace(**values))
    models = {"main": main_model, "summary": summary_model}
    monkeypatch.setattr(agent, "build_model", lambda provider: models[provider])
    monkeypatch.setattr(agent, "build_router", lambda provider: router or fake_router())
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


@pytest.mark.asyncio
async def test_route_from_classifier_is_recorded(monkeypatch) -> None:
    # search 노드가 생기기 전까지는 general 이 답하지만, 분류 결과는 State 에 남는다.
    graph = build_test_graph(monkeypatch, fake_model("답"), fake_model(), fake_router("search"))

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "오늘 날씨"}]}, thread_config("r")
    )

    assert result["route"] == "search"
    assert result["messages"][-1].content == "답"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_general(monkeypatch) -> None:
    graph = build_test_graph(monkeypatch, fake_model("답"), fake_model(), failing_router())

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "안녕"}]}, thread_config("f")
    )

    assert result["route"] == "general"
    assert result["messages"][-1].content == "답"


def test_router_input_is_text_transcript_without_tool_messages() -> None:
    messages = [
        HumanMessage(content="환율 알려줘"),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
        ToolMessage(content="검색 결과", tool_call_id="c1"),
        AIMessage(content="1달러는 1400원입니다.", name="main_agent"),
        HumanMessage(content="그럼 엔화는?"),
    ]

    system, user = agent.router_input(messages)

    assert "No documents are registered yet." in system.content
    assert "user: 환율 알려줘\nassistant: 1달러는 1400원입니다." in user.content
    assert "<latest_message>\n그럼 엔화는?\n</latest_message>" in user.content
    assert "검색 결과" not in user.content


@pytest.mark.parametrize(
    "text",
    [
        '{"route": "search", "reason": "weather changes daily"}',
        '```json\n{"route": "search", "reason": "weather changes daily"}\n```',
    ],
)
def test_router_parses_json_answer(monkeypatch, text: str) -> None:
    monkeypatch.setattr(agent, "build_model", lambda provider: fake_model(text))

    decision = agent.build_router("openai").invoke("오늘 날씨")

    assert decision == agent.RouteDecision(route="search", reason="weather changes daily")


def test_router_rejects_unknown_route(monkeypatch) -> None:
    text = '{"route": "weather", "reason": "x"}'
    monkeypatch.setattr(agent, "build_model", lambda provider: fake_model(text))

    with pytest.raises(OutputParserException):
        agent.build_router("openai").invoke("오늘 날씨")


def build_search_graph(monkeypatch, main_model, calls: list[str], **overrides):
    monkeypatch.setattr(agent, "build_search_tool", lambda: fake_search_tool(calls))
    return build_test_graph(
        monkeypatch,
        main_model,
        fake_model(),
        fake_router("search"),
        tavily_api_key="test-tavily-key",
        **overrides,
    )


@pytest.mark.asyncio
async def test_search_route_calls_tool_and_keeps_tool_history(monkeypatch) -> None:
    calls: list[str] = []
    model = FakeToolModel(
        messages=iter([tool_call("서울 날씨", "c1"), AIMessage("맑음 18도예요.")])
    )
    graph = build_search_graph(monkeypatch, model, calls)

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "오늘 서울 날씨"}]}, thread_config("w")
    )

    assert result["route"] == "search"
    assert calls == ["서울 날씨"]
    assert [m.type for m in result["messages"]] == ["human", "ai", "tool", "ai"]
    assert result["messages"][-1].content == "맑음 18도예요."


@pytest.mark.asyncio
async def test_search_is_limited_per_answer(monkeypatch) -> None:
    calls: list[str] = []
    answers = [tool_call(f"q{i}", f"c{i}") for i in range(1, 4)] + [AIMessage("찾은 만큼 답해요.")]
    graph = build_search_graph(monkeypatch, FakeToolModel(messages=iter(answers)), calls)

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "검색"}]}, thread_config("l")
    )

    # 세 번째 검색은 실행되지 않고, 모델은 가진 결과로 답한다.
    assert calls == ["q1", "q2"]
    assert result["messages"][-1].content == "찾은 만큼 답해요."


@pytest.mark.asyncio
async def test_search_route_without_tavily_key_goes_to_general(monkeypatch) -> None:
    graph = build_test_graph(
        monkeypatch, fake_model("일반 답"), fake_model(), fake_router("search")
    )

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "오늘 날씨"}]}, thread_config("k")
    )

    assert result["route"] == "search"
    assert result["messages"][-1].content == "일반 답"


def test_search_tool_gets_key_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        agent, "settings", SimpleNamespace(tavily_api_key="tvly-test", tavily_max_results=3)
    )

    search_tool = agent.build_search_tool()

    assert search_tool.max_results == 3
    assert search_tool.api_wrapper.tavily_api_key.get_secret_value() == "tvly-test"


@pytest.mark.parametrize(
    ("overrides", "expected_models", "expected_router"),
    [
        # 비우면 main 공급자를 따른다. build_model 은 main, summary, search 순서로 부른다.
        ({}, ["main", "summary", "main"], ["main"]),
        (
            {"router_model_provider": "fast", "search_model_provider": "fast"},
            ["main", "summary", "fast"],
            ["fast"],
        ),
    ],
)
def test_node_providers_default_to_main(
    monkeypatch, overrides, expected_models, expected_router
) -> None:
    models: list[str] = []
    routers: list[str] = []
    values = {
        "main_model_provider": "main",
        "summary_model_provider": "summary",
        "router_model_provider": None,
        "search_model_provider": None,
        "summary_trigger_tokens": 8000,
        "summary_keep_tokens": 4000,
        "tavily_api_key": "test-tavily-key",
        **overrides,
    }
    monkeypatch.setattr(agent, "settings", SimpleNamespace(**values))
    monkeypatch.setattr(agent, "build_search_tool", lambda: fake_search_tool([]))
    monkeypatch.setattr(
        agent, "build_model", lambda p: models.append(p) or FakeToolModel(messages=iter([]))
    )
    monkeypatch.setattr(agent, "build_router", lambda p: routers.append(p) or fake_router())

    agent.build_agent(InMemorySaver())

    assert models == expected_models
    assert routers == expected_router
