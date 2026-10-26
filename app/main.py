"""애플리케이션 진입점.

실행
    uvicorn app.main:app --reload
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# 체크포인트 역직렬화 시 허용되지 않은 타입을 제한한다.
# LangGraph import 전에 설정해야 한다.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from supabase import AsyncClientOptions, create_async_client  # noqa: E402

from app.auth import router as auth_router  # noqa: E402
from app.chat import router as chat_router  # noqa: E402
from app.chat.agent import build_agent  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import create_pool, ping, secure_checkpoint_tables  # noqa: E402
from app.core.errors import register_error_handlers  # noqa: E402
from app.core.logging import log_event, setup_logging  # noqa: E402
from app.core.middleware import RequestContextMiddleware  # noqa: E402
from app.rag import store as rag_store  # noqa: E402
from app.threads import router as threads_router  # noqa: E402
from app.web import router as web_router  # noqa: E402


async def load_rag_sources(pool) -> list[str]:
    """RAG 문서 목록을 읽는다. 테이블이 아직 없어도 서버는 RAG 없이 뜬다."""
    try:
        return await rag_store.list_sources(pool)
    except Exception as exc:
        log_event("rag_sources_unavailable", level=logging.WARNING, error=type(exc).__name__)
        return []


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)

    pool = create_pool()
    await pool.open()

    checkpointer = AsyncPostgresSaver(pool)
    # 체크포인트 테이블을 만든다. 이미 있으면 아무 일도 하지 않는다.
    await checkpointer.setup()
    # 방금 만들어진 테이블은 RLS 가 꺼진 상태다. 바로 막는다.
    await secure_checkpoint_tables(pool)

    app.state.pool = pool
    app.state.checkpointer = checkpointer
    rag_sources = await load_rag_sources(pool)
    app.state.agent = build_agent(checkpointer, pool=pool, rag_sources=rag_sources)
    # 동기 클라이언트는 네트워크 호출이 이벤트 루프를 막으므로 async 로 만든다.
    #
    # 클라이언트 하나를 모든 요청이 공유한다. 세션을 클라이언트에 저장하면
    # 마지막에 로그인한 사용자의 세션이 남아 다른 요청에 섞일 수 있으므로 끈다.
    # 사용자 식별은 항상 요청 쿠키의 토큰을 get_user(token) 에 넘겨서 한다.
    app.state.supabase = await create_async_client(
        settings.supabase_url,
        settings.supabase_anon_key,
        options=AsyncClientOptions(
            auto_refresh_token=False,
            persist_session=False,
        ),
    )

    # 어떤 공급자·모델로 떴는지 남긴다. 배포 후 로그만 보고 설정을 확인할 수 있다.
    log_event(
        "startup_complete",
        agent_pattern=settings.agent_pattern,
        rag_documents=len(rag_sources),
        main_model=settings.model_label(settings.main_model_provider),
        summary_model=settings.model_label(settings.summary_model_provider),
        router_model=settings.model_label(
            settings.router_model_provider or settings.main_model_provider
        ),
        search_model=settings.model_label(
            settings.search_model_provider or settings.main_model_provider
        )
        if settings.tavily_api_key
        else "disabled",
    )
    try:
        yield
    finally:
        await pool.close()
        log_event("shutdown_complete")


app = FastAPI(title="Chatbot API", lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
register_error_handlers(app)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(web_router.router)
app.include_router(auth_router.router)
app.include_router(threads_router.router)
app.include_router(chat_router.router)


@app.get("/health", tags=["ops"])
async def health():
    """서버와 DB 상태 확인.

    DB 에 SELECT 1 을 보내므로, 호출하면 Render 와 Supabase 가 함께 깨어난다.
    """
    await ping(app.state.pool)
    return {"status": "ok"}
