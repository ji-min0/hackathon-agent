"""LangGraph 에이전트 구성.

컨텍스트 유지 전략의 핵심은 두 가지다.

1. FastAPI 는 과거 대화를 다시 조립하지 않는다. 현재 질문만 넘긴다.
2. 같은 thread_id 를 넘기면 체크포인터가 이전 State 를 복구한다.

멀티 에이전트 연결 방식 (AGENT_PATTERN)

    router      START → classify(의도 분류기) → general | search | rag → END
    single      START → single(모든 툴을 가진 에이전트 하나) → END
    supervisor  START → supervisor(담당 에이전트를 툴로 호출해 결과 종합) → END

GUARDRAIL_ENABLED 이면 세 패턴 모두 맨 앞에 guardrail 노드가 붙는다.

    START → guardrail → (거절) END
                      → (통과) 패턴의 첫 노드

자세한 내용은 docs/API_SPEC.md 5장 참고.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
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
from langchain_core.embeddings import Embeddings
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.tools import BaseTool, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel, Field

from app.chat.prompts import (
    GUARDRAIL_PROMPT,
    NO_RAG_DOCUMENTS,
    RAG_PROMPT,
    ROUTER_PROMPT,
    SEARCH_PROMPT,
    SERVICE_SCOPE,
    SINGLE_PROMPT,
    SUMMARY_PROMPT,
    SUPERVISOR_PROMPT,
    SYSTEM_PROMPT,
)
from app.core.config import settings
from app.core.logging import log_event
from app.rag import store
from app.rag.embeddings import build_embeddings


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

    # 답한 경로. router 는 분류기가 고른 값, single·supervisor 는 그 턴에 쓴 툴로 정한다.
    # 응답·로그에 함께 남겨 디버깅과 대시보드에 쓴다.
    route: str
    # 가드레일이 거절했으면 True. 거절된 턴은 에이전트를 거치지 않고 끝난다.
    blocked: bool


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


def format_documents(sources: Sequence[str]) -> str:
    """프롬프트의 {rag_documents} 자리에 넣을 문서 목록."""
    return "\n".join(f"- {source}" for source in sources) or NO_RAG_DOCUMENTS


def router_input(messages: list[AnyMessage], rag_documents: str = NO_RAG_DOCUMENTS) -> list:
    """분류기 입력을 만든다. 최근 대화를 텍스트 대본으로 바꿔 넘긴다.

    메시지 객체를 그대로 넘기면 tool call 과 tool 결과가 잘려 짝이 깨질 수 있고,
    게이트웨이가 거부하는 name 필드도 따라간다. 분류에는 텍스트만 있으면 된다.
    """
    recent = [m for m in messages if isinstance(m, HumanMessage | AIMessage) and m.text]
    *history, latest = recent[-ROUTER_HISTORY_MESSAGES:]
    lines = [f"{'user' if isinstance(m, HumanMessage) else 'assistant'}: {m.text}" for m in history]
    transcript = "\n".join(lines) or "(no earlier turns)"
    return [
        SystemMessage(ROUTER_PROMPT.format(rag_documents=rag_documents)),
        HumanMessage(
            f"<conversation>\n{transcript}\n</conversation>\n\n"
            f"<latest_message>\n{latest.text}\n</latest_message>"
        ),
    ]


def build_classify(router, rag_documents: str = NO_RAG_DOCUMENTS):
    """분류기 노드를 만든다. 분류가 실패해도 대화는 general 로 이어 간다."""

    async def classify(state: ChatState) -> dict:
        try:
            decision = await router.ainvoke(router_input(state["messages"], rag_documents))
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


def dated_prompt(template: str, **fields: str):
    """호출 시점의 날짜를 {today} 에 넣는 프롬프트 미들웨어를 만든다.

    서버가 며칠씩 떠 있어도 "오늘", "내일"이 맞게 풀린다. 나머지 자리는 fields 로 채운다.
    """

    @dynamic_prompt
    def prompt(request: ModelRequest) -> str:
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d (%A)")
        return template.format(today=today, **fields)

    return prompt


def build_search_agent(main_model, summary_model):
    """웹을 검색해서 답하는 에이전트."""
    return create_agent(
        model=main_model,
        tools=[build_search_tool()],
        middleware=[
            dated_prompt(SEARCH_PROMPT),
            # 검색 반복으로 60초 제한을 넘기지 않게 한 번 답할 때 최대 2번만 검색한다.
            # 넘으면 툴 대신 에러 메시지를 돌려주고, 모델은 가진 결과로 답한다.
            ToolCallLimitMiddleware(run_limit=SEARCH_CALL_LIMIT),
            build_summarization(summary_model),
        ],
    )


def format_passages(rows: list[dict]) -> str:
    """검색된 청크를 모델이 출처를 인용할 수 있는 형태로 이어 붙인다."""
    if not rows:
        return "No matching passages found in the documents."
    blocks = []
    for row in rows:
        page = f" p.{row['page']}" if row["page"] is not None else ""
        blocks.append(f"[{row['source']}{page}] (score {row['score']:.2f})\n{row['content']}")
    return "\n\n---\n\n".join(blocks)


def build_document_search_tool(pool, embeddings: Embeddings) -> BaseTool:
    """등록된 문서에서 질문과 뜻이 가까운 청크를 찾는 툴."""

    async def search_documents(query: str) -> str:
        """Searches the registered internal documents and returns the most relevant passages
        with their document name and page."""
        vector = await embeddings.aembed_query(query)
        return format_passages(await store.search(pool, vector, settings.rag_top_k))

    return tool(search_documents)


# RAG 에이전트가 한 번 답할 때 부를 수 있는 툴 횟수(문서 검색 2번 + 웹 검색 1번 정도).
RAG_CALL_LIMIT = 3


def build_rag_agent(rag_model, summary_model, document_tool: BaseTool, rag_documents: str):
    """문서를 근거로 답하는 에이전트. 문서에 없으면 웹 검색으로 넘어간다(Agentic RAG)."""
    tools = [document_tool]
    if settings.tavily_api_key:
        tools.append(build_search_tool())
    return create_agent(
        model=rag_model,
        tools=tools,
        middleware=[
            dated_prompt(RAG_PROMPT, rag_documents=rag_documents),
            ToolCallLimitMiddleware(run_limit=RAG_CALL_LIMIT),
            build_summarization(summary_model),
        ],
    )


# 툴 이름 → route. single·supervisor 패턴은 그 턴에 쓴 툴로 route 를 정한다.
TOOL_ROUTES = {
    "tavily_search": "search",
    "search_agent": "search",
    "search_documents": "rag",
    "rag_agent": "rag",
}

# supervisor 가 한 번 답할 때 담당을 부를 수 있는 횟수. 프롬프트의 "at most 3 times" 와 맞춘다.
SUPERVISOR_CALL_LIMIT = 3


def route_from_tools(messages: list[AnyMessage]) -> str:
    """마지막 사용자 질문 뒤에 쓴 툴로 route 를 정한다. 여러 개면 "rag+search" 처럼 합친다."""
    last_question = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0
    )
    used = {
        TOOL_ROUTES[m.name]
        for m in messages[last_question:]
        if isinstance(m, ToolMessage) and m.name in TOOL_ROUTES
    }
    return "+".join(sorted(used)) or "general"


def build_agent_node(sub_agent, *, track_route: bool = False):
    """하위 에이전트를 부모 그래프의 노드 함수로 감싼다.

    track_route 를 켜면 그 턴에 쓴 툴로 route 도 기록한다(분류기가 없는 패턴용).
    """

    async def node(state: ChatState) -> dict:
        messages = strip_message_names(state["messages"])
        result = await sub_agent.ainvoke({"messages": messages})
        update = {"messages": replace_messages(result["messages"])}
        if track_route:
            update["route"] = route_from_tools(result["messages"])
        return update

    return node


def build_agent_tool(sub_agent, name: str, description: str) -> BaseTool:
    """담당 에이전트를 supervisor 가 부를 수 있는 툴로 감싼다.

    담당은 대화 기록을 보지 못하고 task 문장만 받는다. 필요한 정보는 supervisor 가
    task 에 담아야 한다(프롬프트에 적어 둠).
    """

    async def run(task: str) -> str:
        result = await sub_agent.ainvoke({"messages": [HumanMessage(content=task)]})
        return result["messages"][-1].text

    return tool(name, description=description)(run)


GuardrailCategory = Literal["safe", "harmful", "personal_info", "prompt_injection", "off_topic"]


class GuardrailDecision(BaseModel):
    """가드레일 출력 스키마. 모델이 쓴 JSON 을 이 형식으로 검증한다."""

    allowed: bool
    category: GuardrailCategory
    reason: str


# 형식이 정해진 개인정보는 모델을 부르지 않고 바로 거절한다. 빠르고 확실하다.
SENSITIVE_PATTERNS = {
    "resident_number": re.compile(r"\b\d{6}\s*-\s*[1-4]\d{6}\b"),
    "card_number": re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b"),
}

REFUSALS: dict[str, str] = {
    "harmful": "죄송하지만 다른 사람이나 자신에게 해가 될 수 있는 요청에는 답할 수 없어요.",
    "personal_info": (
        "주민등록번호, 카드·계좌번호, 비밀번호 같은 민감한 개인정보는 보내지 말아 주세요. "
        "해당 부분을 지우고 다시 질문해 주세요."
    ),
    "prompt_injection": "죄송하지만 시스템 설정을 바꾸거나 내부 정보를 요구하는 요청은 처리할 수 없어요.",
    "off_topic": "죄송하지만 이 서비스에서 다루지 않는 주제예요. 서비스와 관련된 질문을 해 주세요.",
}


def build_guardrail(provider: str):
    """GuardrailDecision 을 돌려주는 판정기. 분류기와 같은 이유로 JSON 프롬프트를 쓴다."""
    return build_model(provider) | PydanticOutputParser(pydantic_object=GuardrailDecision)


def latest_user_text(messages: list[AnyMessage]) -> str:
    return next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), "")


def build_guardrail_node(checker):
    """가드레일 노드. 규칙(정규식) → 모델 판정 순서로 검사한다.

    판정 모델이 실패하면 통과시킨다(fail-open). 대화가 끊기지 않는 것을 우선한다.
    """

    def blocked(category: str, reason: str) -> dict:
        log_event("guardrail_blocked", category=category, reason=reason)
        return {
            "messages": [AIMessage(content=REFUSALS[category])],
            "route": "blocked",
            "blocked": True,
        }

    async def guardrail(state: ChatState) -> dict:
        text = latest_user_text(state["messages"])
        for name, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                return blocked("personal_info", name)

        try:
            decision = await checker.ainvoke(
                [
                    SystemMessage(GUARDRAIL_PROMPT.format(service_scope=SERVICE_SCOPE)),
                    HumanMessage(f"<latest_message>\n{text}\n</latest_message>"),
                ]
            )
        except Exception as exc:
            log_event("guardrail_failed", level=logging.WARNING, error=type(exc).__name__)
            return {"blocked": False}

        if not decision.allowed and decision.category in REFUSALS:
            return blocked(decision.category, decision.reason)
        return {"blocked": False}

    return guardrail


def after_guardrail(state: ChatState) -> str:
    return "blocked" if state.get("blocked") else "allowed"


@dataclass(frozen=True)
class GraphParts:
    """패턴별 그래프가 공유하는 재료. 검색·RAG 가 꺼져 있으면 해당 값이 None 이다."""

    main_model: object
    summary_model: object
    search_model: object | None
    rag_model: object | None
    document_tool: BaseTool | None
    rag_documents: str


def build_router_graph(parts: GraphParts) -> tuple[StateGraph, str]:
    """라우터 + 전문 담당. 분류기가 담당 하나를 골라 보낸다."""
    router = build_router(settings.router_model_provider or settings.main_model_provider)

    graph = StateGraph(ChatState)
    graph.add_node("classify", build_classify(router, parts.rag_documents))
    general_agent = build_general_agent(parts.main_model, parts.summary_model)
    graph.add_node("general", build_agent_node(general_agent))

    # 담당이 꺼져 있으면(키·문서 없음) 그 경로는 general 로 보낸다.
    destinations = {"general": "general", "search": "general", "rag": "general"}
    if parts.search_model is not None:
        search_agent = build_search_agent(parts.search_model, parts.summary_model)
        graph.add_node("search", build_agent_node(search_agent))
        graph.add_edge("search", END)
        destinations["search"] = "search"
    if parts.rag_model is not None:
        rag_agent = build_rag_agent(
            parts.rag_model, parts.summary_model, parts.document_tool, parts.rag_documents
        )
        graph.add_node("rag", build_agent_node(rag_agent))
        graph.add_edge("rag", END)
        destinations["rag"] = "rag"

    graph.add_conditional_edges("classify", select_agent, destinations)
    graph.add_edge("general", END)
    return graph, "classify"


def build_single_graph(parts: GraphParts) -> tuple[StateGraph, str]:
    """단일 에이전트 + 도구. 에이전트 하나가 대화 맥락과 모든 툴을 다룬다."""
    tools = []
    if parts.document_tool is not None:
        tools.append(parts.document_tool)
    if parts.search_model is not None:
        tools.append(build_search_tool())
    single_agent = create_agent(
        model=parts.main_model,
        tools=tools,
        middleware=[
            dated_prompt(SINGLE_PROMPT, rag_documents=parts.rag_documents),
            # 문서 검색과 웹 검색을 합쳐 RAG 에이전트와 같은 횟수까지 허용한다.
            ToolCallLimitMiddleware(run_limit=RAG_CALL_LIMIT),
            build_summarization(parts.summary_model),
        ],
    )

    graph = StateGraph(ChatState)
    graph.add_node("single", build_agent_node(single_agent, track_route=True))
    graph.add_edge("single", END)
    return graph, "single"


def build_supervisor_graph(parts: GraphParts) -> tuple[StateGraph, str]:
    """Supervisor + 서브 에이전트. 상위 에이전트가 일을 나눠 맡기고 결과를 합친다."""
    tools = []
    if parts.rag_model is not None:
        rag_agent = build_rag_agent(
            parts.rag_model, parts.summary_model, parts.document_tool, parts.rag_documents
        )
        tools.append(
            build_agent_tool(
                rag_agent,
                "rag_agent",
                "Answers from the registered internal documents and cites the document name "
                "and page. The task must be self-contained.",
            )
        )
    if parts.search_model is not None:
        search_agent = build_search_agent(parts.search_model, parts.summary_model)
        tools.append(
            build_agent_tool(
                search_agent,
                "search_agent",
                "Searches the web for recent or real-time facts and returns an answer with "
                "source URLs. The task must be self-contained.",
            )
        )
    supervisor = create_agent(
        model=parts.main_model,
        tools=tools,
        middleware=[
            dated_prompt(SUPERVISOR_PROMPT, rag_documents=parts.rag_documents),
            ToolCallLimitMiddleware(run_limit=SUPERVISOR_CALL_LIMIT),
            build_summarization(parts.summary_model),
        ],
    )

    graph = StateGraph(ChatState)
    graph.add_node("supervisor", build_agent_node(supervisor, track_route=True))
    graph.add_edge("supervisor", END)
    return graph, "supervisor"


GRAPH_BUILDERS = {
    "router": build_router_graph,
    "single": build_single_graph,
    "supervisor": build_supervisor_graph,
}


def build_agent(checkpointer, pool=None, rag_sources: Sequence[str] = ()):
    """AGENT_PATTERN 에 맞는 그래프를 체크포인터와 함께 만든다. lifespan 에서 한 번만 호출한다.

    rag_sources 는 rag_chunks 에 들어 있는 문서 이름 목록이다. 비어 있으면 RAG 를 끈다.
    문서를 새로 넣었으면(scripts/ingest.py) 서버를 다시 시작해야 반영된다.
    """
    # 기본 상태에서는 main과 summary 모두 OpenAI 호환 모델을 쓴다.
    # 실패 시에는 Anthropic → Gemini 순서로 자동 전환한다.
    main_model = build_model(settings.main_model_provider)
    summary_model = build_model(settings.summary_model_provider)
    # 노드별 공급자를 비우면 main 과 같은 공급자를 쓴다. 검색 키가 없으면 검색을 뺀다.
    search_model = (
        build_model(settings.search_model_provider or settings.main_model_provider)
        if settings.tavily_api_key
        else None
    )
    rag_enabled = pool is not None and bool(rag_sources)
    rag_model = (
        build_model(settings.rag_model_provider or settings.main_model_provider)
        if rag_enabled
        else None
    )
    document_tool = build_document_search_tool(pool, build_embeddings()) if rag_enabled else None

    parts = GraphParts(
        main_model=main_model,
        summary_model=summary_model,
        search_model=search_model,
        rag_model=rag_model,
        document_tool=document_tool,
        rag_documents=format_documents(rag_sources),
    )
    graph, entry = GRAPH_BUILDERS[settings.agent_pattern](parts)

    if settings.guardrail_enabled:
        checker = build_guardrail(settings.guardrail_model_provider or settings.main_model_provider)
        graph.add_node("guardrail", build_guardrail_node(checker))
        graph.add_edge(START, "guardrail")
        graph.add_conditional_edges(
            "guardrail", after_guardrail, {"blocked": END, "allowed": entry}
        )
    else:
        graph.add_edge(START, entry)

    return graph.compile(checkpointer=checkpointer, name="chat_graph")
