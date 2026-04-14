"""Site1 (ita-grading) client。

Auth：POST /api/auth/identify 只傳 identifier；學生直接登入 + Set-Cookie sid。
Teacher/admin 會回 need_password=true，但本站只處理 student。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from net_grading.config import get_settings
from net_grading.sites.base import (
    Period,
    PeriodInfo,
    ScoreCard,
    StudentIdentity,
    SubmissionSnapshot,
    SubmitResult,
    Target,
)
from net_grading.sites.errors import (
    SiteLoginError,
    SiteTokenExpired,
    SiteTransportError,
    SiteUnsupportedRole,
)


USER_AGENT = "xinshoutw-scoring-sync/0.1 (+https://github.com/xinshoutw/scoring-sync)"
SID_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Site1LoginResult:
    identity: StudentIdentity
    sid: str
    sid_expires_at: datetime


class Site1Client:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        self._base_url = base_url or get_settings().site1_base_url
        self._timeout = timeout

    def _client(self, sid: str | None = None) -> httpx.AsyncClient:
        cookies = {"sid": sid} if sid else None
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"user-agent": USER_AGENT},
            cookies=cookies,
            http2=True,
        )

    async def identify(self, student_id: str) -> Site1LoginResult:
        """使用學號登入。只接受 student role。"""
        async with self._client() as client:
            try:
                r = await client.post(
                    "/api/auth/identify",
                    json={"identifier": student_id.strip()},
                )
            except httpx.HTTPError as exc:
                raise SiteTransportError(str(exc)) from exc

            if r.status_code == 404:
                raise SiteLoginError("not_found")
            if r.status_code == 429:
                raise SiteLoginError("rate_limited")
            if r.status_code != 200:
                raise SiteLoginError(f"identify_failed_{r.status_code}")

            body: dict[str, Any] = r.json()
            role = body.get("role")
            if role != "student":
                raise SiteUnsupportedRole(f"role={role}")
            if body.get("need_password"):
                raise SiteUnsupportedRole("student_needs_password_not_supported")

            sid = r.cookies.get("sid")
            if not sid:
                raise SiteLoginError("no_sid_cookie")

            periods = tuple(
                PeriodInfo(code=p["code"], label=p["label"], is_open=bool(p["is_open"]))
                for p in body.get("periods", [])
            )
            identity = StudentIdentity(
                actor_id=body["actor_id"],
                name=body["name"],
                class_name=body["class_name"],
                periods=periods,
            )
            return Site1LoginResult(
                identity=identity,
                sid=sid,
                sid_expires_at=datetime.now(timezone.utc) + SID_TTL,
            )

    async def me(self, sid: str) -> StudentIdentity:
        """驗證 sid 仍然有效，同時取回最新身份（periods 可能變更 is_open）。"""
        async with self._client(sid) as client:
            r = await client.get("/api/auth/me")
            if r.status_code == 401:
                raise SiteTokenExpired("sid_expired")
            if r.status_code != 200:
                raise SiteTransportError(f"me_failed_{r.status_code}")
            body = r.json()
            if body.get("role") != "student":
                raise SiteUnsupportedRole(f"role={body.get('role')}")
            periods = tuple(
                PeriodInfo(code=p["code"], label=p["label"], is_open=bool(p["is_open"]))
                for p in body.get("periods", [])
            )
            return StudentIdentity(
                actor_id=body["actor_id"],
                name=body["name"],
                class_name=body["class_name"],
                periods=periods,
            )

    async def list_targets(self, sid: str, period: Period) -> list[Target]:
        async with self._client(sid) as client:
            r = await client.get(f"/api/student/targets?period={period}")
            if r.status_code == 401:
                raise SiteTokenExpired("sid_expired")
            if r.status_code != 200:
                raise SiteTransportError(f"targets_failed_{r.status_code}")
            return [
                Target(
                    student_id=t["student_id"],
                    name=t["name"],
                    class_name=t["class_name"],
                    evaluated=bool(t["evaluated"]),
                    total=t["total"],
                )
                for t in r.json()
            ]

    async def fetch_submission(
        self, sid: str, period: Period, target_id: str
    ) -> SubmissionSnapshot | None:
        """回 None 表示尚未評分。"""
        async with self._client(sid) as client:
            r = await client.get(
                f"/api/student/submissions/{period}/{target_id}/detail"
            )
            if r.status_code == 401:
                raise SiteTokenExpired("sid_expired")
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise SiteTransportError(f"detail_failed_{r.status_code}")
            body = r.json()
            latest = body.get("latest")
            if not latest:
                return None
            return SubmissionSnapshot(
                target_student_id=target_id,
                period=period,
                scores=ScoreCard(
                    topic=latest["score_topic"],
                    content=latest["score_content"],
                    narrative=latest["score_narrative"],
                    presentation=latest["score_presentation"],
                    teamwork=latest["score_teamwork"],
                ),
                comment=latest.get("comment", "") or "",
                self_note=latest.get("self_note", "") or "",
                submitted_at=datetime.fromisoformat(
                    latest["submitted_at"].replace(" ", "T")
                ).replace(tzinfo=timezone.utc),
                external_id=str(latest["id"]),
                source="site1",
            )

    async def submit(
        self,
        sid: str,
        period: Period,
        target_id: str,
        scores: ScoreCard,
        comment: str,
        self_note: str,
    ) -> SubmitResult:
        payload = {
            "period": period,
            "target_student_id": target_id,
            "scores": {
                "topic": scores.topic,
                "content": scores.content,
                "narrative": scores.narrative,
                "presentation": scores.presentation,
                "teamwork": scores.teamwork,
            },
            "comment": comment,
            "self_note": self_note,
        }
        async with self._client(sid) as client:
            r = await client.post("/api/student/submissions", json=payload)
            if r.status_code == 401:
                raise SiteTokenExpired("sid_expired")
            if r.status_code >= 400:
                raise SiteTransportError(f"submit_failed_{r.status_code}:{r.text[:200]}")
            body = r.json()
            return SubmitResult(
                external_id=str(body.get("id")),
                raw_response=r.text[:4000],
            )
