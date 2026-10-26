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
import time
from uuid import UUID

from app.chat import repository
from app.chat.concurrency import thread_run_guard
from app.core.config import settings
from app.core.errors import AITimeout, AIUpstreamError, Conflict
from app.core.logging import log_event
from app.threads import service as threads_service


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
    question: str,
) -> str:
    await threads_service.require_owned(pool, thread_id, user_id)

    log_event("ai_call_start", thread_id=thread_id)
    started = time.perf_counter()

    # 과거 대화는 직접 다시 조립하지 않는다. 같은 thread_id를 넘기면
    # AsyncPostgresSaver가 이전 State를 복구한다.
    try:
        # 같은 대화방은 한 번에 하나의 State 갱신만 허용한다.
        async with thread_run_guard.acquire(thread_id):
            # 요약과 main·fallback 모델 호출을 모두 포함한 사용자 요청 전체 제한이다.
            async with asyncio.timeout(settings.ai_timeout_seconds):
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": question}]},
                    config={"configurable": {"thread_id": str(thread_id)}},
                )
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
    answer = result["messages"][-1].content
    if not isinstance(answer, str):
        answer = str(answer)

    latency_ms = int((time.perf_counter() - started) * 1000)
    log_event("ai_call_success", latency_ms=latency_ms)

    # DB 저장에 실패해도 이미 받은 답변은 사용자에게 돌려준다.
    try:
        chat_id = await repository.save_success(
            pool,
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            answer=answer,
            latency_ms=latency_ms,
        )
        log_event("db_save_success", chat_id=chat_id)
    except Exception as exc:
        log_event("db_save_failed", level=logging.ERROR, error=type(exc).__name__)

    return answer


async def list_messages(*, pool, thread_id: UUID, user_id: UUID) -> list[dict]:
    await threads_service.require_owned(pool, thread_id, user_id)
    return await repository.list_by_thread(pool, thread_id)
