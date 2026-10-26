"""실제 PostgreSQL(pgvector)에서 SQL 을 확인한다. 다른 테스트는 DB 를 가짜로 바꾸므로
관리자 화면·RAG 의 SQL 은 여기서만 실제로 돈다.

    make test-db   # Docker 로 pgvector 를 잠깐 띄워 이 파일만 돌린다

TEST_DATABASE_URL 이 없으면 건너뛴다(make check 에서는 돌지 않는다).
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from app.admin import repository as admin_repository
from app.rag import store
from app.rag.chunking import Chunk

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL 이 없다")

SCHEMA = Path(__file__).parents[2] / "scripts" / "schema.sql"
# Supabase 에만 있는 것을 흉내 낸다: Auth 의 사용자 테이블, 확장을 두는 extensions 스키마.
SUPABASE_STUB = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);
CREATE SCHEMA IF NOT EXISTS extensions;
"""


@pytest_asyncio.fixture
async def pool():
    # Supabase 처럼 extensions 스키마를 search_path 에 넣어야 vector 타입을 찾는다.
    pool = AsyncConnectionPool(
        URL,
        kwargs={"autocommit": True, "options": "-c search_path=public,extensions"},
        open=False,
    )
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SUPABASE_STUB)
        await conn.execute(SCHEMA.read_text())
        await conn.execute("TRUNCATE prompts, rag_chunks, chat_threads CASCADE")
    yield pool
    await pool.close()


def one_hot(index: int) -> list[float]:
    vector = [0.0] * 768
    vector[index] = 1.0
    return vector


@pytest.mark.asyncio
async def test_prompt_versions_and_failed_activation_keeps_active(pool) -> None:
    assert await admin_repository.create_version(pool, "SERVICE_SCOPE", "v1", None, "a@x") == 1
    assert await admin_repository.create_version(pool, "SERVICE_SCOPE", "v2", "메모", "a@x") == 2
    assert await admin_repository.active_prompts(pool) == {"SERVICE_SCOPE": "v2"}

    # 없는 버전을 고르면 트랜잭션을 되돌려 기존 활성 버전이 그대로여야 한다.
    with pytest.raises(LookupError):
        await admin_repository.activate_version(pool, "SERVICE_SCOPE", 9)
    assert await admin_repository.active_prompts(pool) == {"SERVICE_SCOPE": "v2"}

    await admin_repository.activate_version(pool, "SERVICE_SCOPE", 1)
    assert await admin_repository.active_prompts(pool) == {"SERVICE_SCOPE": "v1"}

    # 0 은 코드 기본값으로 되돌리기다.
    await admin_repository.activate_version(pool, "SERVICE_SCOPE", 0)
    assert await admin_repository.active_prompts(pool) == {}


@pytest.mark.asyncio
async def test_dashboard_counts_routes_and_errors(pool) -> None:
    user_id, thread_id = uuid4(), uuid4()
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (user_id,))
        await conn.execute(
            "INSERT INTO chat_threads (id, user_id) VALUES (%s, %s)", (thread_id, user_id)
        )
        await conn.execute(
            "INSERT INTO chat_logs (thread_id, user_id, question, answer, status, latency_ms, route)"
            " VALUES (%s, %s, 'q1', 'a1', 'success', 1000, 'rag'),"
            "        (%s, %s, 'q2', 'a2', 'success', 3000, 'rag'),"
            "        (%s, %s, 'q3', NULL, 'error', NULL, NULL)",
            (thread_id, user_id) * 3,
        )

    data = await admin_repository.dashboard(pool, 7)

    assert (data["summary"]["total"], data["summary"]["errors"], data["summary"]["users"]) == (
        3,
        1,
        1,
    )
    assert [(r["route"], r["count"], int(r["avg_ms"])) for r in data["routes"]] == [
        ("rag", 2, 2000)
    ]
    assert len(data["recent"]) == 3


@pytest.mark.asyncio
async def test_rag_search_returns_closest_chunk_and_replace_does_not_duplicate(pool) -> None:
    chunks = [Chunk("guide.pdf", page, f"내용 {page}") for page in (1, 2, 3)]
    vectors = [one_hot(0), one_hot(1), one_hot(2)]

    await store.replace_source(pool, "guide.pdf", chunks, vectors)
    await store.replace_source(pool, "guide.pdf", chunks, vectors)  # 다시 넣어도 교체만 된다

    # 2쪽과 가장 가깝고, 그다음 1쪽과 가깝다(3쪽과는 무관).
    query = one_hot(1)
    query[0] = 0.3
    rows = await store.search(pool, query, 2)

    assert [r["page"] for r in rows] == [2, 1]
    assert rows[0]["score"] > rows[1]["score"] > 0
    assert await store.list_sources(pool) == ["guide.pdf"]
    async with pool.connection() as conn:
        count = await (await conn.execute("SELECT count(*) FROM rag_chunks")).fetchone()
    assert count[0] == 3
