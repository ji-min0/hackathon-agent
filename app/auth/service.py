"""Supabase Auth 호출 계층.

자격 증명 관리만 Supabase 에 위임하고, 세션 유지와 접근 제어는 우리가 담당한다.
라우터는 이 모듈만 호출하고 supabase 클라이언트를 직접 다루지 않는다.

Supabase 오류 원문은 사용자에게 그대로 내보내지 않는다.
AppError 로 감싸고 원문은 detail 에 실어 로그에만 남긴다.
"""

from dataclasses import dataclass
from uuid import UUID

from supabase import AsyncClient
from supabase_auth.errors import AuthApiError, AuthWeakPasswordError

from app.core.errors import (
    AppError,
    Conflict,
    InvalidInput,
    TooManyRequests,
    Unauthorized,
)


@dataclass
class AuthUser:
    id: UUID
    email: str


@dataclass
class AuthSession:
    user: AuthUser
    access_token: str
    expires_in: int


def _to_user(user) -> AuthUser:
    return AuthUser(id=UUID(user.id), email=user.email or "")


async def sign_up(client: AsyncClient, email: str, password: str) -> AuthUser:
    """계정을 만든다.

    Supabase 대시보드에서 이메일 확인(Confirm email)이 꺼져 있어야
    가입 직후 바로 로그인할 수 있다.
    """
    try:
        response = await client.auth.sign_up({"email": email, "password": password})
    except AuthWeakPasswordError as exc:
        raise InvalidInput(
            "비밀번호가 너무 단순합니다. 더 복잡하게 설정해 주세요.",
            detail=str(exc),
        ) from exc
    except AuthApiError as exc:
        message = exc.message.lower()

        # 중복 가입. 코드 값이 버전에 따라 달라 메시지도 함께 본다.
        if exc.code == "user_already_exists" or "already" in message:
            raise Conflict("이미 가입된 이메일입니다.", detail=exc.message) from exc

        # 이메일 확인이 켜져 있으면 가입마다 메일을 보내다 발송 한도에 걸린다.
        # 대시보드에서 Confirm email 을 끄면 메일을 보내지 않는다.
        if exc.status == 429:
            raise TooManyRequests(
                "가입 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
                detail=exc.message,
            ) from exc

        # 형식 오류는 사용자가 고칠 수 있는 문제이므로 422 로 돌려준다.
        # Supabase 는 일부 도메인(example.com 등)을 유효하지 않은 것으로 거부한다.
        if exc.status in (400, 422):
            raise InvalidInput("이메일 또는 비밀번호를 확인해 주세요.", detail=exc.message) from exc

        raise AppError("회원가입에 실패했습니다.", detail=exc.message) from exc

    if response.user is None:
        raise AppError("회원가입에 실패했습니다.", detail="user is None")

    return _to_user(response.user)


async def sign_in(client: AsyncClient, email: str, password: str) -> AuthSession:
    """로그인해 세션을 받는다.

    이메일이 없는 경우와 비밀번호가 틀린 경우를 구분하지 않는다.
    구분하면 어떤 이메일이 가입되어 있는지 알려 주는 셈이 된다.
    """
    try:
        response = await client.auth.sign_in_with_password({"email": email, "password": password})
    except AuthApiError as exc:
        # 이메일 확인이 켜져 있으면 인증 전까지 로그인이 막힌다.
        # 자격 증명 문제와 구분해 안내해야 원인을 찾을 수 있다.
        if "not confirmed" in exc.message.lower():
            raise Unauthorized("이메일 인증이 완료되지 않았습니다.", detail=exc.message) from exc
        raise Unauthorized("이메일 또는 비밀번호가 올바르지 않습니다.", detail=exc.message) from exc

    if response.session is None or response.user is None:
        # 이메일 확인이 켜져 있으면 세션 없이 돌아온다.
        raise Unauthorized(
            "로그인하지 못했습니다. 이메일 인증이 필요한지 확인해 주세요.",
            detail="session is None",
        )

    return AuthSession(
        user=_to_user(response.user),
        access_token=response.session.access_token,
        expires_in=response.session.expires_in,
    )


async def verify_token(client: AsyncClient, token: str) -> AuthUser | None:
    """액세스 토큰을 검증한다. 유효하지 않으면 None 을 돌려준다.

    요청마다 Supabase 에 왕복이 생긴다. 단순하고 확실한 대신 지연이 붙는다.
    문제가 되면 로컬 JWT 검증으로 바꾼다. docs/API_SPEC.md 8.2 참고.
    """
    try:
        response = await client.auth.get_user(token)
    except AuthApiError:
        # 만료·위조 토큰은 정상 흐름이므로 예외로 올리지 않는다.
        return None

    if response is None or response.user is None:
        return None

    return _to_user(response.user)
