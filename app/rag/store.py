"""rag_chunks 테이블 접근 계층. 테이블은 scripts/schema.sql 에서 만든다.

벡터 검색은 pgvector 의 <=> (코사인 거리) 연산자를 쓴다. 거리가 작을수록 뜻이 가깝다.
"""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.rag.chunking import Chunk


def to_pgvector(vector: list[float]) -> str:
    """파이썬 리스트를 pgvector 입력 문자열('[0.1,0.2,...]')로 바꾼다."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


async def replace_source(
    pool: AsyncConnectionPool, source: str, chunks: list[Chunk], vectors: list[list[float]]
) -> int:
    """같은 문서의 기존 청크를 지우고 새로 넣는다. 같은 파일을 다시 넣어도 중복되지 않는다."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM rag_chunks WHERE source = %s", (source,))
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO rag_chunks (source, page, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    [
                        (chunk.source, chunk.page, index, chunk.content, to_pgvector(vector))
                        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                    ],
                )
    return len(chunks)


async def search(pool: AsyncConnectionPool, vector: list[float], k: int) -> list[dict]:
    """질문 벡터와 가장 가까운 청크 k 개를 돌려준다. score 는 1에 가까울수록 비슷하다."""
    query_vector = to_pgvector(vector)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT source, page, content, 1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
                (query_vector, query_vector, k),
            )
            return await cursor.fetchall()


async def list_sources(pool: AsyncConnectionPool) -> list[str]:
    """등록된 문서 이름 목록. 분류기 프롬프트에 "RAG 에 어떤 문서가 있는지" 알려 줄 때 쓴다."""
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT DISTINCT source FROM rag_chunks ORDER BY source")
            return [row[0] for row in await cursor.fetchall()]
