from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from net_grading.auth.middleware import optional_user, require_user
from net_grading.auth.session import CurrentUser
from net_grading.routes.templating import templates


router = APIRouter()


@router.get("/")
async def root(user: CurrentUser | None = Depends(optional_user)) -> Response:
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)


@router.get("/dashboard")
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> Response:
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user}
    )
