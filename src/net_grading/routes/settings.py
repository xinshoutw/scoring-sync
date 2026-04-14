"""Site2 憑證的 POST 端點（GET 頁面已內嵌到 dashboard 側欄）."""
from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import require_user
from net_grading.auth.session import CurrentUser
from net_grading.auth.site2_creds import revoke, save_credentials
from net_grading.db.engine import get_session
from net_grading.sites.errors import SiteLoginError, SiteTransportError
from net_grading.sites.site2 import Site2Client


router = APIRouter()


@router.post("/site2/connect")
async def site2_connect(
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    email: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(None),
    period: str = Form("midterm"),
) -> Response:
    redirect_to = f"/dashboard?period={period}"
    try:
        result = await Site2Client().login(email, password)
    except (SiteLoginError, SiteTransportError) as exc:
        return RedirectResponse(
            f"{redirect_to}&site2_error={_urlenc(str(exc))}",
            status_code=303,
        )
    await save_credentials(db, user.user_id, result)
    return RedirectResponse(redirect_to, status_code=303)


@router.post("/site2/revoke")
async def site2_revoke(
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    period: str = Form("midterm"),
) -> Response:
    await revoke(db, user.user_id)
    return RedirectResponse(f"/dashboard?period={period}", status_code=303)


def _urlenc(s: str) -> str:
    from urllib.parse import quote

    return quote(s[:200])
