"""관리자 API. 화면(/admin)은 app/web/router.py 가 그린다."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.admin import repository
from app.admin.deps import get_admin_user
from app.auth.deps import CurrentUser
from app.chat import prompts
from app.core.deps import get_pool
from app.core.errors import InvalidInput, NotFound

router = APIRouter(prefix="/api/admin", tags=["admin"])


class PromptCreate(BaseModel):
    content: str
    note: str | None = None


class PromptActivate(BaseModel):
    # 0 이면 코드 기본값으로 되돌린다.
    version: int


@router.get("/dashboard")
async def dashboard(
    days: int = Query(7, ge=1, le=90),
    admin: CurrentUser = Depends(get_admin_user),
    pool=Depends(get_pool),
) -> dict:
    return await repository.dashboard(pool, days)


@router.get("/prompts")
async def list_prompts(
    admin: CurrentUser = Depends(get_admin_user), pool=Depends(get_pool)
) -> list:
    """고칠 수 있는 프롬프트마다 기본값, 지금 쓰는 내용, 버전 목록을 돌려준다."""
    result = []
    for name, placeholders in prompts.EDITABLE_PROMPTS.items():
        result.append(
            {
                "name": name,
                "placeholders": list(placeholders),
                "default": prompts.default_prompt(name),
                "current": prompts.get_prompt(name),
                "versions": await repository.prompt_versions(pool, name),
            }
        )
    return result


@router.post("/prompts/{name}")
async def create_prompt_version(
    name: str,
    body: PromptCreate,
    request: Request,
    admin: CurrentUser = Depends(get_admin_user),
    pool=Depends(get_pool),
) -> dict:
    """새 버전을 저장하고 바로 적용한다. 그래프를 다시 만들어 다음 질문부터 반영된다."""
    try:
        prompts.validate_prompt(name, body.content)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc
    version = await repository.create_version(pool, name, body.content, body.note, admin.email)
    await request.app.state.reload_agent()
    return {"name": name, "version": version}


@router.post("/prompts/{name}/activate")
async def activate_prompt_version(
    name: str,
    body: PromptActivate,
    request: Request,
    admin: CurrentUser = Depends(get_admin_user),
    pool=Depends(get_pool),
) -> dict:
    """이전 버전으로 되돌린다(version=0 이면 코드 기본값)."""
    if name not in prompts.EDITABLE_PROMPTS:
        raise NotFound("프롬프트를 찾을 수 없습니다.")
    try:
        await repository.activate_version(pool, name, body.version)
    except LookupError as exc:
        raise NotFound("해당 버전을 찾을 수 없습니다.") from exc
    await request.app.state.reload_agent()
    return {"name": name, "version": body.version}


@router.post("/reload")
async def reload(request: Request, admin: CurrentUser = Depends(get_admin_user)) -> dict:
    """RAG 문서 목록과 프롬프트를 다시 읽어 그래프를 다시 만든다(ingest 후 재시작 대신)."""
    sources = await request.app.state.reload_agent()
    return {"rag_documents": sources}
