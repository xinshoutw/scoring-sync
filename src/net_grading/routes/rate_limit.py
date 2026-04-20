"""Per-user sliding window rate limiter（in-memory，單 worker 有效）。

風格對齊 `sync/recheck.py`：module-level dict + ``time.monotonic()``、不帶依賴。
若未來切多 worker，再換成 Redis-backed 即可，不影響呼叫端介面。
"""
from collections import deque
from collections.abc import MutableMapping
from typing import Final
import time

from fastapi import Depends, HTTPException, status

from net_grading.auth.middleware import require_user
from net_grading.auth.session import CurrentUser


SUBMIT_WINDOW_SEC: Final[float] = 10.0
SUBMIT_MAX_REQUESTS: Final[int] = 3

_submit_hits: MutableMapping[str, deque[float]] = {}


def _check_and_record(
    bucket_key: str,
    *,
    window_sec: float,
    max_requests: int,
    store: MutableMapping[str, deque[float]],
) -> float | None:
    """滑動窗口：允許則 append 並回 None；被擋則回 retry_after（秒，>0）。"""
    now = time.monotonic()
    cutoff = now - window_sec
    bucket = store.get(bucket_key)
    if bucket is None:
        bucket = deque()
        store[bucket_key] = bucket
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= max_requests:
        retry_after = bucket[0] + window_sec - now
        # 地板 0.1s，避免 Retry-After: 0 造成 client 立即重打
        return max(retry_after, 0.1)
    bucket.append(now)
    return None


def throttle_submit(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    """FastAPI dependency：每位使用者 10 秒內最多 3 次送出；超過回 429。

    ``detail='rate_limited_submit'`` 由 app.py exception handler 轉成友善 HTML，
    並帶上 ``Retry-After`` 秒數。
    """
    retry_after = _check_and_record(
        user.user_id,
        window_sec=SUBMIT_WINDOW_SEC,
        max_requests=SUBMIT_MAX_REQUESTS,
        store=_submit_hits,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited_submit",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    return user


__all__ = [
    "SUBMIT_MAX_REQUESTS",
    "SUBMIT_WINDOW_SEC",
    "throttle_submit",
]
