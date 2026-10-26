"""Supabase PostgreSQL 커넥션 풀.

풀은 lifespan 에서 한 번만 열고 애플리케이션 전체가 공유한다.
LangGraph 체크포인터도 같은 풀을 사용한다.
"""

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

# LangGraph 가 checkpointer.setup() 에서 직접 만드는 테이블들.
# 우리 schema.sql 이 닿지 않으므로 여기서 따로 다룬다.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoint_migrations",
)


def create_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        # Supabase 무료 플랜은 동시 연결 수가 제한적이라 작게 잡는다.
        max_size=5,
        kwargs={
            # checkpointer.setup() 이 DDL 을 실행하므로 필요하다.
            "autocommit": True,
            # Transaction 모드 풀러(6543)는 prepared statement 를 지원하지 않는다.
            # Session 모드(5432)를 쓰더라도 켜 두면 안전하다.
            "prepare_threshold": None,
        },
        open=False,
    )


async def secure_checkpoint_tables(pool: AsyncConnectionPool) -> None:
    """체크포인트 테이블에 RLS 를 켠다. 정책은 만들지 않는다.

    Supabase 는 public 스키마를 PostgREST 로 자동 노출하고, anon 키는 클라이언트에
    배포되는 공개 키다. 체크포인트에는 대화 메시지 State 가 그대로 들어가므로
    그대로 두면 외부에서 읽을 수 있다.

    정책이 하나도 없으면 PostgREST 경유 접근은 전부 차단되고,
    서버는 소유자 역할로 접속하므로 RLS 를 우회해 영향받지 않는다.

    LangGraph 가 마이그레이션에서 테이블을 다시 만들 수 있으므로
    시작할 때마다 실행한다. 이미 켜져 있으면 아무 일도 하지 않는다.
    """
    async with pool.connection() as conn:
        for table in CHECKPOINT_TABLES:
            await conn.execute(
                sql.SQL("ALTER TABLE IF EXISTS public.{} ENABLE ROW LEVEL SECURITY").format(
                    sql.Identifier(table)
                )
            )


async def ping(pool: AsyncConnectionPool) -> None:
    """DB 가 살아 있는지 확인한다. /health 가 호출한다.

    Supabase 무료 플랜은 7일 무활동 시 프로젝트가 일시정지되므로,
    이 질의가 유휴 방지 역할도 겸한다.
    """
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
