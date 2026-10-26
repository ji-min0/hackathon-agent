"""Jinja2 화면 라우터.

첫 진입만 서버에서 렌더링하고, 이후 메시지 송수신은 fetch 로 처리한다.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.deps import ACCESS_TOKEN_COOKIE

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="templates")


def _is_logged_in(request: Request) -> bool:
    """쿠키 존재만 본다. 실제 검증은 API 호출 시 get_current_user 가 한다."""
    return bool(request.cookies.get(ACCESS_TOKEN_COOKIE))


@router.get("/", response_class=HTMLResponse)
async def chat_page(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "chat.html")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "signup.html")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # 권한은 화면이 부르는 /api/admin/* 가 확인한다(관리자가 아니면 403).
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html")
