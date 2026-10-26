"""관리자 페이지용 DB 접근. 테이블은 scripts/schema.sql 의 6번 블록에서 만든다."""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# 응답 시간 통계. 실패한 요청은 latency_ms 가 비어 있어 자연히 빠진다.
LATENCY = (
    "round(avg(latency_ms)) AS avg_ms, "
    "round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_ms"
)


async def fetch_all(pool: AsyncConnectionPool, query: str, params: tuple = ()) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()


async def dashboard(pool: AsyncConnectionPool, days: int) -> dict:
    """최근 days 일의 요약, 경로별·일별 집계, 최근 로그를 돌려준다."""
    since = "created_at >= now() - make_interval(days => %s)"
    summary = await fetch_all(
        pool,
        f"SELECT count(*) AS total, count(*) FILTER (WHERE status = 'error') AS errors, "
        f"count(DISTINCT user_id) AS users, {LATENCY} FROM chat_logs WHERE {since}",
        (days,),
    )
    routes = await fetch_all(
        pool,
        f"SELECT coalesce(route, '(기록 없음)') AS route, count(*) AS count, {LATENCY} "
        f"FROM chat_logs WHERE {since} AND status = 'success' GROUP BY 1 ORDER BY count DESC",
        (days,),
    )
    daily = await fetch_all(
        pool,
        "SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day, count(*) AS count, "
        "count(*) FILTER (WHERE status = 'error') AS errors "
        f"FROM chat_logs WHERE {since} GROUP BY 1 ORDER BY 1",
        (days,),
    )
    recent = await fetch_all(
        pool,
        "SELECT id, created_at, route, status, latency_ms, left(question, 80) AS question, "
        "left(coalesce(answer, error_message), 120) AS answer "
        "FROM chat_logs ORDER BY id DESC LIMIT 30",
    )
    return {"summary": summary[0], "routes": routes, "daily": daily, "recent": recent}


async def active_prompts(pool: AsyncConnectionPool) -> dict[str, str]:
    rows = await fetch_all(pool, "SELECT name, content FROM prompts WHERE is_active")
    return {row["name"]: row["content"] for row in rows}


async def prompt_versions(pool: AsyncConnectionPool, name: str) -> list[dict]:
    return await fetch_all(
        pool,
        "SELECT version, content, is_active, note, created_by, created_at "
        "FROM prompts WHERE name = %s ORDER BY version DESC",
        (name,),
    )


async def create_version(
    pool: AsyncConnectionPool, name: str, content: str, note: str | None, created_by: str
) -> int:
    """새 버전을 만들고 바로 활성화한다. 기존 활성 버전은 내린다."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cursor = await conn.execute(
                "SELECT coalesce(max(version), 0) + 1 FROM prompts WHERE name = %s", (name,)
            )
            version = (await cursor.fetchone())[0]
            await conn.execute("UPDATE prompts SET is_active = false WHERE name = %s", (name,))
            await conn.execute(
                "INSERT INTO prompts (name, version, content, is_active, note, created_by) "
                "VALUES (%s, %s, %s, true, %s, %s)",
                (name, version, content, note, created_by),
            )
    return version


async def activate_version(pool: AsyncConnectionPool, name: str, version: int) -> bool:
    """지정한 버전을 활성화한다. version=0 이면 모두 내려 코드 기본값으로 돌아간다.

    없는 버전이면 LookupError 를 던지고 아무것도 바꾸지 않는다.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE prompts SET is_active = false WHERE name = %s", (name,))
            if version == 0:
                return True
            cursor = await conn.execute(
                "UPDATE prompts SET is_active = true WHERE name = %s AND version = %s",
                (name, version),
            )
            if cursor.rowcount != 1:
                # 예외로 트랜잭션을 되돌려야 기존 활성 버전이 내려간 채로 남지 않는다.
                raise LookupError(f"{name} v{version}")
            return True
