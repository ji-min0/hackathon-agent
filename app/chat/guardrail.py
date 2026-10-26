"""입력 가드레일. 그래프 맨 앞에서 위험 요청·개인정보·프롬프트 공격·목적 외 질문을 거절한다.

규칙(정규식)으로 먼저 거르고, 나머지는 경량 모델이 JSON 으로 판정한다.
"""

import logging
import re
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel

from app.chat.models import build_model
from app.chat.prompts import get_prompt
from app.core.logging import log_event

GuardrailCategory = Literal["safe", "harmful", "personal_info", "prompt_injection", "off_topic"]


class GuardrailDecision(BaseModel):
    """가드레일 출력 스키마. 모델이 쓴 JSON 을 이 형식으로 검증한다."""

    allowed: bool
    category: GuardrailCategory
    reason: str


# 형식이 정해진 개인정보는 모델을 부르지 않고 바로 거절한다. 빠르고 확실하다.
SENSITIVE_PATTERNS = {
    "resident_number": re.compile(r"\b\d{6}\s*-\s*[1-4]\d{6}\b"),
    "card_number": re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b"),
}

REFUSALS: dict[str, str] = {
    "harmful": "죄송하지만 다른 사람이나 자신에게 해가 될 수 있는 요청에는 답할 수 없어요.",
    "personal_info": (
        "주민등록번호, 카드·계좌번호, 비밀번호 같은 민감한 개인정보는 보내지 말아 주세요. "
        "해당 부분을 지우고 다시 질문해 주세요."
    ),
    "prompt_injection": "죄송하지만 시스템 설정을 바꾸거나 내부 정보를 요구하는 요청은 처리할 수 없어요.",
    "off_topic": "죄송하지만 이 서비스에서 다루지 않는 주제예요. 서비스와 관련된 질문을 해 주세요.",
}


def build_guardrail(provider: str):
    """GuardrailDecision 을 돌려주는 판정기. 분류기와 같은 이유로 JSON 프롬프트를 쓴다."""
    return build_model(provider) | PydanticOutputParser(pydantic_object=GuardrailDecision)


def latest_user_text(messages: list[AnyMessage]) -> str:
    return next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), "")


def build_guardrail_node(checker):
    """가드레일 노드. 규칙(정규식) → 모델 판정 순서로 검사한다.

    판정 모델이 실패하면 통과시킨다(fail-open). 대화가 끊기지 않는 것을 우선한다.
    """

    def blocked(category: str, reason: str) -> dict:
        log_event("guardrail_blocked", category=category, reason=reason)
        return {
            "messages": [AIMessage(content=REFUSALS[category])],
            "route": "blocked",
            "blocked": True,
        }

    async def guardrail(state: dict) -> dict:
        text = latest_user_text(state["messages"])
        for name, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                return blocked("personal_info", name)

        try:
            decision = await checker.ainvoke(
                [
                    SystemMessage(
                        get_prompt("GUARDRAIL_PROMPT").format(
                            service_scope=get_prompt("SERVICE_SCOPE")
                        )
                    ),
                    HumanMessage(f"<latest_message>\n{text}\n</latest_message>"),
                ]
            )
        except Exception as exc:
            log_event("guardrail_failed", level=logging.WARNING, error=type(exc).__name__)
            return {"blocked": False}

        if not decision.allowed and decision.category in REFUSALS:
            return blocked(decision.category, decision.reason)
        return {"blocked": False}

    return guardrail


def after_guardrail(state: dict) -> str:
    return "blocked" if state.get("blocked") else "allowed"
