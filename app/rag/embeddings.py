"""임베딩 모델 생성.

임베딩(embedding)은 문장을 그 뜻을 담은 숫자 목록(벡터)으로 바꾼 것이다. 뜻이 비슷한
문장일수록 벡터가 가깝다. 문서와 질문은 반드시 같은 모델로 임베딩해야 비교할 수 있다.

EMBEDDING_PROVIDER 를 바꾸면 scripts/ingest.py 로 문서를 다시 임베딩해야 한다.
"""

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_openai import OpenAIEmbeddings

from app.core.config import settings


def build_embeddings() -> Embeddings:
    if settings.embedding_provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY가 설정되어 있지 않습니다.")
        # 문서는 RETRIEVAL_DOCUMENT, 질문은 RETRIEVAL_QUERY 로 알아서 구분해 임베딩한다.
        return GoogleGenerativeAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.gemini_api_key,
            # 기본 3072 차원은 pgvector HNSW 인덱스 한도(2000)를 넘어서 줄여 받는다.
            output_dimensionality=settings.embedding_dim,
        )

    # OpenAI 호환 서버(vLLM 등). base_url 만 바꿔 끼운다.
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key or settings.openai_api_key,
        # OpenAI 가 아닌 서버는 tiktoken 토큰 분할을 이해하지 못하므로 문자열 그대로 보낸다.
        check_embedding_ctx_length=False,
    )
