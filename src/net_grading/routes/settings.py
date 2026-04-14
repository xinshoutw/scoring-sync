from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import require_user
from net_grading.auth.session import CurrentUser
from net_grading.auth.site2_creds import load_status, revoke, save_credentials
from net_grading.db.engine import get_session
from net_grading.routes.templating import templates
from net_grading.sites.errors import SiteLoginError, SiteTransportError
from net_grading.sites.site2 import Site2Client


router = APIRouter(prefix="/settings")


@router.get("")
@router.get("/")
async def settings_page(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    site2 = await load_status(db, user.user_id)
    return templates.TemplateResponse(
        request, "settings.html", {"user": user, "site2": site2}
    )


@router.post("/site2")
async def site2_connect(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    email: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(None),
) -> Response:
    try:
        result = await Site2Client().login(email, password)
    except SiteLoginError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"user": user, "site2": None, "error": f"登入失敗：{exc}"},
            status_code=401,
        )
    except SiteTransportError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"user": user, "site2": None, "error": f"連線失敗：{exc}"},
            status_code=502,
        )

    if remember:
        await save_credentials(db, user.user_id, result)
    # 若不記住我：只存 session 記憶體。M1–M4 尚未實作 session-only store；
    # 目前都走 DB 儲存。未勾記住我 UX 留給 M7。
    else:
        await save_credentials(db, user.user_id, result)

    return RedirectResponse("/settings", status_code=303)


@router.post("/site2/revoke")
async def site2_revoke(
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await revoke(db, user.user_id)
    return RedirectResponse("/settings", status_code=303)
