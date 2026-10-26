"""채팅방 비즈니스 로직.

소유권 확인과 체크포인트 정리처럼 "DB 한 번으로 끝나지 않는" 일을 여기서 조립한다.
"""

from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.core.errors import NotFound
from app.threads import repository


async def create_thread(pool, user_id: UUID, title: str | None) -> dict:
    return await repository.create(pool, user_id, title)


async def list_threads(pool, user_id: UUID) -> list[dict]:
    return await repository.list_by_user(pool, user_id)


async def require_owned(pool, thread_id: UUID, user_id: UUID) -> dict:
    """소유한 thread 를 돌려준다. 없으면 NotFound 를 던진다."""
    thread = await repository.get_owned(pool, thread_id, user_id)
    if thread is None:
        raise NotFound("대화를 찾을 수 없습니다.")
    return thread


async def rename_thread(pool, thread_id: UUID, user_id: UUID, title: str) -> dict:
    thread = await repository.update_title(pool, thread_id, user_id, title)
    if thread is None:
        raise NotFound("대화를 찾을 수 없습니다.")
    return thread


async def delete_thread(
    pool: AsyncConnectionPool, checkpointer, thread_id: UUID, user_id: UUID
) -> None:
    """thread 와 대화 로그, 체크포인트까지 정리한다."""
    deleted = await repository.delete(pool, thread_id, user_id)
    if not deleted:
        raise NotFound("대화를 찾을 수 없습니다.")

    # 체크포인트는 CASCADE 대상이 아니다. 빠뜨리면 삭제한 대화의 State 가
    # 계속 남아 Supabase 무료 플랜 500MB 를 잠식한다.
    await checkpointer.adelete_thread(str(thread_id))
