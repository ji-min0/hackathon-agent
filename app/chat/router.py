"""메시지 라우터.

경로는 /api/threads/{thread_id}/messages 지만, 채팅방 CRUD 와 성격이 달라
별도 모듈로 둔다. 여기는 AI 파이프라인을 태우는 쪽이다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.deps import CurrentUser, get_current_user
from app.chat import service
from app.chat.schemas import ChatLogResponse, MessageRequest, MessageResponse
from app.core.deps import get_agent, get_pool

router = APIRouter(prefix="/api/threads", tags=["chat"])


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
    return MessageResponse(thread_id=thread_id, answer=reply.answer, route=reply.route)
