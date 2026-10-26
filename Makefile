# 자주 쓰는 명령 모음. `make help` 로 목록을 본다.
# 모든 명령은 uv 가상환경(.venv)에서 실행된다.

.DEFAULT_GOAL := help
.PHONY: help install dev test test-db cov lint format check fix clean

help: ## 명령 목록
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## 의존성 설치 (개발용 포함). pyproject 를 바꾼 뒤에도 실행
	uv sync

dev: ## 로컬 서버 실행 (코드 변경 시 자동 재시작)
	uv run uvicorn app.main:app --reload

test: ## 테스트
	uv run pytest

test-db: ## 실제 PostgreSQL(pgvector)로 관리자·RAG SQL 테스트 (Docker 필요)
	@docker rm -f chatbot-test-db > /dev/null 2>&1; docker run -d --rm --name chatbot-test-db -e POSTGRES_PASSWORD=test -p 55432:5432 pgvector/pgvector:pg17 > /dev/null
	@until docker exec chatbot-test-db psql -U postgres -c "select 1" > /dev/null 2>&1; do sleep 1; done
	@TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres uv run pytest tests/integration; \
		status=$$?; docker stop chatbot-test-db > /dev/null; exit $$status

cov: ## 테스트 + 커버리지 리포트 (htmlcov/index.html 도 생성)
	uv run pytest --cov --cov-report=term --cov-report=html

lint: ## 린트 검사 + 자동 수정
	uv run ruff check . --fix

format: ## 코드 포맷 정리
	uv run ruff format .

fix: ## 린트 자동 수정 + 포맷 정리를 한 번에
	uv run ruff check . --fix
	uv run ruff format .

check: ## 커밋 전 전체 점검 (린트 → 포맷 → 테스트)
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest --cov --cov-report=term

clean: ## 캐시 정리
	find . -name "__pycache__" -type d -prune -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
