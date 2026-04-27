import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
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
from net_grading.db.models import LoginRecord
from net_grading.routes.templating import templates
from net_grading.sites.errors import (
    SiteLoginError,
    SiteTransportError,
    SiteUnsupportedRole,
)
from net_grading.sites.site1 import Site1Client


log = logging.getLogger(__name__)

router = APIRouter()


def _client_ip(request: Request) -> str:
    """取最外層 client IP；優先看 X-Forwarded-For，方便 reverse proxy 後使用。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


async def _record_login_and_log_history(
    db: AsyncSession,
    *,
    ip: str,
    student_id: str,
    user_agent: str | None,
) -> None:
    """查詢此 IP 過去的登入紀錄並寫到 console，再寫入這次的新紀錄。"""
    stmt = (
        select(LoginRecord.student_id, LoginRecord.created_at)
        .where(LoginRecord.ip == ip)
        .order_by(LoginRecord.created_at.desc())
        .limit(50)
    )
    rows = (await db.execute(stmt)).all()

    log.info("[login] ip=%s student_id=%s 登入成功", ip, student_id)
    if rows:
        log.info("[login] ip=%s 過去登入紀錄（最近 %d 筆）：", ip, len(rows))
        for sid, ts in rows:
            ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            log.info("[login]   - %s @ %s", sid, ts_aware.isoformat())
    else:
        log.info("[login] ip=%s 為首次登入紀錄", ip)

    db.add(
        LoginRecord(
            ip=ip,
            student_id=student_id,
            user_agent=(user_agent or None),
        )
    )
    await db.commit()


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
    student_id = student_id.upper().strip()
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

    await _record_login_and_log_history(
        db,
        ip=_client_ip(request),
        student_id=result.identity.actor_id,
        user_agent=request.headers.get("user-agent"),
    )

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
