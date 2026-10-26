"""LangGraph 에이전트 구성.

컨텍스트 유지 전략의 핵심은 두 가지다.

1. FastAPI 는 과거 대화를 다시 조립하지 않는다. 현재 질문만 넘긴다.
2. 같은 thread_id 를 넘기면 체크포인터가 이전 State 를 복구한다.

멀티 에이전트 그래프 흐름

    START → classify(의도 분류기) → general | search | rag → END

자세한 내용은 docs/API_SPEC.md 5장 참고.
"""

import logging
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelRequest,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    dynamic_prompt,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.output_parsers import PydanticOutputParser
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel, Field

from app.chat.prompts import (
    RAG_DOCUMENTS,
    ROUTER_PROMPT,
    SEARCH_PROMPT,
    SUMMARY_PROMPT,
    SYSTEM_PROMPT,
)
from app.core.config import settings
from app.core.logging import log_event


def build_openai_model() -> ChatOpenAI:
    """기본 OpenAI 호환 모델 인스턴스를 만든다.

    base_url 을 바꾸면 Codyssey 게이트웨이, vLLM 등 OpenAI 호환 서버로 전환된다.
    main과 summary는 같은 공급자를 쓰더라도 인스턴스를 공유하지 않는다.
    """
    return ChatOpenAI(
        model=settings.openai_model_name,
        base_url=settings.openai_base_url,
        # GPT-5 계열은 temperature 기본값(1)만 허용해서 지정하지 않는다.
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        # 재시도 대신 명시적인 폴백 체인으로 장애를 처리한다.
        max_retries=0,
    )


