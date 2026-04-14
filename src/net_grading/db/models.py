from datetime import datetime, timezone

from sqlalchemy import (
    BLOB,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    student_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sync_site1: Mapped[int] = mapped_column(Integer, default=1)
    sync_site2: Mapped[int] = mapped_column(Integer, default=1)
    sync_site3: Mapped[int] = mapped_column(Integer, default=1)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.student_id", ondelete="CASCADE"))
    site1_sid_enc: Mapped[bytes] = mapped_column(BLOB)
    site1_sid_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)


class Site2Credential(Base):
    __tablename__ = "site2_credentials"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.student_id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str] = mapped_column(String(255))
    enc_refresh_token: Mapped[bytes] = mapped_column(BLOB)
    id_token: Mapped[str] = mapped_column(Text)
    id_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    local_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Submission(Base):
    """Append-only：每次按「送出」就新增一列，`current` = latest by submitted_at."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.student_id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(16))
    target_student_id: Mapped[str] = mapped_column(String(32))

    score_topic: Mapped[int] = mapped_column(Integer)
    score_content: Mapped[int] = mapped_column(Integer)
    score_narrative: Mapped[int] = mapped_column(Integer)
    score_presentation: Mapped[int] = mapped_column(Integer)
    score_teamwork: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)

    comment: Mapped[str] = mapped_column(Text, default="")
    self_note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sync_logs: Mapped[list["SyncLog"]] = relationship(back_populates="submission", cascade="all")

    __table_args__ = (
        CheckConstraint("score_topic BETWEEN 0 AND 30", name="ck_score_topic"),
        CheckConstraint("score_content BETWEEN 0 AND 30", name="ck_score_content"),
        CheckConstraint("score_narrative BETWEEN 0 AND 20", name="ck_score_narrative"),
        CheckConstraint("score_presentation BETWEEN 0 AND 10", name="ck_score_presentation"),
        CheckConstraint("score_teamwork BETWEEN 0 AND 10", name="ck_score_teamwork"),
        CheckConstraint("period IN ('midterm','final')", name="ck_period"),
        CheckConstraint(
            "source IN ('local','imported_site1','imported_site2')", name="ck_source"
        ),
        Index(
            "ix_submissions_lookup",
            "user_id",
            "period",
            "target_student_id",
            "submitted_at",
        ),
    )


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE")
    )
    site: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="sync_logs")

    __table_args__ = (
        CheckConstraint("site IN ('site1','site2','site3')", name="ck_site"),
        CheckConstraint(
            "status IN ('pending','success','failed','skipped')", name="ck_status"
        ),
        Index("ix_sync_logs_submission", "submission_id"),
        Index("ix_sync_logs_site_status", "site", "status"),
    )


class TargetCache(Base):
    __tablename__ = "targets_cache"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.student_id", ondelete="CASCADE"), primary_key=True
    )
    period: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_student_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(64))
    is_self: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConflictEvent(Base):
    __tablename__ = "conflict_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.student_id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(16))
    target_student_id: Mapped[str] = mapped_column(String(32))
    site1_snapshot: Mapped[str] = mapped_column(Text)
    site2_snapshot: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('site1','site2','skip')",
            name="ck_resolution",
        ),
        Index("ix_conflict_pending", "user_id", "resolution"),
    )
