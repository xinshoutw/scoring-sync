"""Dashboard 背景 recheck 節流閥：per-user in-flight lock + 30s 最小間隔。"""
import logging
import time

from net_grading.db.engine import get_session_factory
from net_grading.sites.base import Period
from net_grading.sync.orchestrator import fire_and_forget
from net_grading.sync.pull import recheck_conflicts


log = logging.getLogger(__name__)

_in_flight: set[str] = set()
_last_run: dict[str, float] = {}
MIN_INTERVAL_SEC = 30.0


def schedule_recheck(user_id: str, period: Period, site1_sid: str) -> str:
    """呼叫後非阻塞；回傳狀態字串供診斷。"""
    now = time.monotonic()
    if user_id in _in_flight:
        return "in_flight"
    last = _last_run.get(user_id, 0.0)
    if now - last < MIN_INTERVAL_SEC:
        return "throttled"
    _last_run[user_id] = now
    _in_flight.add(user_id)
    fire_and_forget(_run(user_id, period, site1_sid))
    return "scheduled"


async def _run(user_id: str, period: Period, site1_sid: str) -> None:
    try:
        async with get_session_factory()() as db:
            result = await recheck_conflicts(db, user_id, period, site1_sid)
            log.info("recheck %s %s: %s", user_id, period, result)
    except Exception as exc:  # pragma: no cover
        log.warning("recheck failed for %s: %s", user_id, exc)
    finally:
        _in_flight.discard(user_id)