def build_anthropic_model() -> ChatAnthropic:
    """Anthropic 호환 모델 인스턴스를 만든다."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY가 설정되어 있지 않습니다.")

    return ChatAnthropic(
        model=settings.anthropic_model_name,
        # Anthropic SDK 가 /v1/messages 를 직접 붙이므로 /v1 없이 넘긴다.
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        # Anthropic 규격은 max_tokens 가 필수다.
        max_tokens=2048,
        timeout=settings.anthropic_timeout_seconds,
        max_retries=0,
    )


def build_gemini_model() -> ChatGoogleGenerativeAI:
    """명시적으로 선택한 경우에만 Gemini 모델 인스턴스를 만든다."""
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY가 설정되어 있지 않습니다.")

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_name,
        temperature=0,
        api_key=settings.gemini_api_key,
        request_timeout=settings.gemini_timeout_seconds,
        retries=0,
    )


def build_openai_fallback_model():
    """OpenAI 우선. 실패하면 Anthropic, 그다음 Gemini 순서로 넘어간다.

    키가 비어 있는 공급자는 폴백 목록에서 뺀다.
    """
    primary_model = build_openai_model()

    fallbacks = []
    if settings.anthropic_api_key:
        fallbacks.append(build_anthropic_model())
    if settings.gemini_api_key:
        fallbacks.append(build_gemini_model())

    if not fallbacks:
        return primary_model
    return primary_model.with_fallbacks(fallbacks)


def build_model(provider: str):
    """설정에서 명시한 공급자의 역할별 모델을 만든다."""
    if provider == "openai":
        return build_openai_fallback_model()
    if provider == "anthropic":
        return build_anthropic_model()
    if provider == "gemini":
        return build_gemini_model()
    raise ValueError(f"지원하지 않는 모델 공급자입니다: {provider}")


Route = Literal["rag", "search", "general"]


class ChatState(MessagesState):
    """그래프 전체가 공유하는 State. messages 는 MessagesState 가 정의한다."""

    # 분류기가 고른 경로. 응답·로그에 함께 남겨 디버깅과 대시보드에 쓴다.
    route: Route


class RouteDecision(BaseModel):
    """분류기 출력 스키마. 모델이 쓴 JSON 을 이 형식으로 검증한다."""

    route: Route = Field(description="The agent that should answer the latest message.")
    reason: str = Field(description="One short sentence explaining the choice.")


# 분류기에 보여 줄 최근 메시지 수. 지시어("그거", "더 자세히")를 풀 정도면 충분하다.
ROUTER_HISTORY_MESSAGES = 6


def build_router(provider: str):
    """RouteDecision 을 돌려주는 분류기를 만든다.

    with_structured_output 은 쓰지 않는다. Codyssey 게이트웨이의 OpenAI 경로가
    response_format 과 tool_choice 강제를 400(unsupported_feature)으로 거부하기 때문이다.
    대신 프롬프트로 JSON 만 쓰게 하고 Pydantic 파서로 검증한다. 공급자와 상관없이
    동작하므로 vLLM 으로 바꿔도 그대로 쓴다.
    """
    return build_model(provider) | PydanticOutputParser(pydantic_object=RouteDecision)


def router_input(messages: list[AnyMessage]) -> list:
    """분류기 입력을 만든다. 최근 대화를 텍스트 대본으로 바꿔 넘긴다.

    메시지 객체를 그대로 넘기면 tool call 과 tool 결과가 잘려 짝이 깨질 수 있고,
    게이트웨이가 거부하는 name 필드도 따라간다. 분류에는 텍스트만 있으면 된다.
    """
    recent = [m for m in messages if isinstance(m, HumanMessage | AIMessage) and m.text]
    *history, latest = recent[-ROUTER_HISTORY_MESSAGES:]
    lines = [f"{'user' if isinstance(m, HumanMessage) else 'assistant'}: {m.text}" for m in history]
    transcript = "\n".join(lines) or "(no earlier turns)"
    return [
        SystemMessage(ROUTER_PROMPT.format(rag_documents=RAG_DOCUMENTS)),
        HumanMessage(
            f"<conversation>\n{transcript}\n</conversation>\n\n"
            f"<latest_message>\n{latest.text}\n</latest_message>"
        ),
    ]


def build_classify(router):
    """분류기 노드를 만든다. 분류가 실패해도 대화는 general 로 이어 간다."""

    async def classify(state: ChatState) -> dict:
        try:
            decision = await router.ainvoke(router_input(state["messages"]))
        except Exception as exc:
            log_event("route_failed", level=logging.WARNING, error=type(exc).__name__)
            return {"route": "general"}

        log_event("route_selected", route=decision.route, reason=decision.reason)
        return {"route": decision.route}

    return classify


def select_agent(state: ChatState) -> Route:
    """conditional edge 함수. State 의 route 값으로 다음 노드를 고른다."""
    return state["route"]


def replace_messages(messages: list[AnyMessage]) -> list:
    """부모 State 의 messages 를 통째로 바꾸는 업데이트를 만든다.

    messages 채널은 add_messages 리듀서라 기본은 "추가"다. 하위 에이전트의 요약
    미들웨어가 오래된 메시지를 지워도, 결과만 돌려주면 부모에는 지워지지 않고 남는다.
    REMOVE_ALL_MESSAGES 로 먼저 비우고 최종 목록을 넣어야 요약 결과가 저장된다.
    """
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]


def strip_message_names(messages: list[AnyMessage]) -> list[AnyMessage]:
    """AIMessage 의 name 필드를 지운다.

    Codyssey 게이트웨이는 메시지의 name 필드를 400(unsupported_feature)으로 거부한다.
    예전 에이전트(name="main_agent")가 저장한 대화에도 남아 있어서 호출 전에 지운다.
    """
    return [
        m.model_copy(update={"name": None}) if isinstance(m, AIMessage) and m.name else m
        for m in messages
    ]


def build_summarization(summary_model) -> SummarizationMiddleware:
    """하위 에이전트마다 붙이는 요약 미들웨어. 에이전트마다 새 인스턴스를 만든다."""
    return SummarizationMiddleware(
        model=summary_model,
        # 메시지 수가 아니라 토큰 기준으로 잡는다. tool call 이 늘어나면
        # "최근 N개 메시지 = N/2 턴" 이 성립하지 않기 때문이다.
        trigger=("tokens", settings.summary_trigger_tokens),
        keep=("tokens", settings.summary_keep_tokens),
        summary_prompt=SUMMARY_PROMPT,
    )


# 하위 에이전트에는 체크포인터를 주지 않는다. 대화 기록은 부모 State 에 저장된다.
# name 을 주면 답변 메시지에 name 이 붙어 게이트웨이가 거부하므로 주지 않는다.


def build_general_agent(main_model, summary_model):
    """툴 없이 답하는 일반 질문 에이전트."""
    return create_agent(
        model=main_model,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        middleware=[build_summarization(summary_model)],
    )


class SearchQuery(BaseModel):
    """모델에게 보여 주는 검색 툴 입력. 검색어 하나만 받는다."""

    query: str = Field(description="Search query, written like a person would type it.")


def build_search_tool() -> TavilySearch:
    """Tavily 웹 검색 툴. 키는 settings 에서 직접 넘긴다(.env 는 OS 환경 변수가 아니다)."""
    return TavilySearch(
        # 기본 입력 스키마에는 time_range, search_depth, include_domains 같은 선택 인자가
        # 있다. gpt-5-mini 가 이를 임의로 채워(time_range="day" 등) 결과가 0건이 되는 일이
        # 잦아서, 모델에게는 검색어만 보여 준다.
        args_schema=SearchQuery,
        max_results=settings.tavily_max_results,
        tavily_api_key=settings.tavily_api_key,
        # Tavily 가 결과를 요약한 짧은 답도 함께 받는다. 모델이 읽을 양이 줄어든다.
        include_answer=True,
        # 검색 결과가 없을 때 던지는 ToolException 을 에러 메시지로 바꿔 모델에게 돌려준다.
        # 그래야 모델이 검색어를 바꾸거나 "찾지 못했다"고 답할 수 있다.
        handle_tool_error=True,
    )


# 한 번 답할 때 허용하는 검색 횟수. 프롬프트의 "at most 2 times" 와 맞춘다.
SEARCH_CALL_LIMIT = 2


@dynamic_prompt
def search_prompt(request: ModelRequest) -> str:
    """호출 시점의 날짜를 넣는다. 서버가 며칠씩 떠 있어도 "오늘"이 맞게 풀린다."""
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d (%A)")
    return SEARCH_PROMPT.format(today=today)


def build_search_agent(main_model, summary_model):
    """웹을 검색해서 답하는 에이전트."""
    return create_agent(
        model=main_model,
        tools=[build_search_tool()],
        middleware=[
            search_prompt,
            # 검색 반복으로 60초 제한을 넘기지 않게 한 번 답할 때 최대 2번만 검색한다.
            # 넘으면 툴 대신 에러 메시지를 돌려주고, 모델은 가진 결과로 답한다.
            ToolCallLimitMiddleware(run_limit=SEARCH_CALL_LIMIT),
            build_summarization(summary_model),
        ],
    )


def build_agent_node(sub_agent):
    """하위 에이전트를 부모 그래프의 노드 함수로 감싼다."""

    async def node(state: ChatState) -> dict:
        messages = strip_message_names(state["messages"])
        result = await sub_agent.ainvoke({"messages": messages})
        return {"messages": replace_messages(result["messages"])}

    return node


def build_agent(checkpointer):
    """체크포인터를 물린 멀티 에이전트 그래프를 만든다. lifespan 에서 한 번만 호출한다."""
    # 기본 상태에서는 main과 summary 모두 OpenAI 호환 모델을 쓴다.
    # 실패 시에는 Anthropic → Gemini 순서로 자동 전환한다.
    main_model = build_model(settings.main_model_provider)
    summary_model = build_model(settings.summary_model_provider)
    # 노드별 공급자를 비우면 main 과 같은 공급자를 쓴다.
    router = build_router(settings.router_model_provider or settings.main_model_provider)

    graph = StateGraph(ChatState)
    graph.add_node("classify", build_classify(router))
    graph.add_node("general", build_agent_node(build_general_agent(main_model, summary_model)))

    # 검색 키가 없으면 search 노드를 만들지 않고 general 로 보낸다.
    destinations = {"general": "general", "search": "general", "rag": "general"}
    if settings.tavily_api_key:
        search_model = build_model(settings.search_model_provider or settings.main_model_provider)
        graph.add_node("search", build_agent_node(build_search_agent(search_model, summary_model)))
        graph.add_edge("search", END)
        destinations["search"] = "search"

    graph.add_edge(START, "classify")
    # TODO(3단계): rag 노드를 만들면 연결한다. 그 전까지는 general 이 답하되,
    # 분류 결과(route)는 State 에 그대로 남긴다.
    graph.add_conditional_edges("classify", select_agent, destinations)
    graph.add_edge("general", END)

    return graph.compile(checkpointer=checkpointer, name="chat_graph")
