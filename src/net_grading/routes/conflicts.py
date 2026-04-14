import json

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import require_user
from net_grading.auth.session import CurrentUser
from net_grading.db.engine import get_session
from net_grading.routes.templating import templates
from net_grading.sync.pull import (
    list_pending_conflicts,
    resolve_conflict,
)


router = APIRouter()


@router.get("/conflicts")
async def conflicts_page(
    request: Request,
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    rows = await list_pending_conflicts(db, user.user_id)
    items = [
        {
            "id": r.id,
            "period": r.period,
            "target_student_id": r.target_student_id,
            "site1": json.loads(r.site1_snapshot),
            "site2": json.loads(r.site2_snapshot),
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request, "conflicts.html", {"user": user, "items": items}
    )


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve(
    conflict_id: int = Path(..., ge=1),
    choice: str = Form(...),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await resolve_conflict(db, user, conflict_id, choice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/conflicts", status_code=303)
