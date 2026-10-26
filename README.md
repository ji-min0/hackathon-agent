# AI 챗봇 (LangGraph 멀티 에이전트)

**배포 주소 — https://43.203.94.29** (관리자: `/admin`)

로그인한 사용자가 웹에서 질문하면, 입력 가드레일을 거친 뒤 의도에 맞는 에이전트(일반 답변·웹 검색·문서
검색)가 답한다. 대화는 대화방별로 저장되어 다시 열면 이어진다.

## 주요 기능

| 기능 | 내용 |
|---|---|
| 입력 가드레일 | 위험 요청·민감 개인정보·프롬프트 공격·목적 외 질문을 맨 앞에서 거절한다(정규식 + 경량 모델 판정) |
| 멀티 에이전트 | `AGENT_PATTERN` 으로 연결 방식을 고른다. `router`(의도 분류 → 담당 하나), `single`(에이전트 하나가 모든 툴), `supervisor`(상위 에이전트가 담당에게 나눠 맡기고 종합) |
| 웹 검색 | Tavily. 한 번 답할 때 최대 2회 검색하고 출처를 단다 |
| RAG | PDF·마크다운을 청크로 잘라 임베딩해 Supabase pgvector 에 저장. 문서에서 찾아 "(문서명 p.쪽)"으로 인용하고, 없으면 웹 검색으로 넘어간다 |
| 휴먼인더루프 | `HITL_TOOLS` 의 툴은 실행 직전에 멈추고, 사용자가 채팅으로 "네/아니요"라고 답하면 이어간다 |
| 대화 유지 | LangGraph 체크포인터가 대화방별 상태를 저장한다. 길어지면 오래된 대화를 요약한다 |
| 관리자 페이지 | 사용량 대시보드(질문 수·에러율·응답 시간·경로별 집계), 프롬프트 편집(버전 관리·되돌리기·즉시 반영) |
| API | 웹 화면용 `/api/threads/...`, 단일 진입점 `POST /chat`. 응답에 `route`(답한 경로)와 `status`(done·interrupted) |

## 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 백엔드 | FastAPI (async), uvicorn |
| 프론트엔드 | Jinja2 템플릿 + Vanilla JS |
| AI | LangGraph `StateGraph` + LangChain `create_agent`, 미들웨어(요약·툴 호출 제한·휴먼인더루프) |
| LLM | OpenAI·Anthropic 호환 API(Codyssey 게이트웨이). `OPENAI_BASE_URL` 로 vLLM 등에 교체 |
| 검색·RAG | Tavily, Gemini 임베딩, pymupdf, Supabase pgvector |
| 인증·DB | Supabase Auth(HttpOnly 쿠키), Supabase PostgreSQL |
| 배포 | AWS EC2(CloudFormation) + Docker Compose + Caddy(HTTPS 자동 인증서) |

## 로컬 실행

### 1. 설치

```bash
git clone https://github.com/ji-min0/hackathon-agent.git
cd hackathon-agent
uv sync          # = make install
```

