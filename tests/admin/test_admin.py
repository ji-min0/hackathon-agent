"""관리자 페이지 테스트. DB 는 가짜 함수로 바꾼다."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import repository
from app.admin import router as admin_router
from app.admin.deps import get_admin_user
from app.auth.deps import CurrentUser
from app.chat import prompts
from app.core import config
from app.core.deps import get_pool
from app.core.errors import Forbidden, register_error_handlers


@pytest.fixture(autouse=True)
def reset_overrides():
    yield
    prompts.set_overrides({})


def test_get_prompt_prefers_active_override() -> None:
    assert prompts.get_prompt("SERVICE_SCOPE") == prompts.SERVICE_SCOPE

    prompts.set_overrides({"SERVICE_SCOPE": "해커톤 안내 챗봇", "UNKNOWN": "무시"})

    assert prompts.get_prompt("SERVICE_SCOPE") == "해커톤 안내 챗봇"
    assert "UNKNOWN" not in prompts._overrides


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("RAG_PROMPT", "{today} only", "{rag_documents}"),
        ("ROUTER_PROMPT", '{rag_documents} {"route": "x"}', "중괄호"),
        ("SYSTEM_PROMPT", "   ", "비어"),
        ("NOT_A_PROMPT", "x", "고칠 수 없는"),
    ],
)
def test_validate_prompt_rejects_broken_templates(name, content, error) -> None:
    with pytest.raises(ValueError, match=error):
        prompts.validate_prompt(name, content)


def test_validate_prompt_accepts_escaped_braces() -> None:
    prompts.validate_prompt("ROUTER_PROMPT", '{rag_documents} {{"route": "x"}}')


def test_every_default_prompt_passes_validation() -> None:
    for name in prompts.EDITABLE_PROMPTS:
        prompts.validate_prompt(name, prompts.default_prompt(name))


@pytest.mark.asyncio
async def test_admin_requires_listed_email(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "admin_emails", "Admin@Example.com, b@example.com")
    admin = CurrentUser(id=None, email="admin@example.com")

    assert await get_admin_user(admin) is admin
    with pytest.raises(Forbidden):
        await get_admin_user(CurrentUser(id=None, email="user@example.com"))


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(admin_router.router)
    app.dependency_overrides[get_admin_user] = lambda: CurrentUser(id=None, email="admin@x.com")
    app.dependency_overrides[get_pool] = lambda: None
    reloads: list[bool] = []

    async def reload_agent():
        reloads.append(True)
        return ["guide.pdf"]

    app.state.reload_agent = reload_agent
    app.state.reloads = reloads
    return TestClient(app)


def test_saving_prompt_creates_version_and_reloads_agent(client, monkeypatch) -> None:
    saved: list[tuple] = []

    async def create_version(pool, name, content, note, created_by):
        saved.append((name, content, note, created_by))
        return 3

    monkeypatch.setattr(repository, "create_version", create_version)

    res = client.post(
        "/api/admin/prompts/SERVICE_SCOPE", json={"content": "새 범위", "note": "테스트"}
    )

    assert res.status_code == 200
    assert res.json() == {"name": "SERVICE_SCOPE", "version": 3}
    assert saved == [("SERVICE_SCOPE", "새 범위", "테스트", "admin@x.com")]
    assert client.app.state.reloads == [True]


def test_saving_broken_prompt_is_rejected_without_db_write(client, monkeypatch) -> None:
    async def must_not_save(*args):
        raise AssertionError("검증에 실패하면 저장하지 않는다")

    monkeypatch.setattr(repository, "create_version", must_not_save)

    res = client.post("/api/admin/prompts/RAG_PROMPT", json={"content": "{today} 만 있음"})

    assert res.status_code == 422
    assert "{rag_documents}" in res.json()["message"]
    assert client.app.state.reloads == []


def test_activating_missing_version_returns_404(client, monkeypatch) -> None:
    async def activate_version(pool, name, version):
        raise LookupError(name)

    monkeypatch.setattr(repository, "activate_version", activate_version)

    res = client.post("/api/admin/prompts/SYSTEM_PROMPT/activate", json={"version": 9})

    assert res.status_code == 404
    assert client.app.state.reloads == []


def test_reload_returns_rag_documents(client) -> None:
    res = client.post("/api/admin/reload")

    assert res.json() == {"rag_documents": ["guide.pdf"]}
