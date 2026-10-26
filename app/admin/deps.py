"""관리자 권한 의존성. ADMIN_EMAILS 에 있는 이메일만 통과시킨다."""

from fastapi import Depends

from app.auth.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.core.errors import Forbidden


async def get_admin_user(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.email.lower() not in settings.admin_email_list:
        raise Forbidden("관리자만 접근할 수 있습니다.")
    return user
