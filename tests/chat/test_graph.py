"""멀티 에이전트 그래프 테스트. 실제 LLM 대신 정해진 답을 내는 가짜 모델을 쓴다."""

from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
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
        content="", tool_calls=[{"name": "tavily_search", "args": {"query": query}, "id": call_id}]
    )


def fake_search_tool(calls: list[str]):
    @tool
    def tavily_search(query: str) -> str:
        """Search the web."""
        calls.append(query)
        return f"{query} 결과: 맑음 18도"

    return tavily_search


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
        "agent_pattern": "router",
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


def test_search_tool_exposes_only_query() -> None:
    # 선택 인자(time_range 등)를 모델이 임의로 채우면 결과가 0건이 되곤 했다.
    schema = convert_to_openai_tool(agent.build_search_tool())["function"]["parameters"]

    assert list(schema["properties"]) == ["query"]


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
        "agent_pattern": "router",
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


def tool_result(name: str, call_id: str) -> ToolMessage:
    return ToolMessage(content="결과", name=name, tool_call_id=call_id)


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([HumanMessage("안녕"), AIMessage("안녕하세요")], "general"),
        ([HumanMessage("날씨"), tool_result("tavily_search", "c1"), AIMessage("맑음")], "search"),
        # 이전 턴의 검색은 세지 않는다.
        (
            [HumanMessage("날씨"), tool_result("tavily_search", "c1"), AIMessage("맑음")]
            + [HumanMessage("고마워"), AIMessage("천만에요")],
            "general",
        ),
        (
            [HumanMessage("둘 다"), tool_result("search_agent", "c1")]
            + [tool_result("rag_agent", "c2"), AIMessage("종합")],
            "search",
        ),
    ],
)
def test_route_from_tools_counts_only_current_turn(messages, expected) -> None:
    assert agent.route_from_tools(messages) == expected


def test_route_from_tools_joins_multiple_routes(monkeypatch) -> None:
    monkeypatch.setitem(agent.TOOL_ROUTES, "rag_agent", "rag")
    messages = [
        HumanMessage("문서 마감일이랑 오늘 날씨"),
        tool_result("search_agent", "c1"),
        tool_result("rag_agent", "c2"),
        AIMessage("종합"),
    ]

    assert agent.route_from_tools(messages) == "rag+search"


@pytest.mark.asyncio
async def test_single_pattern_uses_tools_directly_and_records_route(monkeypatch) -> None:
    calls: list[str] = []
    model = FakeToolModel(messages=iter([tool_call("서울 날씨", "c1"), AIMessage("맑음이에요.")]))
    monkeypatch.setattr(agent, "build_search_tool", lambda: fake_search_tool(calls))
    graph = build_test_graph(
        monkeypatch, model, fake_model(), agent_pattern="single", tavily_api_key="k"
    )

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "오늘 서울 날씨"}]}, thread_config("p1")
    )

    assert calls == ["서울 날씨"]
    assert result["route"] == "search"
    assert result["messages"][-1].content == "맑음이에요."


@pytest.mark.asyncio
async def test_supervisor_delegates_self_contained_task_to_search_agent(monkeypatch) -> None:
    calls: list[str] = []
    # main 은 supervisor, search 모델은 검색 담당이 쓴다. 공급자를 나눠 각자 답을 정한다.
    supervisor_model = FakeToolModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_agent", "args": {"task": "오늘 부산 날씨"}, "id": "s1"}
                    ],
                ),
                AIMessage("부산은 맑아요. (출처 포함)"),
            ]
        )
    )
    search_model = FakeToolModel(
        messages=iter([tool_call("부산 날씨", "t1"), AIMessage("부산 맑음, 출처: example.com")])
    )
    models = {"main": supervisor_model, "summary": fake_model(), "search": search_model}
    values = {
        "agent_pattern": "supervisor",
        "main_model_provider": "main",
        "summary_model_provider": "summary",
        "router_model_provider": None,
        "search_model_provider": "search",
        "summary_trigger_tokens": 8000,
        "summary_keep_tokens": 4000,
        "tavily_api_key": "k",
    }
    monkeypatch.setattr(agent, "settings", SimpleNamespace(**values))
    monkeypatch.setattr(agent, "build_model", lambda provider: models[provider])
    monkeypatch.setattr(agent, "build_search_tool", lambda: fake_search_tool(calls))
    graph = agent.build_agent(InMemorySaver())

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "오늘 날씨 어때?"}]}, thread_config("p2")
    )

    assert calls == ["부산 날씨"]
    assert result["route"] == "search"
    # 담당의 답이 supervisor 에게 툴 결과로 돌아간다.
    delegated = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert delegated[0].name == "search_agent"
    assert "부산 맑음" in delegated[0].content
    assert result["messages"][-1].content == "부산은 맑아요. (출처 포함)"
