"""LLM 모델 생성. 공급자(OpenAI 호환·Anthropic·Gemini)별 모델을 만든다.

openai 공급자는 LiteLLM 프록시(deploy/litellm.yaml)를 부른다. 어떤 모델로 갈지와
실패 시 폴백 순서는 프록시가 정하므로 여기에는 폴백 코드가 없다.
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from app.core.config import settings


def build_openai_model() -> ChatOpenAI:
    """기본 OpenAI 호환 모델 인스턴스를 만든다.

    base_url 을 바꾸면 Codyssey 게이트웨이, vLLM 등 OpenAI 호환 서버로 전환된다.
    main과 summary는 같은 공급자를 쓰더라도 인스턴스를 공유하지 않는다.
    """
    return ChatOpenAI(
        model=settings.openai_model_name,
        base_url=settings.openai_base_url,
        # GPT-5 계열은 temperature 기본값(1)만 허용해서 지정하지 않는다.
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        # 재시도·폴백은 LiteLLM 프록시가 한다.
        max_retries=0,
    )


def build_anthropic_model() -> ChatAnthropic:
    """Anthropic 호환 모델 인스턴스를 만든다."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY가 설정되어 있지 않습니다.")

    return ChatAnthropic(
        model=settings.anthropic_model_name,
        # Anthropic SDK 가 /v1/messages 를 직접 붙이므로 /v1 없이 넘긴다.
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        # Anthropic 규격은 max_tokens 가 필수다.
        max_tokens=2048,
        timeout=settings.anthropic_timeout_seconds,
        max_retries=0,
    )


def build_gemini_model() -> ChatGoogleGenerativeAI:
    """명시적으로 선택한 경우에만 Gemini 모델 인스턴스를 만든다."""
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY가 설정되어 있지 않습니다.")

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_name,
        temperature=0,
        api_key=settings.gemini_api_key,
        request_timeout=settings.gemini_timeout_seconds,
        retries=0,
    )


def build_model(provider: str):
    """설정에서 명시한 공급자의 역할별 모델을 만든다."""
    if provider == "openai":
        return build_openai_model()
    if provider == "anthropic":
        return build_anthropic_model()
    if provider == "gemini":
        return build_gemini_model()
    raise ValueError(f"지원하지 않는 모델 공급자입니다: {provider}")
