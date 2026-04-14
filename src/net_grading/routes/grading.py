from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import optional_user, require_user
from net_grading.auth.session import CurrentUser
from net_grading.db.engine import get_session
from net_grading.db.models import TargetCache
from net_grading.routes.templating import templates
from net_grading.sites.base import Period, ScoreCard
from net_grading.sites.errors import SiteError, SiteTokenExpired
from net_grading.sites.site1 import Site1Client
from net_grading.sync.local import (
    SCORE_MAX,
    get_latest_submission,
    insert_local_submission,
    list_dashboard_targets,
    list_submission_history,
    upsert_targets_cache,
)
from net_grading.sync.orchestrator import (
    latest_logs_for_submission,
    sync_one_submission,
)
from net_grading.sync.pull import initial_import, pending_conflicts_count
from sqlalchemy import select


router = APIRouter()


async def _load_periods_from_user(site1_sid: str):
    """呼叫 Site1 /me 拿最新 periods（含 is_open）."""
    return (await Site1Client().me(site1_sid)).periods


async def _refresh_targets_if_needed(
    db: AsyncSession, user: CurrentUser, period: Period
) -> str | None:
    """若 targets_cache 該 (user, period) 為空，就呼叫 Site1 拉一次."""
    stmt = select(TargetCache).where(
        TargetCache.user_id == user.user_id, TargetCache.period == period
    ).limit(1)
    has_cache = (await db.execute(stmt)).first() is not None
    if has_cache:
        return None
    try:
        targets = await Site1Client().list_targets(user.site1_sid, period)
        await upsert_targets_cache(db, user.user_id, period, targets)
        return None
    except SiteError as exc:
        return str(exc)


@router.get("/")
async def root(user: CurrentUser | None = Depends(optional_user)) -> Response:
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)


@router.get("/dashboard")
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_user),
    period: Period = Query("midterm"),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if period not in ("midterm", "final"):
        raise HTTPException(status_code=400, detail="invalid_period")

    try:
        periods = await _load_periods_from_user(user.site1_sid)
    except SiteTokenExpired:
        return _force_relogin()
    except SiteError as exc:
        # Site1 暫時無法連線：仍用本地快取顯示
        periods = ()

    reload_error = await _refresh_targets_if_needed(db, user, period)

    # 首次匯入（僅當本地該期別為空時觸發一次）
    import_summary = await initial_import(db, user, period)

    # 如有 pending 衝突，強制導向 /conflicts
    pending = await pending_conflicts_count(db, user.user_id)
    if pending > 0:
        return RedirectResponse("/conflicts", status_code=303)

    targets = await list_dashboard_targets(db, user.user_id, period)
    evaluated_count = sum(1 for t in targets if t.local_total is not None)
    period_label = next((p.label for p in periods if p.code == period), period)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "period": period,
            "period_label": period_label,
            "periods": periods,
            "targets": targets,
            "evaluated_count": evaluated_count,
            "reload_error": reload_error,
            "import_summary": import_summary,
        },
    )


@router.get("/grade/{period}/{target_id}")
async def grade_form(
    request: Request,
    period: Period = Path(...),
    target_id: str = Path(..., pattern=r"^[A-Za-z0-9]+$"),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    saved_id: int | None = Query(None),
) -> Response:
    if period not in ("midterm", "final"):
        raise HTTPException(status_code=400, detail="invalid_period")

    target_row = await db.get(TargetCache, (user.user_id, period, target_id))
    if target_row is None:
        raise HTTPException(status_code=404, detail="target_not_in_list")

    latest = await get_latest_submission(db, user.user_id, period, target_id)
    history = await list_submission_history(db, user.user_id, period, target_id)

    scores = {
        "topic": latest.score_topic if latest else SCORE_MAX["topic"],
        "content": latest.score_content if latest else SCORE_MAX["content"],
        "narrative": latest.score_narrative if latest else SCORE_MAX["narrative"],
        "presentation": latest.score_presentation if latest else SCORE_MAX["presentation"],
        "teamwork": latest.score_teamwork if latest else SCORE_MAX["teamwork"],
    }

    # 同步狀態：若有 saved_id 就拿那筆的；否則拿最新 submission 的
    sync_of_submission_id = saved_id or (latest.id if latest else None)
    sync_status = (
        await latest_logs_for_submission(db, sync_of_submission_id)
        if sync_of_submission_id
        else {}
    )

    return templates.TemplateResponse(
        request,
        "grade.html",
        {
            "user": user,
            "period": period,
            "target": {
                "student_id": target_row.target_student_id,
                "name": target_row.name,
                "class_name": target_row.class_name,
                "is_self": bool(target_row.is_self),
            },
            "scores": scores,
            "comment": latest.comment if latest else "",
            "self_note": latest.self_note if latest else "",
            "history": history,
            "saved_id": saved_id,
            "sync_submission_id": sync_of_submission_id,
            "sync_status": sync_status,
        },
    )


@router.post("/grade/{period}/{target_id}")
async def grade_submit(
    request: Request,
    period: Period = Path(...),
    target_id: str = Path(..., pattern=r"^[A-Za-z0-9]+$"),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    score_topic: int = Form(...),
    score_content: int = Form(...),
    score_narrative: int = Form(...),
    score_presentation: int = Form(...),
    score_teamwork: int = Form(...),
    comment: str = Form(""),
    self_note: str = Form(""),
) -> Response:
    if period not in ("midterm", "final"):
        raise HTTPException(status_code=400, detail="invalid_period")
    target_row = await db.get(TargetCache, (user.user_id, period, target_id))
    if target_row is None:
        raise HTTPException(status_code=404, detail="target_not_in_list")

    fields = {
        "topic": score_topic,
        "content": score_content,
        "narrative": score_narrative,
        "presentation": score_presentation,
        "teamwork": score_teamwork,
    }
    for name, value in fields.items():
        max_ = SCORE_MAX[name]
        if not (0 <= value <= max_):
            raise HTTPException(
                status_code=400,
                detail=f"score_{name}_out_of_range_0_to_{max_}",
            )

    saved = await insert_local_submission(
        db,
        user.user_id,
        period,
        target_id,
        ScoreCard(**fields),
        comment=comment.strip(),
        self_note=self_note.strip(),
        source="local",
    )

    # 阻塞並行送出三站（~2-3 秒）
    await sync_one_submission(
        db,
        user,
        grader_name=user.name,
        submission=saved,
        target_name=target_row.name,
    )

    return RedirectResponse(
        f"/grade/{period}/{target_id}?saved_id={saved.id}", status_code=303
    )


@router.post("/sync/{submission_id}/retry/{site}")
async def sync_retry(
    submission_id: int = Path(..., ge=1),
    site: str = Path(..., pattern=r"^site[123]$"),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    from net_grading.db.models import Submission

    sub = await db.get(Submission, submission_id)
    if sub is None or sub.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="submission_not_found")

    target_row = await db.get(
        TargetCache, (user.user_id, sub.period, sub.target_student_id)
    )
    target_name = target_row.name if target_row else sub.target_student_id

    await sync_one_submission(
        db,
        user,
        grader_name=user.name,
        submission=sub,
        target_name=target_name,
        sites=(site,),  # type: ignore[arg-type]
    )
    return RedirectResponse(
        f"/grade/{sub.period}/{sub.target_student_id}?saved_id={sub.id}",
        status_code=303,
    )


def _force_relogin() -> Response:
    from net_grading.auth.session import SESSION_COOKIE
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r
