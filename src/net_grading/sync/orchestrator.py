"""三站並行送出；每完成一站立刻寫 sync_logs + publish SSE event。"""
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Coroutine, Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.site2_creds import get_id_token
from net_grading.db.engine import get_session_factory
from net_grading.db.models import Submission, SyncLog, utcnow
from net_grading.sites.base import ScoreCard, SiteName
from net_grading.sites.errors import SiteError, SiteTokenExpired
from net_grading.sites.site1 import Site1Client
from net_grading.sites.site2 import Site2Client
from net_grading.sites.site3 import Site3Client
from net_grading.sync.sse import bus


# ─── Fire-and-forget task registry：避免 Python GC 清掉背景 task ────
_bg_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro: Coroutine[Any, Any, None]) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


SiteStatus = Literal["pending", "success", "failed", "skipped"]


@dataclass(frozen=True)
class SiteResult:
    site: SiteName
    status: SiteStatus
    http_status: int | None
    external_id: str | None
    error: str | None
    duration_ms: int
    response_body: str | None


@dataclass(frozen=True)
class SyncOutcome:
    submission_id: int
    results: list[SiteResult]


# ─── 前景版（POST /grade 舊行為保留）+ SSE 發佈 ──────────────────────

async def sync_one_submission(
    db: AsyncSession,
    *,
    grader_id: str,
    grader_name: str,
    site1_sid: str,
    submission: Submission,
    target_name: str,
    sites: tuple[SiteName, ...],
) -> SyncOutcome:
    scores = _scores_of(submission)
    tasks = [
        asyncio.create_task(
            _dispatch_safe(
                site,
                db,
                grader_id,
                grader_name,
                site1_sid,
                submission,
                target_name,
                scores,
            )
        )
        for site in sites
    ]

    results: list[SiteResult] = []
    for fut in asyncio.as_completed(tasks):
        r = await fut
        db.add(
            SyncLog(
                submission_id=submission.id,
                site=r.site,
                status=r.status,
                http_status=r.http_status,
                external_id=r.external_id,
                error_message=r.error,
                response_body=r.response_body,
                duration_ms=r.duration_ms,
                attempted_at=utcnow(),
            )
        )
        await db.commit()
        bus.publish(submission.id, _evt_payload(r))
        results.append(r)

    bus.publish(submission.id, {"site": "done", "status": "done"})
    bus.close(submission.id)
    return SyncOutcome(submission_id=submission.id, results=results)


# ─── 背景版：不需 HTTP 連線，fresh DB session ─────────────────────────

async def run_sync_background(
    *,
    submission_id: int,
    grader_id: str,
    grader_name: str,
    site1_sid: str,
    target_name: str,
    sites: tuple[SiteName, ...],
) -> None:
    async with get_session_factory()() as db:
        sub = await db.get(Submission, submission_id)
        if sub is None:
            return
        await sync_one_submission(
            db,
            grader_id=grader_id,
            grader_name=grader_name,
            site1_sid=site1_sid,
            submission=sub,
            target_name=target_name,
            sites=sites,
        )


# ─── 預寫 pending 列（讓 UI 進來就看到三顆 ⏳）─────────────────────────

async def preinsert_pending(
    db: AsyncSession, submission_id: int, sites: tuple[SiteName, ...]
) -> None:
    for site in sites:
        db.add(
            SyncLog(
                submission_id=submission_id,
                site=site,
                status="pending",
                attempted_at=utcnow(),
            )
        )
    await db.commit()


async def latest_logs_for_submission(
    db: AsyncSession, submission_id: int
) -> dict[SiteName, SyncLog]:
    stmt = (
        select(SyncLog)
        .where(SyncLog.submission_id == submission_id)
        .order_by(desc(SyncLog.id))
    )
    rows = (await db.execute(stmt)).scalars().all()
    seen: dict[SiteName, SyncLog] = {}
    for row in rows:
        if row.site not in seen:
            seen[row.site] = row  # type: ignore[assignment]
    return seen


# ─── 內部：單站執行 ────────────────────────────────────────────────────

