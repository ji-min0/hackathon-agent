-- ============================================================================
-- Supabase 스키마 정의
--
-- 실행 방법
--   Supabase 대시보드 → SQL Editor → 이 파일 전체를 붙여넣고 Run
--
-- 이 스크립트는 여러 번 실행해도 안전하다.
--
-- 사용자 계정 테이블은 만들지 않는다.
-- Supabase Auth 가 관리하는 auth.users 를 그대로 참조한다.
-- 자세한 내용은 docs/API_SPEC.md 3.1 참고.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. chat_threads : 사용자별 채팅방
--    id 가 그대로 LangGraph 의 thread_id 로 사용된다.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.chat_threads (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    title      TEXT        NOT NULL DEFAULT '새 대화',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "내 채팅 목록을 updated_at DESC 로 조회" 질의를 그대로 커버한다.
CREATE INDEX IF NOT EXISTS idx_chat_threads_user_updated
    ON public.chat_threads (user_id, updated_at DESC);


-- ----------------------------------------------------------------------------
-- 2. chat_logs : 질문/응답 원본
--    LangGraph 컨텍스트 관리용이 아니다. 화면 복구 · 조회 · 감사용이다.
--    미들웨어가 오래된 대화를 요약해도 이 원본은 유지된다.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.chat_logs (
    id            BIGSERIAL   PRIMARY KEY,
    thread_id     UUID        NOT NULL REFERENCES public.chat_threads(id) ON DELETE CASCADE,
    user_id       UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    question      TEXT        NOT NULL,
    answer        TEXT,
    status        TEXT        NOT NULL DEFAULT 'success'
                              CHECK (status IN ('success', 'error')),
    error_message TEXT,
    latency_ms    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_logs_thread_created
    ON public.chat_logs (thread_id, created_at);

-- user_id 는 사용자 기준 조회를 조인 없이 하기 위한 비정규화 컬럼이다.
CREATE INDEX IF NOT EXISTS idx_chat_logs_user_created
    ON public.chat_logs (user_id, created_at DESC);


-- ----------------------------------------------------------------------------
-- 3. updated_at 자동 갱신 트리거
--    애플리케이션이 갱신을 빠뜨려도 목록 정렬이 어긋나지 않게 한다.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_threads_updated_at ON public.chat_threads;

CREATE TRIGGER trg_chat_threads_updated_at
    BEFORE UPDATE ON public.chat_threads
    FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


-- ----------------------------------------------------------------------------
-- 4. Row Level Security
--
--    Supabase 는 public 스키마의 테이블을 PostgREST 로 자동 노출한다.
--    anon 키는 클라이언트에 배포되는 공개 키이므로, RLS 를 끈 상태로 두면
--    누구나 REST API 로 chat_logs 를 읽을 수 있다.
--
--    정책을 하나도 만들지 않은 채 RLS 만 켜면 PostgREST 경유 접근은 전부 차단된다.
--    반면 서버가 사용하는 DATABASE_URL 의 소유자 역할은 RLS 를 우회하므로
--    애플리케이션 동작에는 영향이 없다.
--
--    즉 접근 제어는 FastAPI 가 소유권 확인으로 담당하고,
--    RLS 는 외부 직접 접근을 막는 차단벽 역할만 한다.
-- ----------------------------------------------------------------------------
ALTER TABLE public.chat_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_logs    ENABLE ROW LEVEL SECURITY;


-- ----------------------------------------------------------------------------
-- 5. rag_chunks : RAG 문서 청크와 임베딩
--    scripts/ingest.py 가 채운다. 서버는 질문 임베딩과 가까운 청크를 찾아 답의 근거로 쓴다.
--    vector(768) 은 EMBEDDING_DIM 과 같아야 한다. 임베딩 모델을 바꿔 차원이 달라지면
--    테이블을 지우고(DROP TABLE public.rag_chunks) 이 블록을 다시 실행한 뒤 ingest 한다.
-- ----------------------------------------------------------------------------
-- Supabase 는 확장을 extensions 스키마에 둔다. 서버 연결의 search_path 에 포함되어 있다.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

CREATE TABLE IF NOT EXISTS public.rag_chunks (
    id          BIGSERIAL    PRIMARY KEY,
    source      TEXT         NOT NULL,
    page        INTEGER,
    chunk_index INTEGER      NOT NULL,
    content     TEXT         NOT NULL,
    embedding   vector(768)  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 코사인 거리(<=>) 검색용 HNSW 인덱스. 2000 차원까지만 만들 수 있다.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedding
    ON public.rag_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_source
    ON public.rag_chunks (source);

-- 다른 테이블과 같이 정책 없이 RLS 만 켜서 anon 키(PostgREST) 접근을 막는다.
ALTER TABLE public.rag_chunks ENABLE ROW LEVEL SECURITY;


-- ----------------------------------------------------------------------------
-- 6. 관리자 페이지 : chat_logs.route, prompts
--    route 는 대시보드의 경로별 집계에 쓴다(general | search | rag | blocked ...).
--    prompts 는 관리자 화면에서 고친 프롬프트 버전이다. 이름마다 활성 버전은 최대 하나이고,
--    활성 버전이 없으면 코드 기본값(app/chat/prompts.py)을 쓴다.
-- ----------------------------------------------------------------------------
ALTER TABLE public.chat_logs ADD COLUMN IF NOT EXISTS route TEXT;

CREATE INDEX IF NOT EXISTS idx_chat_logs_created
    ON public.chat_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS public.prompts (
    id         BIGSERIAL   PRIMARY KEY,
    name       TEXT        NOT NULL,
    version    INTEGER     NOT NULL,
    content    TEXT        NOT NULL,
    is_active  BOOLEAN     NOT NULL DEFAULT false,
    note       TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_one_active
    ON public.prompts (name) WHERE is_active;

ALTER TABLE public.prompts ENABLE ROW LEVEL SECURITY;


-- ----------------------------------------------------------------------------
-- 참고: LangGraph 체크포인트 테이블
--
--    checkpoints, checkpoint_writes, checkpoint_blobs, checkpoint_migrations 는
--    여기서 만들지 않는다.
--    애플리케이션 시작 시 await checkpointer.setup() 이 자동으로 생성한다.
--    스키마가 LangGraph 버전에 종속되므로 직접 수정하지 않는다.
-- ----------------------------------------------------------------------------


-- ----------------------------------------------------------------------------
-- 실행 결과 확인
-- ----------------------------------------------------------------------------
SELECT table_name,
       (SELECT count(*)
          FROM information_schema.columns c
         WHERE c.table_schema = t.table_schema
           AND c.table_name   = t.table_name) AS column_count
  FROM information_schema.tables t
 WHERE t.table_schema = 'public'
   AND t.table_name IN ('chat_threads', 'chat_logs', 'rag_chunks', 'prompts')
 ORDER BY table_name;
