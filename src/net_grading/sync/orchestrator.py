"""平行送出 + best-effort：一次阻塞約 2-3 秒等三站都回來，寫 sync_logs."""
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.session import CurrentUser
from net_grading.auth.site2_creds import get_id_token
from net_grading.db.models import Submission, SyncLog, utcnow
from net_grading.sites.base import Period, ScoreCard, SiteName
from net_grading.sites.errors import SiteError, SiteTokenExpired
from net_grading.sites.site1 import Site1Client
from net_grading.sites.site2 import Site2Client
from net_grading.sites.site3 import Site3Client


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

    def status_of(self, site: SiteName) -> SiteStatus:
        for r in self.results:
            if r.site == site:
                return r.status
        return "pending"


async def sync_one_submission(
    db: AsyncSession,
    user: CurrentUser,
    grader_name: str,
    submission: Submission,
    target_name: str,
    sites: tuple[SiteName, ...] | None = None,
) -> SyncOutcome:
    """sites=None 時自動採用 user.enabled_sites()（respect 使用者同步偏好）."""
    if sites is None:
        sites = user.enabled_sites()  # type: ignore[assignment]

    scores = ScoreCard(
        topic=submission.score_topic,
        content=submission.score_content,
        narrative=submission.score_narrative,
        presentation=submission.score_presentation,
        teamwork=submission.score_teamwork,
    )

    tasks: list[asyncio.Task[SiteResult]] = []
    for site in sites:
        match site:
            case "site1":
                tasks.append(
                    asyncio.create_task(
                        _do_site1(user, submission, scores)
                    )
                )
            case "site2":
                tasks.append(
                    asyncio.create_task(
                        _do_site2(db, user, grader_name, submission, target_name, scores)
                    )
                )
            case "site3":
                tasks.append(
                    asyncio.create_task(
                        _do_site3(user, grader_name, submission, target_name, scores)
                    )
                )

    results = await asyncio.gather(*tasks, return_exceptions=False)

    for r in results:
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
    return SyncOutcome(submission_id=submission.id, results=list(results))


async def _do_site1(
    user: CurrentUser, sub: Submission, scores: ScoreCard
) -> SiteResult:
    t0 = time.monotonic()
    try:
        result = await Site1Client().submit(
            user.site1_sid,
            sub.period,
            sub.target_student_id,
            scores,
            sub.comment or "",
            sub.self_note or "",
        )
    except SiteTokenExpired as exc:
        return _fail("site1", t0, None, f"token_expired:{exc}", None)
    except SiteError as exc:
        return _fail("site1", t0, None, str(exc), None)
    return SiteResult(
        site="site1",
        status="success",
        http_status=200,
        external_id=result.external_id,
        error=None,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=result.raw_response[:2000],
    )


async def _do_site2(
    db: AsyncSession,
    user: CurrentUser,
    grader_name: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    t0 = time.monotonic()
    id_token = await get_id_token(db, user.user_id)
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
    try:
        result = await Site2Client().submit(
            id_token,
            grader_id=user.user_id,
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
    return SiteResult(
        site="site2",
        status="success",
        http_status=200,
        external_id=result.external_id,
        error=None,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=result.raw_response[:2000],
    )


async def _do_site3(
    user: CurrentUser,
    grader_name: str,
    sub: Submission,
    target_name: str,
    scores: ScoreCard,
) -> SiteResult:
    t0 = time.monotonic()
    try:
        result = await Site3Client().submit(
            user.user_id,
            grader_name,
            sub.period,
            sub.target_student_id,
            target_name,
            scores,
            sub.comment or "",
        )
    except SiteError as exc:
        return _fail("site3", t0, None, str(exc), None)
    return SiteResult(
        site="site3",
        status="success",
        http_status=200,
        external_id=result.external_id,
        error=None,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=result.raw_response[:2000],
    )


def _fail(
    site: SiteName,
    t0: float,
    http_status: int | None,
    error: str,
    body: str | None,
) -> SiteResult:
    return SiteResult(
        site=site,
        status="failed",
        http_status=http_status,
        external_id=None,
        error=error,
        duration_ms=int((time.monotonic() - t0) * 1000),
        response_body=body,
    )


async def latest_logs_for_submission(
    db: AsyncSession, submission_id: int
) -> dict[SiteName, SyncLog]:
    """查該 submission 對每一站的「最近一次」sync_log."""
    stmt = (
        select(SyncLog)
        .where(SyncLog.submission_id == submission_id)
        .order_by(desc(SyncLog.attempted_at))
    )
    rows = (await db.execute(stmt)).scalars().all()
    seen: dict[SiteName, SyncLog] = {}
    for row in rows:
        if row.site not in seen:
            seen[row.site] = row  # type: ignore[assignment]
    return seen
