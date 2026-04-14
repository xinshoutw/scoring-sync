"""Site2 (ntust-grading / Firebase) client。

Auth：POST identitytoolkit.googleapis.com/v1/accounts:signInWithPassword
Refresh：POST securetoken.googleapis.com/v1/token  grant_type=refresh_token
Write：POST firestore.googleapis.com/v1/projects/{project}/databases/(default)/documents/grades
       with body {"fields": {...}} （新建 doc；對齊 Site1 append-only 語意）

> Firestore grades 集合的實際欄位命名（`score_topic` vs `scoreTopic` 等）在 spec #9
> 列為開放問題。本檔採 snake_case 並開 override hook，必要時實作端直接調。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from net_grading.config import get_settings
from net_grading.sites.base import (
    Period,
    ScoreCard,
    SubmissionSnapshot,
    SubmitResult,
)
from net_grading.sites.errors import (
    SiteLoginError,
    SiteNotSupported,
    SiteTokenExpired,
    SiteTransportError,
)


IDENTITY_BASE = "https://identitytoolkit.googleapis.com"
SECURETOKEN_BASE = "https://securetoken.googleapis.com"
FIRESTORE_BASE = "https://firestore.googleapis.com"
USER_AGENT = "net-grading/0.1 (+https://github.com/xinshoutw/net-grading)"


@dataclass(frozen=True)
class Site2LoginResult:
    email: str
    local_id: str
    id_token: str
    refresh_token: str
    id_token_expires_at: datetime


@dataclass(frozen=True)
class Site2RefreshResult:
    id_token: str
    refresh_token: str
    id_token_expires_at: datetime


def _to_fs_value(v: Any) -> dict[str, Any]:
    """Python → Firestore REST Value."""
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, datetime):
        iso = v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"timestampValue": iso}
    raise TypeError(f"unsupported type: {type(v)}")


def _fs_fields(d: dict[str, Any]) -> dict[str, Any]:
    return {"fields": {k: _to_fs_value(v) for k, v in d.items()}}


class Site2Client:
    def __init__(self, timeout: float = 10.0) -> None:
        s = get_settings()
        self._api_key = s.site2_firebase_api_key
        self._project = s.site2_firebase_project
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            headers={"user-agent": USER_AGENT},
            http2=True,
        )

    async def login(self, email: str, password: str) -> Site2LoginResult:
        async with self._client() as c:
            try:
                r = await c.post(
                    f"{IDENTITY_BASE}/v1/accounts:signInWithPassword",
                    params={"key": self._api_key},
                    json={
                        "email": email,
                        "password": password,
                        "returnSecureToken": True,
                        "clientType": "CLIENT_TYPE_WEB",
                    },
                )
            except httpx.HTTPError as exc:
                raise SiteTransportError(str(exc)) from exc
            if r.status_code != 200:
                err = r.json().get("error", {}).get("message", r.text[:200])
                raise SiteLoginError(err)
            body = r.json()
            expires_in = int(body.get("expiresIn", 3600))
            return Site2LoginResult(
                email=body["email"],
                local_id=body["localId"],
                id_token=body["idToken"],
                refresh_token=body["refreshToken"],
                id_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            )

    async def refresh(self, refresh_token: str) -> Site2RefreshResult:
        async with self._client() as c:
            try:
                r = await c.post(
                    f"{SECURETOKEN_BASE}/v1/token",
                    params={"key": self._api_key},
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                )
            except httpx.HTTPError as exc:
                raise SiteTransportError(str(exc)) from exc
            if r.status_code != 200:
                err = r.json().get("error", {}).get("message", r.text[:200])
                raise SiteTokenExpired(err)
            body = r.json()
            expires_in = int(body.get("expires_in", 3600))
            return Site2RefreshResult(
                id_token=body["id_token"],
                refresh_token=body["refresh_token"],
                id_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            )

    async def submit(
        self,
        id_token: str,
        grader_id: str,
        period: Period,
        target_id: str,
        scores: ScoreCard,
        comment: str,
    ) -> SubmitResult:
        """新建一筆 grades document（append）。"""
        fields = {
            "graderId": grader_id,
            "targetId": target_id,
            "period": period,
            "score_topic": scores.topic,
            "score_content": scores.content,
            "score_narrative": scores.narrative,
            "score_presentation": scores.presentation,
            "score_teamwork": scores.teamwork,
            "total": scores.total,
            "comment": comment,
            "submittedAt": datetime.now(timezone.utc),
        }
        url = (
            f"{FIRESTORE_BASE}/v1/projects/{self._project}/databases/(default)/documents/grades"
        )
        async with self._client() as c:
            try:
                r = await c.post(
                    url,
                    headers={"authorization": f"Bearer {id_token}"},
                    json=_fs_fields(fields),
                )
            except httpx.HTTPError as exc:
                raise SiteTransportError(str(exc)) from exc
            if r.status_code == 401:
                raise SiteTokenExpired("id_token_expired")
            if r.status_code >= 400:
                raise SiteTransportError(
                    f"firestore_write_failed_{r.status_code}:{r.text[:200]}"
                )
            body = r.json()
            doc_name = body.get("name", "")
            return SubmitResult(external_id=doc_name, raw_response=r.text[:4000])

    async def list_submissions(
        self, id_token: str, grader_id: str, period: Period
    ) -> list[SubmissionSnapshot]:
        """以 structuredQuery 拉該 grader 該期別的全部紀錄；同一 target 取最新一筆."""
        url = (
            f"{FIRESTORE_BASE}/v1/projects/{self._project}"
            f"/databases/(default)/documents:runQuery"
        )
        query = {
            "structuredQuery": {
                "from": [{"collectionId": "grades"}],
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "graderId"},
                                    "op": "EQUAL",
                                    "value": {"stringValue": grader_id},
                                }
                            },
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "period"},
                                    "op": "EQUAL",
                                    "value": {"stringValue": period},
                                }
                            },
                        ],
                    }
                },
            }
        }
        async with self._client() as c:
            r = await c.post(
                url,
                headers={"authorization": f"Bearer {id_token}"},
                json=query,
            )
            if r.status_code == 401:
                raise SiteTokenExpired("id_token_expired")
            if r.status_code >= 400:
                raise SiteTransportError(
                    f"firestore_query_failed_{r.status_code}:{r.text[:200]}"
                )
            rows = r.json()
            latest: dict[str, SubmissionSnapshot] = {}
            for row in rows:
                doc = row.get("document")
                if not doc:
                    continue
                f = doc.get("fields", {})
                snap = _doc_to_snapshot(doc.get("name", ""), f, period)
                if snap is None:
                    continue
                prev = latest.get(snap.target_student_id)
                if prev is None or snap.submitted_at > prev.submitted_at:
                    latest[snap.target_student_id] = snap
            return list(latest.values())


def _doc_to_snapshot(name: str, fields: dict, period: Period) -> SubmissionSnapshot | None:
    def _as_int(key: str) -> int:
        v = fields.get(key)
        if not v:
            return 0
        return int(v.get("integerValue", 0))

    def _as_str(key: str) -> str:
        v = fields.get(key)
        if not v:
            return ""
        return v.get("stringValue", "")

    def _as_ts(key: str) -> datetime:
        v = fields.get(key)
        if not v:
            return datetime.now(timezone.utc)
        raw = v.get("timestampValue", "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)

    target = _as_str("targetId")
    if not target:
        return None
    return SubmissionSnapshot(
        target_student_id=target,
        period=period,
        scores=ScoreCard(
            topic=_as_int("score_topic"),
            content=_as_int("score_content"),
            narrative=_as_int("score_narrative"),
            presentation=_as_int("score_presentation"),
            teamwork=_as_int("score_teamwork"),
        ),
        comment=_as_str("comment"),
        self_note="",
        submitted_at=_as_ts("submittedAt"),
        external_id=name,
        source="site2",
    )
