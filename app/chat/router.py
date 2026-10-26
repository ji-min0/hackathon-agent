"""메시지 라우터.

경로는 /api/threads/{thread_id}/messages 지만, 채팅방 CRUD 와 성격이 달라
별도 모듈로 둔다. 여기는 AI 파이프라인을 태우는 쪽이다.

- /api/threads/{id}/messages, /api/threads/{id}/resume : 웹 화면이 쓴다.
- POST /chat : 외부·평가 스크립트용 단일 진입점. 새 질문과 승인·거절을 한 곳에서 받는다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.deps import CurrentUser, get_current_user
from app.chat import service
from app.chat.schemas import (
    ChatLogResponse,
    ChatRequest,
    MessageRequest,
    MessageResponse,
    ResumeRequest,
)
from app.core.deps import get_agent, get_pool
from app.threads import service as threads_service

router = APIRouter(prefix="/api/threads", tags=["chat"])
chat_router = APIRouter(tags=["chat"])

# 새 대화방 제목으로 쓸 첫 질문 글자 수.
TITLE_LENGTH = 30


def to_response(thread_id: UUID, reply: service.ChatReply) -> MessageResponse:
    return MessageResponse(
        thread_id=thread_id,
        answer=reply.answer,
        route=reply.route,
        status=reply.status,
        pending=reply.pending,
    )


@router.get("/{thread_id}/messages")
async def list_messages(
    thread_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    pool=Depends(get_pool),
) -> list[ChatLogResponse]:
    # 체크포인트가 아니라 chat_logs 를 읽는다. 요약되지 않은 원본이 필요하다.
    logs = await service.list_messages(pool=pool, thread_id=thread_id, user_id=user.id)
    return [ChatLogResponse(**log) for log in logs]


@router.post("/{thread_id}/messages")
async def send_message(
    thread_id: UUID,
    body: MessageRequest,
    user: CurrentUser = Depends(get_current_user),
    pool=Depends(get_pool),
    agent=Depends(get_agent),
) -> MessageResponse:
    reply = await service.send_message(
        pool=pool,
        agent=agent,
        thread_id=thread_id,
        user_id=user.id,
        question=body.message,
    )
    return to_response(thread_id, reply)


@router.post("/{thread_id}/resume")
async def resume(
    thread_id: UUID,
    body: ResumeRequest,
    user: CurrentUser = Depends(get_current_user),
    pool=Depends(get_pool),
    agent=Depends(get_agent),
) -> MessageResponse:
    """휴먼인더루프: 승인을 기다리는 툴 실행을 승인·거절하고 이어서 답한다."""
    reply = await service.send_message(
        pool=pool,
        agent=agent,
        thread_id=thread_id,
        user_id=user.id,
        decision=body.decision,
    )
    return to_response(thread_id, reply)


@chat_router.post("/chat")
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    pool=Depends(get_pool),
    agent=Depends(get_agent),
) -> MessageResponse:
    """단일 진입점. thread_id 없이 message 를 보내면 새 대화방을 만든다."""
    thread_id = body.thread_id
    if thread_id is None:
        thread = await threads_service.create_thread(pool, user.id, body.message[:TITLE_LENGTH])
        thread_id = thread["id"]

    reply = await service.send_message(
        pool=pool,
        agent=agent,
        thread_id=thread_id,
        user_id=user.id,
        question=body.message,
        decision=body.resume,
    )
    return to_response(thread_id, reply)
