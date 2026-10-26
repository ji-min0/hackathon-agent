"""환경 변수 로딩. 값은 .env 에서 읽고, 키 목록은 .env.example 을 기준으로 한다."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "anthropic", "gemini"]


class Settings(BaseSettings):
    # model_ 로 시작하는 필드가 pydantic 예약어와 충돌하지 않도록 보호를 해제한다.
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        protected_namespaces=(),
    )

    # LLM
    openai_api_key: str
    gemini_api_key: str = ""
    openai_model_name: str = "gpt-5-mini"
    # OpenAI 호환 게이트웨이(Codyssey)나 vLLM 주소. 비우면 OpenAI 공식 서버로 간다.
    openai_base_url: str | None = None

    anthropic_api_key: str = ""
    anthropic_model_name: str = "claude-haiku-4"
    # Anthropic SDK 가 /v1/messages 를 직접 붙이므로 /v1 없이 적는다.
    anthropic_base_url: str | None = None
    anthropic_timeout_seconds: int = 40
    gemini_model_name: str = "gemini-2.5-flash"

    # RAG 임베딩. 바꾸면 scripts/ingest.py 로 문서를 다시 넣어야 한다.
    embedding_provider: Literal["gemini", "openai"] = "gemini"
    embedding_model: str = "gemini-embedding-001"
    # rag_chunks.embedding 컬럼 차원과 같아야 한다(scripts/schema.sql 의 vector(768)).
    embedding_dim: int = 768
    # embedding_provider=openai 일 때 쓰는 OpenAI 호환 서버(vLLM 등) 주소와 키.
    embedding_base_url: str | None = None
    embedding_api_key: str = ""
    # 질문 하나에 가져올 청크 수.
    rag_top_k: int = 5

    # 웹 검색(Tavily). 비우면 search 로 분류된 질문도 general 이 답한다.
    tavily_api_key: str = ""
    tavily_max_results: int = 5
    # 멀티 에이전트 연결 방식. router: 분류기가 담당 하나를 고른다 / single: 에이전트
    # 하나가 모든 툴을 쓴다 / supervisor: 상위 에이전트가 담당들을 툴처럼 불러 결과를 합친다.
    agent_pattern: Literal["router", "single", "supervisor"] = "router"

    main_model_provider: Provider = "openai"
    summary_model_provider: Provider = "openai"
    # 노드별 공급자. 비우면 main 과 같은 공급자를 쓴다.
    router_model_provider: Provider | None = None
    search_model_provider: Provider | None = None
    rag_model_provider: Provider | None = None
    openai_timeout_seconds: int = 18
    gemini_timeout_seconds: int = 20
    ai_timeout_seconds: int = 60
    summary_trigger_tokens: int = 8000
    summary_keep_tokens: int = 4000

    def model_label(self, provider: str) -> str:
        """로그에 남길 `공급자:모델명` 문자열."""
        names = {
            "openai": self.openai_model_name,
            "anthropic": self.anthropic_model_name,
            "gemini": self.gemini_model_name,
        }
        name = names.get(provider, "unknown")
        return f"{provider}:{name}"

    # Supabase
    database_url: str
    supabase_url: str
    supabase_anon_key: str

    # 현재 코드 경로에서는 사용하지 않는다.
    # 데이터 접근은 DATABASE_URL 로 직접 하고, 인증은 anon 키로 충분하기 때문이다.
    # 관리자 전용 작업이 필요해지면 그때 채운다.
    supabase_service_role_key: str = ""

    # 애플리케이션
    log_level: str = "INFO"
    max_message_length: int = 2000
    cookie_secure: bool = False


settings = Settings()  # type: ignore[call-arg]
