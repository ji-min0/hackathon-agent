"""메시지 전송 파이프라인.

TODO(챗 담당): 전체 구현

처리 순서 (docs/API_SPEC.md 7.5)
    1. thread 소유권 확인      threads.service.require_owned
    2. LangGraph 호출          현재 질문만 넘긴다. 과거 대화는 넘기지 않는다
    3. 응답 반환
    4. chat_logs 저장          실패도 status='error' 로 기록
    5. updated_at 갱신         DB 트리거가 처리

로그 이벤트: ai_call_start / ai_call_success / ai_call_failed / db_save_success
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from langgraph.types import Command

from app.chat import repository
from app.chat.agent import TOOL_ROUTES
from app.chat.concurrency import thread_run_guard
from app.core.config import settings
from app.core.errors import AITimeout, AIUpstreamError, Conflict, InvalidInput
from app.core.logging import log_event
from app.threads import service as threads_service

Decision = Literal["approve", "reject"]

# 거절하면 툴 결과 대신 모델에게 전달하는 문장.
REJECT_MESSAGE = (
    "The user chose not to run this action. Do not run it. Briefly say that you skipped it as "
    "the user asked, then help without it or suggest what the user can do instead."
)


@dataclass(frozen=True)
class ChatReply:
    answer: str
    # 답한 경로(rag | search | general | blocked ...). 디버깅·대시보드용.
    route: str
    # done: 답변 완료 / interrupted: 툴 실행 승인을 기다리는 중(휴먼인더루프)
    status: Literal["done", "interrupted"] = "done"
    # 승인을 기다리는 툴 호출 목록. status=interrupted 일 때만 채운다.
    pending: list[dict] = field(default_factory=list)


# 승인 안내에 보여 줄 툴별 문장. 없는 툴은 이름과 인자를 풀어서 보여 준다.
APPROVAL_LABELS = {
    "tavily_search": lambda args: f"웹에서 '{args.get('query', '')}'을(를) 검색",
    "search_documents": lambda args: f"등록된 문서에서 '{args.get('query', '')}'을(를) 검색",
    "search_agent": lambda args: f"검색 담당에게 '{args.get('task', '')}' 작업을 맡기기",
    "rag_agent": lambda args: f"문서 담당에게 '{args.get('task', '')}' 작업을 맡기기",
}


def describe_action(action: dict) -> str:
    label = APPROVAL_LABELS.get(action["name"])
    if label:
        return f"- {label(action['args'])}"
    details = "".join(f"\n  · {key}: {value}" for key, value in action["args"].items())
    return f"- '{action['name']}' 작업을 실행{details}"


def approval_text(pending: list[dict]) -> str:
    """승인을 기다리는 동안 사용자에게 보여 줄 안내 문장. 사용자는 채팅으로 답한다."""
    lines = "\n".join(describe_action(action) for action in pending)
    return f"다음 작업을 진행할까요? '네' 또는 '아니요'로 답해 주세요.\n{lines}"


# 승인 대기 중에 채팅으로 온 짧은 답을 승인·거절로 읽는다. 앞부분만 맞으면 된다("네 해주세요").
APPROVE_WORDS = ("네", "예", "응", "ㅇㅇ", "그래", "좋아", "승인", "진행", "해줘", "ok", "yes")
REJECT_WORDS = ("아니", "아뇨", "싫어", "거절", "취소", "하지마", "ㄴㄴ", "no")
# 이보다 길면 예/아니오 답이 아니라 새 질문으로 본다("아니 그냥 환율 알려줘").
DECISION_MAX_LENGTH = 7


def parse_decision(text: str) -> Decision | None:
    normalized = re.sub(r"[\s.,!?~ㅎㅋ]", "", text.lower())
    if not normalized or len(normalized) > DECISION_MAX_LENGTH:
        return None
    # 애매할 때 실행하지 않는 쪽이 안전해서 거절을 먼저 확인한다.
    if normalized.startswith(REJECT_WORDS):
        return "reject"
    if normalized.startswith(APPROVE_WORDS):
        return "approve"
    return None


async def build_resume(agent, config: dict, decision: Decision) -> Command:
    """멈춘 지점마다 같은 결정을 담아 재개 명령을 만든다."""
    snapshot = await agent.aget_state(config)
    if not snapshot.interrupts:
        raise InvalidInput("승인을 기다리는 작업이 없습니다.")

    answer = (
        {"type": "approve"}
        if decision == "approve"
        else {
            "type": "reject",
            "message": REJECT_MESSAGE,
        }
    )
    # 한 번 멈출 때 툴 호출 여러 개를 묶어서 물어보므로 호출 수만큼 결정을 넣는다.
    return Command(
        resume={
            item.id: {"decisions": [answer] * len(item.value["action_requests"])}
            for item in snapshot.interrupts
        }
    )


def parse_result(result: dict) -> ChatReply:
    """그래프 결과를 응답으로 바꾼다. 멈췄으면 승인 안내를 답으로 돌려준다."""
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        pending = [
            {"name": action["name"], "args": action["args"]}
            for item in interrupts
            for action in item.value.get("action_requests", [])
        ]
        routes = sorted({TOOL_ROUTES.get(action["name"], "general") for action in pending})
        return ChatReply(
            answer=approval_text(pending),
            route="+".join(routes),
            status="interrupted",
            pending=pending,
        )

    answer = result["messages"][-1].content
    if not isinstance(answer, str):
        answer = str(answer)
    return ChatReply(answer=answer, route=result.get("route", "general"))


async def save_error_log(*, pool, thread_id, user_id, question: str, error: Exception) -> None:
    """AI 실패를 기록하되, 로그 DB 실패가 원래 오류를 덮어쓰지 않게 한다."""
    try:
        chat_id = await repository.save_error(
            pool,
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            error_message=f"{type(error).__name__}: {error}",
        )
        log_event("db_save_success", chat_id=chat_id)
    except Exception as exc:
        log_event("db_save_failed", level=logging.ERROR, error=type(exc).__name__)


async def send_message(
    *,
    pool,
    agent,
    thread_id: UUID,
    user_id: UUID,
    question: str | None = None,
    decision: Decision | None = None,
) -> ChatReply:
    """질문을 보내거나(question), 멈춘 작업을 승인·거절한다(decision). 둘 중 하나만 준다.

    승인을 기다리는 중에 "네"/"아니요" 같은 짧은 답이 question 으로 오면 승인·거절로 처리한다.
    그 밖의 메시지는 새 질문으로 보고, 기다리던 작업은 버린다.
    """
    if (question is None) == (decision is None):
        raise InvalidInput("message 와 resume 중 하나만 보내 주세요.")
    await threads_service.require_owned(pool, thread_id, user_id)
    config = {"configurable": {"thread_id": str(thread_id)}}
    if question is not None and (answer := parse_decision(question)):
        snapshot = await agent.aget_state(config)
        if snapshot.interrupts:
            decision = answer

    if decision is not None:
        payload = await build_resume(agent, config, decision)
        # chat_logs.question 은 비울 수 없어서, API 로 결정만 온 경우 결정 내용을 남긴다.
        question = question or ("[승인]" if decision == "approve" else "[거절]")
    else:
        payload = {"messages": [{"role": "user", "content": question}]}

    log_event("ai_call_start", thread_id=thread_id)
    started = time.perf_counter()

    # 과거 대화는 직접 다시 조립하지 않는다. 같은 thread_id를 넘기면
    # AsyncPostgresSaver가 이전 State를 복구한다.
    try:
        # 같은 대화방은 한 번에 하나의 State 갱신만 허용한다.
        async with thread_run_guard.acquire(thread_id):
            # 요약과 main·fallback 모델 호출을 모두 포함한 사용자 요청 전체 제한이다.
            async with asyncio.timeout(settings.ai_timeout_seconds):
                result = await agent.ainvoke(payload, config=config)
    except TimeoutError as exc:
        log_event("ai_call_failed", level=logging.ERROR, error_code="AI_TIMEOUT")
        await save_error_log(
            pool=pool,
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            error=exc,
        )
        raise AITimeout(detail="agent invocation timed out") from exc
    except Conflict:
        # 동일 thread의 진행 중인 요청은 AI 실패가 아니라 409로 그대로 돌려준다.
        raise
    except Exception as exc:
        log_event(
            "ai_call_failed",
            level=logging.ERROR,
            error_code="AI_UPSTREAM_ERROR",
            error=type(exc).__name__,
        )
        await save_error_log(
            pool=pool,
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            error=exc,
        )
        raise AIUpstreamError(detail=f"{type(exc).__name__}: {exc}") from exc
    reply = parse_result(result)

    latency_ms = int((time.perf_counter() - started) * 1000)
    log_event("ai_call_success", latency_ms=latency_ms, route=reply.route, status=reply.status)

    # DB 저장에 실패해도 이미 받은 답변은 사용자에게 돌려준다.
    try:
        chat_id = await repository.save_success(
            pool,
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            answer=reply.answer,
            latency_ms=latency_ms,
            route=reply.route,
        )
        log_event("db_save_success", chat_id=chat_id)
    except Exception as exc:
        log_event("db_save_failed", level=logging.ERROR, error=type(exc).__name__)

    return reply


async def list_messages(*, pool, thread_id: UUID, user_id: UUID) -> list[dict]:
    await threads_service.require_owned(pool, thread_id, user_id)
    return await repository.list_by_thread(pool, thread_id)