async def _dispatch(
    site: SiteName,
    db: AsyncSession,
    grader_id: str,
    grader_name: str,
    site1_sid: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    match site:
        case "site1":
            return await _do_site1(site1_sid, sub, scores)
        case "site2":
            return await _do_site2(db, grader_id, grader_name, sub, target_name, scores)
        case "site3":
            return await _do_site3(grader_id, grader_name, sub, target_name, scores)
    return _fail(site, time.monotonic(), None, "unknown_site", None)


async def _dispatch_safe(
    site: SiteName,
    db: AsyncSession,
    grader_id: str,
    grader_name: str,
    site1_sid: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    t0 = time.monotonic()
    try:
        return await _dispatch(
            site, db, grader_id, grader_name, site1_sid, sub, target_name, scores
        )
    except Exception as exc:  # pragma: no cover - 最後一道防線，避免單站異常拖垮整體同步
        return _fail(
            site,
            t0,
            None,
            f"unexpected_{type(exc).__name__}:{str(exc)[:200]}",
            None,
        )


async def _do_site1(site1_sid: str, sub: Submission, scores: ScoreCard) -> SiteResult:
    t0 = time.monotonic()
    try:
        result = await Site1Client().submit(
            site1_sid, sub.period, sub.target_student_id, scores, sub.comment or "", sub.self_note or ""
        )
    except SiteTokenExpired as exc:
        return _fail("site1", t0, None, f"token_expired:{exc}", None)
    except SiteError as exc:
        return _fail("site1", t0, None, str(exc), None)
    return _ok("site1", t0, result.external_id, result.raw_response)


async def _do_site2(
    db: AsyncSession,
    grader_id: str,
    grader_name: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    t0 = time.monotonic()
    try:
        id_token = await get_id_token(db, grader_id)
        if id_token is None:
            return SiteResult(
                site="site2",
                status="skipped",
                http_status=None,
                external_id=None,
                error="site2_not_connected",
                duration_ms=int((time.monotonic() - t0) * 1000),
                response_body=None,
            )
        result = await Site2Client().submit(
            id_token,
            grader_id=grader_id,
            grader_name=grader_name,
            period=sub.period,
            target_id=sub.target_student_id,
            target_name=target_name,
            scores=scores,
            comment=sub.comment or "",
        )
    except SiteTokenExpired as exc:
        return _fail("site2", t0, None, f"token_expired:{exc}", None)
    except SiteError as exc:
        return _fail("site2", t0, None, str(exc), None)
    return _ok("site2", t0, result.external_id, result.raw_response)


async def _do_site3(
    grader_id: str,
    grader_name: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    t0 = time.monotonic()
    try:
        result = await Site3Client().submit(
            grader_id, grader_name, sub.period, sub.target_student_id, target_name, scores, sub.comment or ""
        )
    except SiteError as exc:
        return _fail("site3", t0, None, str(exc), None)
    return _ok("site3", t0, result.external_id, result.raw_response)


# ─── helpers ───────────────────────────────────────────────────────────

def _scores_of(sub: Submission) -> ScoreCard:
    return ScoreCard(
        topic=sub.score_topic,
        content=sub.score_content,
        narrative=sub.score_narrative,
        presentation=sub.score_presentation,
        teamwork=sub.score_teamwork,
    )


def _ok(site: SiteName, t0: float, external_id: str | None, body: str) -> SiteResult:
    return SiteResult(
        site=site,
        status="success",
        http_status=200,
        external_id=external_id,
        error=None,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=body[:2000] if body else None,
    )


def _fail(site: SiteName, t0: float, http_status: int | None, error: str, body: str | None) -> SiteResult:
    return SiteResult(
        site=site,
        status="failed",
        http_status=http_status,
        external_id=None,
        error=error,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=body,
    )


def _evt_payload(r: SiteResult) -> dict:
    return {
        "site": r.site,
        "status": r.status,
        "external_id": r.external_id,
        "error": r.error,
        "duration_ms": r.duration_ms,
    }
