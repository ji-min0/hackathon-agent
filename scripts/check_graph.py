"""멀티 에이전트 그래프를 실제 모델로 한 번 돌려 본다.

실행 전 .env 에 OPENAI_API_KEY(게이트웨이 키)를 설정한다.

    uv run python scripts/check_graph.py

체크포인터는 메모리(InMemorySaver)를 써서 Supabase DB 에는 아무것도 쓰지 않는다.
같은 thread_id 로 두 번 물어 멀티턴(이전 대화 기억)이 되는지 확인한다.
"""

import asyncio
import sys
import time
from pathlib import Path

# `python scripts/check_graph.py`로 실행해도 프로젝트 패키지를 찾는다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langgraph.checkpoint.memory import InMemorySaver

from app.chat.agent import build_agent
from app.core.config import settings

TURNS = [
    "내가 좋아하는 숫자는 7이야. '알겠어'라고만 답해.",
    "내가 좋아하는 숫자가 뭐였지? 숫자만 답해.",
]


async def main() -> None:
    graph = build_agent(InMemorySaver())
    config = {"configurable": {"thread_id": "check-graph"}}
    print(f"main_model={settings.model_label(settings.main_model_provider)}")

    for question in TURNS:
        started = time.perf_counter()
        result = await graph.ainvoke(
            {"messages": [{"role": "user", "content": question}]}, config=config
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        answer = result["messages"][-1]
        # 폴백이 일어났는지 보려고 실제로 답한 모델 이름을 함께 찍는다.
        model = answer.response_metadata.get("model_name") or answer.response_metadata.get(
            "model", "unknown"
        )
        print(f"\nQ: {question}")
        print(f"A: {answer.content}")
        print(f"   route={result['route']} model={model} latency_ms={latency_ms}")

    passed = "7" in str(result["messages"][-1].content)
    print(f"\n멀티턴 기억: {'OK' if passed else 'FAIL'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
