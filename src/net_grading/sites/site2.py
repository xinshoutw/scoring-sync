"""Site2 (ntust-grading, stanleyowen/ntust-grading) Firebase client。

Schema（對齊其 src/lib/types.ts `GradeSubmission`）：
    stage, graderId, graderName, targetId, targetName,
    scores: {topicMastery, contentRichness, narrativeSkill, presentationSkill, teamwork},
    total, comment, submittedAt

Security rule：grades.create 需 auth + isStageOpen(request.resource.data.stage)；
grades.read 只給 admin，因此 list_submissions 對學生會 403，呼叫端要容忍。

寫入採 upsert：query existing（graderId+targetId+stage），有則 PATCH、無則 POST。
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


def _v_str(s: str) -> dict[str, Any]:
    return {"stringValue": s}


def _v_int(n: int) -> dict[str, Any]:
    return {"integerValue": str(n)}


def _v_ts(dt: datetime) -> dict[str, Any]:
    iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"timestampValue": iso}


def _v_map(d: dict[str, Any]) -> dict[str, Any]:
    return {"mapValue": {"fields": d}}


def _build_grade_fields(
    stage: Period,
    grader_id: str,
    grader_name: str,
    target_id: str,
    target_name: str,
    scores: ScoreCard,
    comment: str,
) -> dict[str, Any]:
    return {
        "stage": _v_str(stage),
        "graderId": _v_str(grader_id),
        "graderName": _v_str(grader_name),
        "targetId": _v_str(target_id),
        "targetName": _v_str(target_name),
        "scores": _v_map(
            {
                "topicMastery": _v_int(scores.topic),
                "contentRichness": _v_int(scores.content),
                "narrativeSkill": _v_int(scores.narrative),
                "presentationSkill": _v_int(scores.presentation),
                "teamwork": _v_int(scores.teamwork),
            }
        ),
        "total": _v_int(scores.total),
        "comment": _v_str(comment),
        "submittedAt": _v_ts(datetime.now(timezone.utc)),
    }


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

    async def _find_existing_grade_doc_id(
        self,
        id_token: str,
        grader_id: str,
        target_id: str,
        stage: Period,
    ) -> str | None:
        """查既有 grades doc；成功回 doc id；rule 可能擋讀，讀失敗就當作無."""
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
                            {"fieldFilter": {"field": {"fieldPath": "graderId"}, "op": "EQUAL", "value": _v_str(grader_id)}},
                            {"fieldFilter": {"field": {"fieldPath": "targetId"}, "op": "EQUAL", "value": _v_str(target_id)}},
                            {"fieldFilter": {"field": {"fieldPath": "stage"}, "op": "EQUAL", "value": _v_str(stage)}},
                        ],
                    }
                },
                "limit": 1,
            }
        }
        async with self._client() as c:
            r = await c.post(url, headers={"authorization": f"Bearer {id_token}"}, json=query)
            if r.status_code != 200:
                return None  # 權限被擋或其他錯誤 → 當作沒有現有紀錄，走 create
            rows = r.json()
            for row in rows:
                doc = row.get("document")
                if doc:
                    name = doc.get("name", "")
                    return name.rsplit("/", 1)[-1] if name else None
            return None

    async def submit(
        self,
        id_token: str,
        grader_id: str,
        grader_name: str,
        period: Period,
        target_id: str,
        target_name: str,
        scores: ScoreCard,
        comment: str,
    ) -> SubmitResult:
        fields = _build_grade_fields(
            period, grader_id, grader_name, target_id, target_name, scores, comment
        )

        existing_id = await self._find_existing_grade_doc_id(
            id_token, grader_id, target_id, period
        )

        async with self._client() as c:
            if existing_id:
                # PATCH：覆寫指定欄位
                url = (
                    f"{FIRESTORE_BASE}/v1/projects/{self._project}"
                    f"/databases/(default)/documents/grades/{existing_id}"
                )
                params = [
                    ("updateMask.fieldPaths", k) for k in fields.keys()
                ]
                try:
                    r = await c.patch(
                        url,
                        headers={"authorization": f"Bearer {id_token}"},
                        params=params,
                        json={"fields": fields},
                    )
                except httpx.HTTPError as exc:
                    raise SiteTransportError(str(exc)) from exc
            else:
                # 新建
                url = (
                    f"{FIRESTORE_BASE}/v1/projects/{self._project}"
                    f"/databases/(default)/documents/grades"
                )
                try:
                    r = await c.post(
                        url,
                        headers={"authorization": f"Bearer {id_token}"},
                        json={"fields": fields},
                    )
                except httpx.HTTPError as exc:
                    raise SiteTransportError(str(exc)) from exc

            if r.status_code == 401:
                raise SiteTokenExpired("id_token_expired")
            if r.status_code >= 400:
                raise SiteTransportError(
                    f"firestore_write_failed_{r.status_code}:{r.text[:300]}"
                )
            body = r.json()
            doc_name = body.get("name", existing_id or "")
            return SubmitResult(external_id=doc_name, raw_response=r.text[:4000])

    async def list_submissions(
        self, id_token: str, grader_id: str, period: Period
    ) -> list[SubmissionSnapshot]:
        """Site2 security rule 只允許 admin read grades；學生會拿 403 空集合。"""
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
                            {"fieldFilter": {"field": {"fieldPath": "graderId"}, "op": "EQUAL", "value": _v_str(grader_id)}},
                            {"fieldFilter": {"field": {"fieldPath": "stage"}, "op": "EQUAL", "value": _v_str(period)}},
                        ],
                    }
                },
            }
        }
        async with self._client() as c:
            r = await c.post(url, headers={"authorization": f"Bearer {id_token}"}, json=query)
            if r.status_code == 401:
                raise SiteTokenExpired("id_token_expired")
            if r.status_code in (403, 404):
                return []  # 被 security rule 擋 → 視同沒有資料
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
                snap = _doc_to_snapshot(doc.get("name", ""), doc.get("fields", {}), period)
                if snap is None:
                    continue
                prev = latest.get(snap.target_student_id)
                if prev is None or snap.submitted_at > prev.submitted_at:
                    latest[snap.target_student_id] = snap
            return list(latest.values())


def _doc_to_snapshot(
    name: str, fields: dict[str, Any], period: Period
) -> SubmissionSnapshot | None:
    def _as_str(key: str) -> str:
        return fields.get(key, {}).get("stringValue", "")

    def _as_ts(key: str) -> datetime:
        raw = fields.get(key, {}).get("timestampValue", "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)

    scores_map = fields.get("scores", {}).get("mapValue", {}).get("fields", {})

    def _score(k: str) -> int:
        v = scores_map.get(k, {})
        return int(v.get("integerValue", 0))

    target = _as_str("targetId")
    if not target:
        return None
    return SubmissionSnapshot(
        target_student_id=target,
        period=period,
        scores=ScoreCard(
            topic=_score("topicMastery"),
            content=_score("contentRichness"),
            narrative=_score("narrativeSkill"),
            presentation=_score("presentationSkill"),
            teamwork=_score("teamwork"),
        ),
        comment=_as_str("comment"),
        self_note="",
        submitted_at=_as_ts("submittedAt"),
        external_id=name,
        source="site2",
    )
