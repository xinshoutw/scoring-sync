from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.middleware import optional_user, require_user
from net_grading.auth.session import CurrentUser
from net_grading.routes.rate_limit import throttle_submit
from net_grading.auth.site2_creds import load_status as load_site2_status
from net_grading.config import get_settings
from net_grading.db.engine import get_session
from net_grading.db.models import TargetCache
from net_grading.routes.templating import templates
from net_grading.sites.base import Period, PeriodInfo, ScoreCard
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
    fire_and_forget,
    latest_logs_for_submission,
    preinsert_pending,
    run_sync_background,
)
from net_grading.sync.pull import (
    initial_import,
    list_skipped_targets,
    pending_conflicts_count,
)
from net_grading.sync.recheck import schedule_recheck
from sqlalchemy import select
import asyncio
import json
from sse_starlette.sse import EventSourceResponse
from net_grading.sync.sse import bus, is_sentinel


router = APIRouter()


async def _load_periods_from_user(site1_sid: str):
    """呼叫 Site1 /me 拿最新 periods（含 is_open）."""
    return (await Site1Client().me(site1_sid)).periods


def _period_info(periods: tuple[PeriodInfo, ...], period: Period) -> PeriodInfo | None:
    return next((item for item in periods if item.code == period), None)


async def _ensure_period_open(user: CurrentUser, period: Period) -> None:
    """403 period_closed / 400 invalid_period / 503 period_lookup_failed / 401 重登。
    403 + 503 的友善 HTML 錯誤頁由 app.py 的 exception handler 統一渲染。"""
    try:
        periods = await _load_periods_from_user(user.site1_sid)
    except SiteTokenExpired:
        raise HTTPException(status_code=401, detail="sid_expired") from None
    except SiteError as exc:
        raise HTTPException(status_code=503, detail=f"period_lookup_failed:{exc}") from exc

    current = _period_info(periods, period)
    if current is None:
        raise HTTPException(status_code=400, detail="invalid_period")
    if not current.is_open:
        raise HTTPException(status_code=403, detail="period_closed")


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
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/welcome" if not user.welcomed else "/dashboard", status_code=303)


@router.get("/welcome")
async def welcome(
    request: Request,
    user: CurrentUser = Depends(require_user),
    site2_error: str | None = Query(None),
) -> Response:
    if user.welcomed:
        return RedirectResponse("/dashboard?period=midterm", status_code=303)
    cfg = get_settings()
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "user": user,
            "site2_error": site2_error,
            "site_labels": {
                "site1": cfg.site1_label,
                "site2": cfg.site2_label,
                "site3": cfg.site3_label,
            },
        },
    )


@router.get("/dashboard")
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_user),
    period: Period = Query("midterm"),
    site2_error: str | None = Query(None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if not user.welcomed:
        return RedirectResponse("/welcome", status_code=303)
    if period not in ("midterm", "final"):
        raise HTTPException(status_code=400, detail="invalid_period")

    try:
        periods = await _load_periods_from_user(user.site1_sid)
    except SiteTokenExpired:
        return _force_relogin()
    except SiteError:
        periods = ()

    reload_error = await _refresh_targets_if_needed(db, user, period)
    import_summary = await initial_import(db, user, period)

    # 節流的背景 recheck：每位使用者 30s 最多一次；前景 render 不等它
    schedule_recheck(user.user_id, period, user.site1_sid)

    pending = await pending_conflicts_count(db, user.user_id)
    if pending > 0:
        return RedirectResponse("/conflicts", status_code=303)

    targets = await list_dashboard_targets(db, user.user_id, period)
    evaluated_count = sum(1 for t in targets if t.local_total is not None)
    period_label = next((p.label for p in periods if p.code == period), period)
    site2_status = await load_site2_status(db, user.user_id)
    skipped_targets = await list_skipped_targets(db, user.user_id, period)

    # 依 .env STUDENT_GROUPS 重排目標
    cfg = get_settings()
    grouped = _arrange_groups(targets, cfg.student_groups)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "period": period,
            "period_label": period_label,
            "periods": periods,
            "targets": targets,
            "grouped_targets": grouped,
            "evaluated_count": evaluated_count,
            "reload_error": reload_error,
            "import_summary": import_summary,
            "site2": site2_status,
            "site2_error": site2_error,
            "skipped_targets": skipped_targets,
            "site_labels": {
                "site1": cfg.site1_label,
                "site2": cfg.site2_label,
                "site3": cfg.site3_label,
            },
        },
    )


def _arrange_groups(targets, groups_cfg: list[list[str]]):
    """回 [(label, [target]), ...]；groups_cfg 空時就一個無標籤的 section."""
    by_id = {t.student_id: t for t in targets}
    used: set[str] = set()
    result: list[tuple[str, list]] = []
    for idx, group_ids in enumerate(groups_cfg):
        members = [by_id[sid] for sid in group_ids if sid in by_id]
        if members:
            result.append((f"第 {idx + 1} 組", members))
            used.update(t.student_id for t in members)
    leftover = [t for t in targets if t.student_id not in used]
    if leftover:
        label = "未分組" if result else ""
        result.append((label, leftover))
    return result


