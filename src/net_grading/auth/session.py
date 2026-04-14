"""本站 session CRUD：加密 Site1 sid、配本站 session token."""
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.crypto import decrypt, encrypt
from net_grading.db.models import Session as DbSession, User, utcnow
from net_grading.sites.base import StudentIdentity
from net_grading.sites.site1 import Site1LoginResult


SESSION_COOKIE = "ng_session"


@dataclass(frozen=True)
class CurrentUser:
    session_id: str
    user_id: str
    name: str
    class_name: str
    site1_sid: str
    site1_sid_expires_at: datetime
    expires_at: datetime
    sync_site1: bool
    sync_site2: bool
    sync_site3: bool
    welcomed: bool

    def enabled_sites(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.sync_site1:
            out.append("site1")
        if self.sync_site2:
            out.append("site2")
        if self.sync_site3:
            out.append("site3")
        return tuple(out)


async def create_session(
    db: AsyncSession,
    login: Site1LoginResult,
) -> tuple[str, datetime]:
    """寫 users + sessions，回 (session_id, expires_at)."""
    identity = login.identity

    existing = await db.get(User, identity.actor_id)
    if existing is None:
        db.add(
            User(
                student_id=identity.actor_id,
                name=identity.name,
                class_name=identity.class_name,
                last_login_at=utcnow(),
            )
        )
    else:
        existing.name = identity.name
        existing.class_name = identity.class_name
        existing.last_login_at = utcnow()

    session_id = secrets.token_urlsafe(32)
    row = DbSession(
        id=session_id,
        user_id=identity.actor_id,
        site1_sid_enc=encrypt(login.sid),
        site1_sid_expires_at=login.sid_expires_at,
        expires_at=login.sid_expires_at,
    )
    db.add(row)
    await db.commit()
    return session_id, login.sid_expires_at


async def load_session(db: AsyncSession, session_id: str) -> CurrentUser | None:
    stmt = (
        select(DbSession, User)
        .join(User, DbSession.user_id == User.student_id)
        .where(DbSession.id == session_id)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return None
    s, user = row.tuple()
    now = utcnow()
    expires = _aware(s.expires_at)
    if expires <= now:
        await destroy_session(db, session_id)
        return None
    return CurrentUser(
        session_id=s.id,
        user_id=user.student_id,
        name=user.name,
        class_name=user.class_name,
        site1_sid=decrypt(s.site1_sid_enc),
        site1_sid_expires_at=_aware(s.site1_sid_expires_at),
        expires_at=expires,
        sync_site1=bool(user.sync_site1),
        sync_site2=bool(user.sync_site2),
        sync_site3=bool(user.sync_site3),
        welcomed=bool(user.welcomed),
    )


async def destroy_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(DbSession).where(DbSession.id == session_id))
    await db.commit()


def _aware(dt: datetime) -> datetime:
    """SQLite 存回來的 datetime 有時會失去 tzinfo，統一補 UTC."""
    if dt.tzinfo is None:
        from datetime import timezone

        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "SESSION_COOKIE",
    "CurrentUser",
    "StudentIdentity",  # re-export
    "create_session",
    "destroy_session",
    "load_session",
]
