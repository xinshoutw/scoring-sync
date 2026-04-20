from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import optional_user, require_user
from net_grading.auth.session import (
    SESSION_COOKIE,
    CurrentUser,
    create_session,
    destroy_session,
)
from net_grading.config import get_settings
from net_grading.db.engine import get_session
from net_grading.routes.templating import templates
from net_grading.sites.errors import (
    SiteLoginError,
    SiteTransportError,
    SiteUnsupportedRole,
)
from net_grading.sites.site1 import Site1Client


router = APIRouter()


@router.get("/login")
async def login_form(
    request: Request,
    user: CurrentUser | None = Depends(optional_user),
) -> Response:
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None})


@router.post("/login")
async def login_submit(
    request: Request,
    student_id: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    client = Site1Client()
    student_id = student_id.upper()
    try:
        if student_id == 'B11315009':
            return templates.TemplateResponse(
                request,
                "login.html",
                {"user": None, "student_id": student_id, "error": "未授權的行為"},
                status_code=401,
            )

        result = await client.identify(student_id)
    except SiteUnsupportedRole as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "student_id": student_id,
                "error": f"本站僅支援學生帳號（{exc}）",
            },
            status_code=400,
        )
    except SiteLoginError as exc:
        msg = "查無此學號" if "not_found" in str(exc) else f"登入失敗：{exc}"
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "student_id": student_id, "error": msg},
            status_code=401,
        )
    except SiteTransportError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "student_id": student_id,
                "error": f"Site1 連線失敗：{exc}",
            },
            status_code=502,
        )

    session_id, expires_at = await create_session(db, result)

    # 首次登入沒 welcomed 過就先進 onboarding
    from net_grading.db.models import User as _U
    _u = await db.get(_U, result.identity.actor_id)
    welcomed = bool(_u.welcomed) if _u else False

    response = RedirectResponse(
        "/dashboard" if welcomed else "/welcome", status_code=303
    )
    settings = get_settings()
    max_age = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await destroy_session(db, user.session_id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
