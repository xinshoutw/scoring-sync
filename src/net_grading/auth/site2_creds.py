"""Site2 憑證的 DB 存取 + idToken 懶加載續期。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from net_grading.crypto import decrypt, encrypt
from net_grading.db.models import Site2Credential
from net_grading.sites.errors import SiteLoginError, SiteTokenExpired
from net_grading.sites.site2 import Site2Client, Site2LoginResult


REFRESH_AHEAD = timedelta(seconds=60)


@dataclass(frozen=True)
class Site2Status:
    email: str
    local_id: str
    id_token_valid_until: datetime


async def save_credentials(
    db: AsyncSession, user_id: str, login: Site2LoginResult
) -> None:
    stmt = (
        sqlite_insert(Site2Credential)
        .values(
            user_id=user_id,
            email=login.email,
            enc_refresh_token=encrypt(login.refresh_token),
            id_token=login.id_token,
            id_token_expires_at=login.id_token_expires_at,
            local_id=login.local_id,
        )
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "email": login.email,
                "enc_refresh_token": encrypt(login.refresh_token),
                "id_token": login.id_token,
                "id_token_expires_at": login.id_token_expires_at,
                "local_id": login.local_id,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


async def load_status(db: AsyncSession, user_id: str) -> Site2Status | None:
    cred = await db.get(Site2Credential, user_id)
    if cred is None:
        return None
    return Site2Status(
        email=cred.email,
        local_id=cred.local_id,
        id_token_valid_until=_aware(cred.id_token_expires_at),
    )


async def get_id_token(db: AsyncSession, user_id: str) -> str | None:
    """懶加載續期；回 None 表示此使用者沒綁 Site2."""
    cred = await db.get(Site2Credential, user_id)
    if cred is None:
        return None

    expires = _aware(cred.id_token_expires_at)
    if expires > datetime.now(timezone.utc) + REFRESH_AHEAD:
        return cred.id_token

    refresh_token = decrypt(cred.enc_refresh_token)
    try:
        refreshed = await Site2Client().refresh(refresh_token)
    except SiteTokenExpired:
        await revoke(db, user_id)
        return None

    cred.id_token = refreshed.id_token
    cred.id_token_expires_at = refreshed.id_token_expires_at
    cred.enc_refresh_token = encrypt(refreshed.refresh_token)
    await db.commit()
    return cred.id_token


async def revoke(db: AsyncSession, user_id: str) -> None:
    await db.execute(delete(Site2Credential).where(Site2Credential.user_id == user_id))
    await db.commit()


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
