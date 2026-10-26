"""OpenAI·Gemini와 자동 폴백의 실제 호출을 점검한다.

실행 전 .env 에 OPENAI_API_KEY, GEMINI_API_KEY 를 설정한다.

    python scripts/check_model_fallback.py

출력에는 API 키와 모델 응답 전문을 담지 않는다. 실패한 OpenAI를 흉내 내기 위해
테스트 전용 잘못된 키를 쓰며, 이때 Gemini 폴백이 응답하면 성공으로 본다.
"""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

# `python scripts/check_model_fallback.py`로 실행해도 프로젝트 패키지를 찾는다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from app.chat.agent import build_gemini_model, build_openai_model
from app.core.config import settings

Prompt = "Reply with exactly: OK"


async def measure(name: str, invoke: Callable[[], Awaitable[object]]) -> None:
    """호출 성공 여부와 총 지연 시간만 출력한다."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.ai_timeout_seconds):
            result = await invoke()
        if not getattr(result, "content", None):
            raise RuntimeError("빈 응답")
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        print(f"{name}: FAILED latency_ms={latency_ms} error={type(exc).__name__}")
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    print(f"{name}: OK latency_ms={latency_ms}")


async def main() -> None:
    openai_model = build_openai_model()
    gemini_model = build_gemini_model()

    await measure("openai_direct", lambda: openai_model.ainvoke(Prompt))
    await measure("gemini_direct", lambda: gemini_model.ainvoke(Prompt))

    # 요약 미들웨어가 쓰는 model.ainvoke() 경로와 같은 폴백 체인이다.
    failed_openai = ChatOpenAI(
        model=settings.openai_model_name,
        temperature=0,
        api_key="invalid-key-for-fallback-verification",
        timeout=settings.openai_timeout_seconds,
        max_retries=0,
    )
    summary_model = failed_openai.with_fallbacks([gemini_model])
    await measure("summary_openai_to_gemini", lambda: summary_model.ainvoke(Prompt))

    # main Agent도 summary와 같은 model.with_fallbacks() 체인을 사용한다.
    main_agent = create_agent(
        model=failed_openai.with_fallbacks([gemini_model]),
        tools=[],
    )

    async def invoke_main_fallback():
        result = await main_agent.ainvoke({"messages": [{"role": "user", "content": Prompt}]})
        return result["messages"][-1]

    await measure("main_openai_to_gemini", invoke_main_fallback)


if __name__ == "__main__":
    asyncio.run(main())
