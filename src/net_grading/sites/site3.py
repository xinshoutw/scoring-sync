"""Site3 (Google Apps Script) client — 純寫入鏡像。"""
import httpx

from net_grading.config import get_settings
from net_grading.sites.base import Period, ScoreCard, SubmitResult
from net_grading.sites.errors import SiteNotSupported, SiteTransportError


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class Site3Client:
    def __init__(self, timeout: float = 15.0) -> None:
        self._url = get_settings().site3_apps_script_url
        self._timeout = timeout

    async def submit(
        self,
        grader_id: str,
        grader_name: str,
        period: Period,
        target_id: str,
        target_name: str,
        scores: ScoreCard,
        comment: str,
    ) -> SubmitResult:
        form = {
            "sheetType": period,  # midterm / final
            "Name": grader_name,
            "Id_number": grader_id,
            "Presenter": target_name,
            "topicMastery": str(scores.topic),
            "contentRichness": str(scores.content),
            "narrativeSkills": str(scores.narrative),
            "presentationSkills": str(scores.presentation),
            "teamwork": str(scores.teamwork),
            "Message": comment,
        }
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"user-agent": USER_AGENT},
            follow_redirects=True,
        ) as c:
            try:
                r = await c.post(self._url, data=form)
            except httpx.HTTPError as exc:
                raise SiteTransportError(str(exc)) from exc
            if r.status_code >= 400:
                raise SiteTransportError(
                    f"apps_script_failed_{r.status_code}:{r.text[:200]}"
                )
            try:
                body = r.json()
            except Exception:
                raise SiteTransportError(f"non_json_response:{r.text[:200]}")
            if body.get("result") != "success":
                raise SiteTransportError(f"apps_script_non_success:{body}")
            return SubmitResult(external_id=str(body.get("row")), raw_response=r.text[:4000])

    async def list_submissions(self, *args, **kwargs):
        raise SiteNotSupported("Site3 Apps Script 僅支援寫入")
