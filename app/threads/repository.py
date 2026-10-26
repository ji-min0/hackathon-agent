"""chat_threads 테이블 접근 계층.

TODO(챗 담당): 전체 구현

SQL 은 여기에만 둔다. 라우터와 서비스는 이 모듈을 통해서만 DB 에 닿는다.
스키마는 scripts/schema.sql 참고.
"""

from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# updated_at 은 DB 트리거가 갱신하므로 UPDATE 문에 직접 넣지 않는다.
COLUMNS = "id, title, created_at, updated_at"


async def create(pool: AsyncConnectionPool, user_id: UUID, title: str | None) -> dict:
    """채팅방을 만들고, DB가 생성한 UUID를 LangGraph thread_id로 돌려준다."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            if title is None:
                await cursor.execute(
                    f"INSERT INTO chat_threads (user_id) VALUES (%s) RETURNING {COLUMNS}",
                    (user_id,),
                )
            else:
                await cursor.execute(
                    f"INSERT INTO chat_threads (user_id, title) VALUES (%s, %s) "
                    f"RETURNING {COLUMNS}",
                    (user_id, title),
                )
            return await cursor.fetchone()


async def list_by_user(pool: AsyncConnectionPool, user_id: UUID) -> list[dict]:
    """현재 사용자의 채팅방을 최근 활동 순으로 조회한다."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {COLUMNS} FROM chat_threads WHERE user_id = %s ORDER BY updated_at DESC",
                (user_id,),
            )
            return await cursor.fetchall()


async def get_owned(pool: AsyncConnectionPool, thread_id: UUID, user_id: UUID) -> dict | None:
    """소유권까지 함께 확인한다. 없거나 남의 것이면 None.

    라우터는 None 을 받으면 403 이 아니라 404 로 응답한다.
    403 을 주면 그 ID 의 thread 가 존재한다는 사실이 노출된다.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {COLUMNS} FROM chat_threads WHERE id = %s AND user_id = %s",
                (thread_id, user_id),
            )
            return await cursor.fetchone()


async def update_title(
    pool: AsyncConnectionPool, thread_id: UUID, user_id: UUID, title: str
) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"UPDATE chat_threads SET title = %s "
                "WHERE id = %s AND user_id = %s "
                f"RETURNING {COLUMNS}",
                (title, thread_id, user_id),
            )
            return await cursor.fetchone()


async def delete(pool: AsyncConnectionPool, thread_id: UUID, user_id: UUID) -> bool:
    """삭제되면 True. chat_logs 는 ON DELETE CASCADE 로 함께 지워진다.

    LangGraph 체크포인트는 CASCADE 대상이 아니다. 서비스 계층에서 따로 지운다.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM chat_threads WHERE id = %s AND user_id = %s RETURNING id",
                (thread_id, user_id),
            )
            return await cursor.fetchone() is not None
