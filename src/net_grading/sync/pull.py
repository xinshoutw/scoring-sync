"""首次匯入與衝突偵測：Site1 優先名單 + Site2 可選，按被評學生比對."""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.auth.session import CurrentUser
from net_grading.auth.site2_creds import get_id_token
from net_grading.db.models import ConflictEvent, Submission, TargetCache
from net_grading.sites.base import Period, ScoreCard, SiteName, SubmissionSnapshot
from net_grading.sites.errors import SiteError
from net_grading.sites.site1 import Site1Client
from net_grading.sites.site2 import Site2Client
from net_grading.sync.local import insert_local_submission


async def initial_import(
    db: AsyncSession,
    user: CurrentUser,
    period: Period,
) -> dict:
    """若 submissions 對 (user, period) 為空就拉 Site1/Site2；寫匯入或衝突。
    回傳 summary：{imported_site1, imported_site2, conflicts, agreements, errors}.
    """
    existing = (
        await db.execute(
            select(Submission.id)
            .where(
                Submission.user_id == user.user_id,
                Submission.period == period,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return {"skipped": "already_has_local"}

    site1 = Site1Client()
    errors: dict[str, str] = {}

    try:
        targets = await site1.list_targets(user.site1_sid, period)
    except SiteError as exc:
        errors["site1_targets"] = str(exc)
        targets = []

    sem = asyncio.Semaphore(10)

    async def _one(tid: str) -> SubmissionSnapshot | None:
        async with sem:
            try:
                return await site1.fetch_submission(user.site1_sid, period, tid)
            except SiteError:
                return None

    evaluated_targets = [t for t in targets if t.evaluated]
    site1_snaps = await asyncio.gather(*(_one(t.student_id) for t in evaluated_targets))
    site1_by_target: dict[str, SubmissionSnapshot] = {}
    for snap in site1_snaps:
        if snap is not None:
            site1_by_target[snap.target_student_id] = snap

    site2_by_target: dict[str, SubmissionSnapshot] = {}
    id_token = await get_id_token(db, user.user_id)
    if id_token is not None:
        try:
            s2_list = await Site2Client().list_submissions(id_token, user.user_id, period)
            for snap in s2_list:
                site2_by_target[snap.target_student_id] = snap
        except SiteError as exc:
            errors["site2_list"] = str(exc)

    imported_site1 = 0
    imported_site2 = 0
    conflicts = 0
    agreements = 0

    all_targets = set(site1_by_target.keys()) | set(site2_by_target.keys())
    for tid in all_targets:
        s1 = site1_by_target.get(tid)
        s2 = site2_by_target.get(tid)

        if s1 and s2:
            if _same_scores(s1, s2) and s1.comment == s2.comment:
                await insert_local_submission(
                    db, user.user_id, period, tid,
                    s2.scores, s2.comment, s2.self_note or "",
                    source="imported_site2",
                )
                agreements += 1
            else:
                db.add(ConflictEvent(
                    user_id=user.user_id,
                    period=period,
                    target_student_id=tid,
                    site1_snapshot=_snap_json(s1),
                    site2_snapshot=_snap_json(s2),
                    resolution=None,
                ))
                conflicts += 1
        elif s1:
            await insert_local_submission(
                db, user.user_id, period, tid,
                s1.scores, s1.comment, s1.self_note or "",
                source="imported_site1",
            )
            imported_site1 += 1
        elif s2:
            await insert_local_submission(
                db, user.user_id, period, tid,
                s2.scores, s2.comment, "",
                source="imported_site2",
            )
            imported_site2 += 1

    await db.commit()

    return {
        "imported_site1": imported_site1,
        "imported_site2": imported_site2,
        "conflicts": conflicts,
        "agreements": agreements,
        "errors": errors,
    }


async def pending_conflicts_count(
    db: AsyncSession, user_id: str, period: Period | None = None
) -> int:
    stmt = select(ConflictEvent.id).where(
        ConflictEvent.user_id == user_id,
        ConflictEvent.resolution.is_(None),
    )
    if period:
        stmt = stmt.where(ConflictEvent.period == period)
    return len(list((await db.execute(stmt)).scalars().all()))


async def list_skipped_targets(
    db: AsyncSession, user_id: str, period: Period
) -> set[str]:
    """使用者 skip 過的衝突 target_student_id 集合（dashboard 加標籤用）."""
    stmt = select(ConflictEvent.target_student_id).where(
        ConflictEvent.user_id == user_id,
        ConflictEvent.period == period,
        ConflictEvent.resolution == "skip",
    )
    return {r for r in (await db.execute(stmt)).scalars().all()}


async def recheck_conflicts(
    db: AsyncSession,
    user_id: str,
    period: Period,
    site1_sid: str,
) -> dict:
    """重新拉 Site1 / Site2，對每位被評學生比對：
    - 不一致 + 沒有未決/skip 紀錄 → INSERT conflict_events(resolution=NULL)
    - 一致 + 有現存 conflict_events → DELETE（obsolete，兩站已經自行對齊）
    - 絕不動 submissions；只動 conflict_events
    """
    site1 = Site1Client()
    try:
        targets = await site1.list_targets(site1_sid, period)
    except SiteError:
        return {"error": "site1_targets_failed"}

    sem = asyncio.Semaphore(10)

    async def _one_s1(tid: str):
        async with sem:
            try:
                return await site1.fetch_submission(site1_sid, period, tid)
            except SiteError:
                return None

    evaluated = [t for t in targets if t.evaluated]
    s1_list = await asyncio.gather(*(_one_s1(t.student_id) for t in evaluated))
    site1_by_target = {s.target_student_id: s for s in s1_list if s is not None}

    site2_by_target: dict = {}
    id_token = await get_id_token(db, user_id)
    if id_token is not None:
        try:
            s2_list = await Site2Client().list_submissions(id_token, user_id, period)
            for snap in s2_list:
                site2_by_target[snap.target_student_id] = snap
        except SiteError:
            pass

    # 抓現有 conflicts (含 skip) 做 dedupe / 對齊後的清掃
    existing_stmt = select(ConflictEvent).where(
        ConflictEvent.user_id == user_id,
        ConflictEvent.period == period,
    )
    existing_rows = (await db.execute(existing_stmt)).scalars().all()
    existing_by_target: dict[str, ConflictEvent] = {
        r.target_student_id: r for r in existing_rows
    }

    new_conflicts = 0
    obsoleted = 0

    all_targets = set(site1_by_target.keys()) | set(site2_by_target.keys())
    for tid in all_targets:
        s1 = site1_by_target.get(tid)
        s2 = site2_by_target.get(tid)
        prev = existing_by_target.get(tid)

        if not (s1 and s2):
            continue  # 僅單邊有資料 → 非衝突語意

        aligned = _same_scores(s1, s2) and s1.comment == s2.comment

        if aligned:
            if prev is not None:
                await db.delete(prev)
                obsoleted += 1
        else:
            if prev is None:
                db.add(
                    ConflictEvent(
                        user_id=user_id,
                        period=period,
                        target_student_id=tid,
                        site1_snapshot=_snap_json(s1),
                        site2_snapshot=_snap_json(s2),
                        resolution=None,
                    )
                )
                new_conflicts += 1
            # 若 prev.resolution == 'skip' 但內容又變了 → 保留 skip 不打擾
            # （使用者曾明確跳過；recheck 不該強拉回來）

    await db.commit()
    return {"new_conflicts": new_conflicts, "obsoleted": obsoleted}


async def list_pending_conflicts(
    db: AsyncSession, user_id: str
) -> list[ConflictEvent]:
    stmt = (
        select(ConflictEvent)
        .where(
            ConflictEvent.user_id == user_id,
            ConflictEvent.resolution.is_(None),
        )
        .order_by(ConflictEvent.period, ConflictEvent.target_student_id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def resolve_conflict(
    db: AsyncSession,
    user: CurrentUser,
    conflict_id: int,
    choice: str,
) -> None:
    """選 site1 → 把值寫回 site2（反之亦然）；skip 不動。"""
    if choice not in ("site1", "site2", "skip"):
        raise ValueError("invalid_choice")

    conflict = await db.get(ConflictEvent, conflict_id)
    if conflict is None or conflict.user_id != user.user_id:
        raise ValueError("not_found")
    if conflict.resolution is not None:
        return

    if choice == "skip":
        conflict.resolution = "skip"
        conflict.resolved_at = datetime.utcnow()
        await db.commit()
        return

    snap_json = conflict.site1_snapshot if choice == "site1" else conflict.site2_snapshot
    data = json.loads(snap_json)
    scores = ScoreCard(
        topic=data["scores"]["topic"],
        content=data["scores"]["content"],
        narrative=data["scores"]["narrative"],
        presentation=data["scores"]["presentation"],
        teamwork=data["scores"]["teamwork"],
    )
    submission = await insert_local_submission(
        db,
        user.user_id,
        conflict.period,
        conflict.target_student_id,
        scores,
        data.get("comment", ""),
        data.get("self_note") or "",
        source=f"imported_{choice}",
    )
    conflict.resolution = choice
    conflict.resolved_at = datetime.utcnow()
    await db.commit()

    # 對齊：把選中版本 push 回敗方站台，讓三站一致
    target_row = await db.get(
        TargetCache,
        (user.user_id, conflict.period, conflict.target_student_id),
    )
    target_name = target_row.name if target_row else conflict.target_student_id

    opposite: SiteName = "site2" if choice == "site1" else "site1"  # type: ignore[assignment]
    # lazy import 避免循環
    from net_grading.sync.orchestrator import sync_one_submission

    await sync_one_submission(
        db,
        grader_id=user.user_id,
        grader_name=user.name,
        site1_sid=user.site1_sid,
        submission=submission,
        target_name=target_name,
        sites=(opposite,),
    )


def _same_scores(a: SubmissionSnapshot, b: SubmissionSnapshot) -> bool:
    return (
        a.scores.topic == b.scores.topic
        and a.scores.content == b.scores.content
        and a.scores.narrative == b.scores.narrative
        and a.scores.presentation == b.scores.presentation
        and a.scores.teamwork == b.scores.teamwork
    )


def _snap_json(s: SubmissionSnapshot) -> str:
    return json.dumps(
        {
            "target_student_id": s.target_student_id,
            "period": s.period,
            "scores": {
                "topic": s.scores.topic,
                "content": s.scores.content,
                "narrative": s.scores.narrative,
                "presentation": s.scores.presentation,
                "teamwork": s.scores.teamwork,
            },
            "comment": s.comment,
            "self_note": s.self_note or "",
            "submitted_at": s.submitted_at.isoformat(),
            "external_id": s.external_id,
            "source": s.source,
        },
        ensure_ascii=False,
    )
