# 자주 쓰는 명령 모음. `make help` 로 목록을 본다.
# 모든 명령은 uv 가상환경(.venv)에서 실행된다.

.DEFAULT_GOAL := help
.PHONY: help install dev test cov lint format check fix docker-build docker-run deploy deploy-logs clean

IMAGE ?= chatbot-api

help: ## 명령 목록
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## 의존성 설치 (개발용 포함). pyproject 를 바꾼 뒤에도 실행
	uv sync

dev: ## 로컬 서버 실행 (코드 변경 시 자동 재시작)
	uv run uvicorn app.main:app --reload

test: ## 테스트
	uv run pytest

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

docker-build: ## Docker 이미지 빌드
	docker build -t $(IMAGE) .

docker-run: ## Docker 컨테이너 실행 (.env 사용, localhost:8000)
	docker run --rm --env-file .env -p 8000:8000 $(IMAGE)

deploy: ## (서버에서) 빌드 후 실행. 서버 .env 에 DOMAIN 이 있어야 한다
	docker compose up -d --build

deploy-logs: ## (서버에서) 앱 로그 보기
	docker compose logs -f app

clean: ## 캐시 정리
	find . -name "__pycache__" -type d -prune -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
