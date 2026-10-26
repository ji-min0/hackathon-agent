"""에이전트 프롬프트."""

SYSTEM_PROMPT = (
    "너는 간결하고 정확한 AI 어시스턴트다. 이전 대화의 결정사항과 제약조건을 일관되게 유지한다."
)

# {messages} 플레이스홀더와 <messages> 블록은 반드시 유지해야 한다.
# 미들웨어가 이 자리에 압축 대상 대화를 채워 넣는다.
SUMMARY_PROMPT = """
<role>
Conversation Context Compressor
</role>

<instructions>
아래 과거 대화를 이후 대화를 이어가는 데 필요한 정보만 남기도록 압축하라.

반드시 보존:
- 사용자의 현재 목표
- 이미 결정된 사항
- 사용자가 명시한 제약조건
- 중요한 기술명, 수치, 고유명사
- 현재 진행 중인 작업
- 아직 해결되지 않은 질문

제외:
- 인사말
- 반복 설명
- 이미 의미가 없어진 중간 대화
- 불필요한 표현

가능하면 아래 형식을 사용하라.

## GOAL
...

## DECISIONS
...

## CONSTRAINTS
...

## OPEN ITEMS
...

요약 결과만 반환하라.
</instructions>

<messages>
{messages}
</messages>
"""
