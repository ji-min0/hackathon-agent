"""인증 라우터.

동작 방식
    Jinja2 폼 → 여기 → Supabase Auth → JWT 수신 → HttpOnly 쿠키로 발급

토큰은 응답 본문에 담지 않는다. 쿠키에만 넣어 JavaScript 가 읽지 못하게 한다.
"""

from fastapi import APIRouter, Depends, Response

from app.auth import service
from app.auth.deps import ACCESS_TOKEN_COOKIE, CurrentUser, get_current_user
from app.auth.schemas import LoginRequest, SignupRequest, UserResponse
from app.core.config import settings
from app.core.deps import get_supabase
from app.core.logging import log_event

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/signup", status_code=201)
async def signup(
    body: SignupRequest,
    client=Depends(get_supabase),
) -> UserResponse:
    user = await service.sign_up(client, body.email, body.password)
    log_event("signup_success", user_id=user.id)
    return UserResponse(id=user.id, email=user.email)


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    client=Depends(get_supabase),
) -> UserResponse:
    session = await service.sign_in(client, body.email, body.password)

    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        session.access_token,
        httponly=True,  # JavaScript 접근 차단
        secure=settings.cookie_secure,  # 배포(HTTPS)에서는 true
        samesite="lax",
        max_age=session.expires_in,
        path="/",
    )

    log_event("login_success", user_id=session.user.id)
    return UserResponse(id=session.user.id, email=session.user.email)


@router.post("/auth/logout", status_code=204)
async def logout(response: Response) -> None:
    # 쿠키를 지운다. 토큰 자체는 만료 시각까지 유효하지만 브라우저가 더는 보내지 않는다.
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
    log_event("logout")


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=user.id, email=user.email)
