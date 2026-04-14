"""本地 SQLite 查詢 / 寫入工具。跨站同步邏輯不在這層（M5 orchestrator 才做）."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.db.models import Submission, TargetCache
from net_grading.sites.base import Period, ScoreCard, Target


SCORE_MAX = {
    "topic": 30,
    "content": 30,
    "narrative": 20,
    "presentation": 10,
    "teamwork": 10,
}
TOTAL_MAX = sum(SCORE_MAX.values())


@dataclass(frozen=True)
class DashboardTarget:
    """Dashboard 列表的每一列：來自 targets_cache + 最新本地 submission."""

    student_id: str
    name: str
    class_name: str
    is_self: bool
    local_total: int | None
    local_submitted_at: datetime | None


async def upsert_targets_cache(
    db: AsyncSession,
    user_id: str,
    period: Period,
    targets: list[Target],
) -> None:
    """整批 upsert（每次 dashboard 重整呼叫）."""
    for t in targets:
        stmt = (
            sqlite_insert(TargetCache)
            .values(
                user_id=user_id,
                period=period,
                target_student_id=t.student_id,
                name=t.name,
                class_name=t.class_name,
                is_self=1 if t.student_id == user_id else 0,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "period", "target_student_id"],
                set_={
                    "name": t.name,
                    "class_name": t.class_name,
                    "is_self": 1 if t.student_id == user_id else 0,
                },
            )
        )
        await db.execute(stmt)
    await db.commit()


async def list_dashboard_targets(
    db: AsyncSession, user_id: str, period: Period
) -> list[DashboardTarget]:
    """targets_cache 全量 + 每位學生的本地最新 submission（如果有）."""
    cache_stmt = (
        select(TargetCache)
        .where(TargetCache.user_id == user_id, TargetCache.period == period)
        .order_by(TargetCache.target_student_id)
    )
    rows = (await db.execute(cache_stmt)).scalars().all()

    result: list[DashboardTarget] = []
    for row in rows:
        latest = await get_latest_submission(db, user_id, period, row.target_student_id)
        result.append(
            DashboardTarget(
                student_id=row.target_student_id,
                name=row.name,
                class_name=row.class_name,
                is_self=bool(row.is_self),
                local_total=latest.total if latest else None,
                local_submitted_at=latest.submitted_at if latest else None,
            )
        )
    return result


async def get_latest_submission(
    db: AsyncSession, user_id: str, period: Period, target_id: str
) -> Submission | None:
    stmt = (
        select(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.period == period,
            Submission.target_student_id == target_id,
        )
        .order_by(desc(Submission.submitted_at))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_submission_history(
    db: AsyncSession, user_id: str, period: Period, target_id: str
) -> list[Submission]:
    stmt = (
        select(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.period == period,
            Submission.target_student_id == target_id,
        )
        .order_by(desc(Submission.submitted_at))
    )
    return list((await db.execute(stmt)).scalars().all())


async def insert_local_submission(
    db: AsyncSession,
    user_id: str,
    period: Period,
    target_id: str,
    scores: ScoreCard,
    comment: str,
    self_note: str,
    source: str = "local",
) -> Submission:
    row = Submission(
        user_id=user_id,
        period=period,
        target_student_id=target_id,
        score_topic=scores.topic,
        score_content=scores.content,
        score_narrative=scores.narrative,
        score_presentation=scores.presentation,
        score_teamwork=scores.teamwork,
        total=scores.total,
        comment=comment,
        self_note=self_note,
        source=source,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
