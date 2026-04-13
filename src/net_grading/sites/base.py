"""共用型別：三站 adapter 都用這組結構回傳，讓上層商業邏輯不用知道協議差異."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


Period = Literal["midterm", "final"]
SiteName = Literal["site1", "site2", "site3"]


@dataclass(frozen=True)
class PeriodInfo:
    code: str
    label: str
    is_open: bool


@dataclass(frozen=True)
class StudentIdentity:
    """Site1 identify 的回傳或 Site2 login 後的身份."""

    actor_id: str
    name: str
    class_name: str
    periods: tuple[PeriodInfo, ...]


@dataclass(frozen=True)
class Target:
    student_id: str
    name: str
    class_name: str
    evaluated: bool
    total: int | None


@dataclass
class ScoreCard:
    topic: int
    content: int
    narrative: int
    presentation: int
    teamwork: int

    @property
    def total(self) -> int:
        return (
            self.topic
            + self.content
            + self.narrative
            + self.presentation
            + self.teamwork
        )


@dataclass(frozen=True)
class SubmissionSnapshot:
    """站台回傳的單筆評分（跨站統一表示）."""

    target_student_id: str
    period: Period
    scores: ScoreCard
    comment: str
    self_note: str
    submitted_at: datetime
    external_id: str | None = None
    source: SiteName | None = None


@dataclass(frozen=True)
class SubmitResult:
    external_id: str | None
    raw_response: str
