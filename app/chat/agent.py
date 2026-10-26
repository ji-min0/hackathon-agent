"""LangGraph 에이전트 구성.

컨텍스트 유지 전략의 핵심은 두 가지다.

1. FastAPI 는 과거 대화를 다시 조립하지 않는다. 현재 질문만 넘긴다.
2. 같은 thread_id 를 넘기면 체크포인터가 이전 State 를 복구한다.

멀티 에이전트 그래프 흐름

    START → classify(의도 분류기) → general | search | rag → END

자세한 내용은 docs/API_SPEC.md 5장 참고.
"""

from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage, RemoveMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from app.chat.prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from app.core.config import settings


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


def classify(state: ChatState) -> dict:
    """질문 의도를 분류해 route 를 정한다.

    TODO(2단계): structured output 분류기로 교체한다. 지금은 그래프 연결 확인용으로
    항상 general 을 돌려준다.
    """
    return {"route": "general"}


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


def build_general_agent(main_model, summary_model):
    """툴 없이 답하는 일반 질문 에이전트."""
    middleware = [
        SummarizationMiddleware(
            model=summary_model,
            # 메시지 수가 아니라 토큰 기준으로 잡는다. tool call 이 늘어나면
            # "최근 N개 메시지 = N/2 턴" 이 성립하지 않기 때문이다.
            trigger=("tokens", settings.summary_trigger_tokens),
            keep=("tokens", settings.summary_keep_tokens),
            summary_prompt=SUMMARY_PROMPT,
        )
    ]
    # 체크포인터는 부모 그래프만 가진다. 대화 기록은 부모 State 에 저장된다.
    return create_agent(
        model=main_model,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        name="general_agent",
    )


def build_agent(checkpointer):
    """체크포인터를 물린 멀티 에이전트 그래프를 만든다. lifespan 에서 한 번만 호출한다."""
    # 기본 상태에서는 main과 summary 모두 OpenAI 호환 모델을 쓴다.
    # 실패 시에는 Anthropic → Gemini 순서로 자동 전환한다.
    main_model = build_model(settings.main_model_provider)
    summary_model = build_model(settings.summary_model_provider)
    general_agent = build_general_agent(main_model, summary_model)

    async def general(state: ChatState) -> dict:
        result = await general_agent.ainvoke({"messages": state["messages"]})
        return {"messages": replace_messages(result["messages"])}

    graph = StateGraph(ChatState)
    graph.add_node("classify", classify)
    graph.add_node("general", general)

    graph.add_edge(START, "classify")
    # TODO(2단계): search, rag 노드를 만들면 여기에 경로를 추가한다.
    graph.add_conditional_edges("classify", select_agent, {"general": "general"})
    graph.add_edge("general", END)

    return graph.compile(checkpointer=checkpointer, name="chat_graph")