Python 3.12 와 [uv](https://docs.astral.sh/uv/) 가 필요하다. 자주 쓰는 명령은 `make help`.

### 2. 환경 변수

```bash
cp .env.example .env
```

`.env` 를 채운다. 각 키 설명은 `.env.example` 주석에 있다. **`.env` 는 저장소에 올라가지 않는다.**

| 키 | 필수 | 내용 |
|---|:--:|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | O | 기본 LLM. `OPENAI_BASE_URL` 을 비우면 OpenAI 공식 서버 |
| `ANTHROPIC_API_KEY` | | OpenAI 실패 시 폴백. 노드별 공급자(`*_MODEL_PROVIDER`)로도 쓴다 |
| `DATABASE_URL` | O | Supabase → Connect → **Session pooler(5432)**. Transaction pooler(6543)는 체크포인터와 충돌한다 |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | O | Supabase → Settings → API Keys |
| `TAVILY_API_KEY` | | 웹 검색. 비우면 검색 질문도 일반 에이전트가 답한다 |
| `GEMINI_API_KEY` | | RAG 임베딩(`EMBEDDING_PROVIDER=gemini`) |
| `ADMIN_EMAILS` | | 관리자 페이지에 들어갈 이메일(쉼표 구분) |
| `AGENT_PATTERN` | | `router`(기본) · `single` · `supervisor` |
| `COOKIE_SECURE` | | 로컬은 `false`, HTTPS 배포는 `true` |

`DATABASE_URL` 비밀번호에 특수문자가 있으면 URL 인코딩한다(예: `!` → `%21`).

### 3. DB 준비

Supabase → **SQL Editor** 에서 [`scripts/schema.sql`](scripts/schema.sql) 을 실행한다. 여러 번 실행해도 안전하다.
Authentication → Sign In / Providers → Email 에서 **Confirm email 을 끈다**.
LangGraph 체크포인트 테이블은 서버가 처음 뜰 때 자동으로 만든다.

### 4. 실행·확인

```bash
make dev                       # = uvicorn app.main:app --reload
curl -s localhost:8000/health  # {"status":"ok"}
```

시작 로그 `startup_complete` 에 패턴·모델·RAG 문서 수가 찍힌다. 브라우저에서 http://localhost:8000.

### 5. RAG 문서 넣기

```bash
uv run python scripts/ingest.py "docs/문서.pdf"   # pdf · md · txt
```

넣은 뒤 서버를 재시작하거나 관리자 화면의 "문서·프롬프트 다시 읽기"를 누르면 분류기에 반영된다.

### 문제가 생기면

| 증상 | 해결 |
|---|---|
| `ValidationError` 로 기동 실패 | 필수 환경 변수가 빠졌거나 값 형식이 틀렸다(숫자 칸에 문자 등) |
| DB 연결 타임아웃 | `DATABASE_URL` 이 Session pooler 주소인지, 비밀번호가 인코딩됐는지 확인 |
| 로그인 후 계속 `/login` 으로 돌아감 | 로컬은 HTTP 라 `COOKIE_SECURE=false` 여야 한다 |
| 가입은 되는데 로그인이 안 됨 | Supabase 에서 Confirm email 을 끈다 |

## 개발

```bash
make check    # ruff 린트·포맷 검사 + pytest(커버리지). 커밋 전에 돌린다
make fix      # 린트 자동 수정 + 포맷
```

테스트는 실제 DB·LLM 을 부르지 않는다(가짜 모델과 메모리 체크포인터). 실제 모델로 그래프를 확인하려면
`uv run python scripts/check_graph.py`.

## 프로젝트 구조

```
app/
├─ main.py        진입점. lifespan 에서 DB 풀·체크포인터·그래프를 조립한다
├─ core/          설정, DB 풀, 로깅, 미들웨어, 예외
├─ auth/          회원가입·로그인·토큰 검증
├─ threads/       대화방 CRUD
├─ chat/          메시지 전송, 그래프(agent.py), 프롬프트(prompts.py)
├─ rag/           청크 분할, 임베딩, pgvector 저장·검색
├─ admin/         관리자 API(대시보드·프롬프트 버전)
└─ web/           Jinja2 화면 라우터

templates/, static/   웹 화면(채팅·로그인·관리자)
scripts/              schema.sql, ingest.py, deploy.sh, server-setup.sh, check_graph.py
deploy/               Caddyfile, aws/ec2-cfn.yml(CloudFormation)
docs/deploy.md        배포 절차
```

기능 폴더는 `router.py`(HTTP) → `service.py`(로직) → `repository.py`(SQL) 로 나눈다.

## 민감정보 관리

- API 키·DB 비밀번호는 코드와 문서에 쓰지 않고 `.env` 로만 관리한다. 저장소에는 `.env.example` 만 둔다.
- Supabase 키는 서버에서만 쓴다. `public` 스키마 테이블(`chat_threads`, `chat_logs`, `rag_chunks`,
  `prompts`, 체크포인트)은 정책 없이 RLS 만 켜서 anon 키로의 외부 접근을 막는다. 서버는 소유자 역할로
  접속해 영향받지 않는다.

## 배포

AWS 에는 CloudFormation 스택 하나(`deploy/aws/ec2-cfn.yml`)로 EC2·탄력적 IP·방화벽을 만들고, 서버에서는
Docker Compose 로 앱과 Caddy 를 띄운다.

```bash
# main 에 push 한 뒤 로컬에서 한 번에 배포 (git pull → 빌드·실행)
ssh -i ~/.ssh/hackathon-agent-key.pem ubuntu@<IP> "cd hackathon-agent && make deploy"
```

스택 만들기, `.env` 올리기, NCP 로 옮기기는 [`docs/deploy.md`](docs/deploy.md) 참고.
Supabase 무료 플랜은 7일 무활동이면 일시정지되니 시연 전에 `/health` 를 한 번 호출해 깨운다.
