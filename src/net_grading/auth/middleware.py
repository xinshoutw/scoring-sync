from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.session import (
    SESSION_COOKIE,
    CurrentUser,
    load_session,
)
from net_grading.db.engine import get_session


async def optional_user(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: AsyncSession = Depends(get_session),
) -> CurrentUser | None:
    if not session_cookie:
        return None
    user = await load_session(db, session_cookie)
    if user is not None:
        request.state.user = user
    return user


async def require_user(
    user: CurrentUser | None = Depends(optional_user),
) -> CurrentUser:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not_logged_in"
        )
    return user