@router.get("/grade/{period}/{target_id}")
async def grade_form(
    request: Request,
    period: Period = Path(...),
    target_id: str = Path(..., pattern=r"^[A-Za-z0-9]+$"),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    saved_id: int | None = Query(None),
    live: int = Query(0),
) -> Response:
    if period not in ("midterm", "final"):
        raise HTTPException(status_code=400, detail="invalid_period")
    await _ensure_period_open(user, period)

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

    cfg = get_settings()
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
            "live": bool(live),
            "site_labels": {
                "site1": cfg.site1_label,
                "site2": cfg.site2_label,
                "site3": cfg.site3_label,
            },
        },
    )


@router.post("/grade/{period}/{target_id}")
async def grade_submit(
    request: Request,
    period: Period = Path(...),
    target_id: str = Path(..., pattern=r"^[A-Za-z0-9]+$"),
    user: CurrentUser = Depends(throttle_submit),
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
    await _ensure_period_open(user, period)
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

    # 使用者手動重送 = 新權威版本，先前任何（skip/未決）import-phase 衝突都失效
    from sqlalchemy import delete as _sa_delete
    from net_grading.db.models import ConflictEvent

    await db.execute(
        _sa_delete(ConflictEvent).where(
            ConflictEvent.user_id == user.user_id,
            ConflictEvent.period == period,
            ConflictEvent.target_student_id == target_id,
        )
    )
    await db.commit()

    enabled = user.enabled_sites()
    # 預寫 pending 列：讓 redirect 後的 GET 立刻能顯示三顆 ⏳
    if enabled:
        await preinsert_pending(db, saved.id, enabled)

    # Fire-and-forget；新 session 在 task 內開。保 reference 免 GC。
    fire_and_forget(
        run_sync_background(
            submission_id=saved.id,
            grader_id=user.user_id,
            grader_name=user.name,
            site1_sid=user.site1_sid,
            target_name=target_row.name,
            sites=enabled,
        )
    )

    return RedirectResponse(
        f"/grade/{period}/{target_id}?saved_id={saved.id}&live=1",
        status_code=303,
    )


@router.post("/sync/{submission_id}/retry/{site}")
async def sync_retry(
    request: Request,
    submission_id: int = Path(..., ge=1),
    site: str = Path(..., pattern=r"^site[123]$"),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    from net_grading.db.models import Submission

    sub = await db.get(Submission, submission_id)
    if sub is None or sub.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="submission_not_found")
    await _ensure_period_open(user, sub.period)

    target_row = await db.get(
        TargetCache, (user.user_id, sub.period, sub.target_student_id)
    )
    target_name = target_row.name if target_row else sub.target_student_id

    await preinsert_pending(db, sub.id, (site,))  # type: ignore[arg-type]
    fire_and_forget(
        run_sync_background(
            submission_id=sub.id,
            grader_id=user.user_id,
            grader_name=user.name,
            site1_sid=user.site1_sid,
            target_name=target_name,
            sites=(site,),  # type: ignore[arg-type]
        )
    )
    return RedirectResponse(
        f"/grade/{sub.period}/{sub.target_student_id}?saved_id={sub.id}&live=1",
        status_code=303,
    )


@router.get("/sync/{submission_id}/events")
async def sync_events(
    submission_id: int = Path(..., ge=1),
    user: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    from net_grading.db.models import Submission

    sub = await db.get(Submission, submission_id)
    if sub is None or sub.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="submission_not_found")

    async def gen():
        q = bus.subscribe(submission_id)
        try:
            # 重播既有狀態，避免 client 連線前已完成的事件遺漏
            for site_name, log in (await latest_logs_for_submission(db, submission_id)).items():
                yield {
                    "event": site_name,
                    "data": json.dumps(
                        {
                            "site": site_name,
                            "status": log.status,
                            "external_id": log.external_id,
                            "error": log.error_message,
                            "duration_ms": log.duration_ms,
                        }
                    ),
                }
            # 如果全部都已經完成，直接關
            logs_after = await latest_logs_for_submission(db, submission_id)
            if logs_after and all(
                log_row.status in ("success", "failed", "skipped")
                for log_row in logs_after.values()
            ):
                yield {"event": "done", "data": "{}"}
                return

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # 保活：瀏覽器 EventSource 預設 > 30s 無訊息會怒斷；送 comment
                    yield {"event": "ping", "data": "{}"}
                    continue
                if is_sentinel(event):
                    yield {"event": "done", "data": "{}"}
                    break
                yield {"event": event["site"], "data": json.dumps(event)}
        finally:
            bus.unsubscribe(submission_id, q)

    return EventSourceResponse(gen())


def _force_relogin() -> Response:
    from net_grading.auth.session import SESSION_COOKIE
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r
