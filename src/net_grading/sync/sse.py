"""每個 submission_id 一組 asyncio.Queue 的記憶體 pub/sub。

單 worker uvicorn 下有效。多 worker 要換 Redis pub/sub 或其他。
"""
import asyncio
from typing import Any


_SENTINEL = object()  # close 用


class SSEBus:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}

    def subscribe(self, sid: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subs.setdefault(sid, set()).add(q)
        return q

    def unsubscribe(self, sid: int, q: asyncio.Queue) -> None:
        bag = self._subs.get(sid)
        if bag is None:
            return
        bag.discard(q)
        if not bag:
            self._subs.pop(sid, None)

    def publish(self, sid: int, event: dict[str, Any]) -> None:
        for q in list(self._subs.get(sid, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 背壓：慢訂閱者的資料掉棄，下次 page load 會從 DB reconcile

    def close(self, sid: int) -> None:
        for q in list(self._subs.get(sid, ())):
            try:
                q.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                pass


bus = SSEBus()


def is_sentinel(event: Any) -> bool:
    return event is _SENTINEL
