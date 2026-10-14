"""A tiny publish/subscribe bus used to push state to the browser (SSE)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any


class RingLog:
    """Bounded in-memory log buffer shared with the UI."""

    def __init__(self, capacity: int = 800):
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, message: str, level: str = "info", source: str = "app", **extra: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "time": time.time(),
                "level": level,
                "source": source,
                "message": message,
                **extra,
            }
            self._items.append(item)
            return item

    def tail(self, limit: int = 200, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            items = [i for i in self._items if i["id"] > after]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class EventBus:
    """Fan-out of :class:`Event` objects to any number of subscribers.

    ``publish`` is safe to call from worker threads as well as from the event
    loop; subscribers are asyncio queues which are filled with
    ``call_soon_threadsafe``.
    """

    def __init__(self, history: int = 100):
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=history)

    # -- plumbing --------------------------------------------------------- #

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- publishing ------------------------------------------------------- #

    def publish(self, event_type: str, data: Any = None) -> None:
        event = {"type": event_type, "data": data, "time": time.time()}
        self._history.append(event)
        with self._lock:
            subscribers = list(self._subscribers)
        queue: asyncio.Queue | None = None
        for queue in subscribers:
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._offer, queue, event)
                else:
                    self._offer(queue, event)
            except RuntimeError:
                continue

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # A slow browser: drop the oldest message and keep going.
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def replay(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]


def sse_encode(event: dict[str, Any]) -> str:
    """Encode an event as a Server-Sent Events frame."""
    payload = json.dumps(event.get("data"), default=str)
    return f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
