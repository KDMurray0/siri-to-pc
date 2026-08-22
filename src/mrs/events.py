"""Event bus. Worker threads publish, SSE clients subscribe."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from .logging_setup import get

log = get("events")


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        # Last value per event type, so a fresh subscriber can be caught up.
        self._latest: dict[str, Any] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subs.add(q)
        for kind, payload in list(self._latest.items()):
            try:
                q.put_nowait({"type": kind, "data": payload, "replay": True})
            except asyncio.QueueFull:
                break
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, kind: str, data: Any = None, *, sticky: bool = True) -> None:
        """Fan an event out to every subscriber. Safe from any thread."""
        if sticky:
            self._latest[kind] = data
        evt = {"type": kind, "data": data, "ts": time.time()}
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, evt)
        except RuntimeError:
            pass

    def _fanout(self, evt: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # A stalled client shouldn't wedge the bus; drop its backlog.
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass

    def latest(self, kind: str, default: Any = None) -> Any:
        return self._latest.get(kind, default)


bus = EventBus()


# Event names, kept in one place so the UI and server can't drift.
class Ev:
    STATUS = "status"          # now-playing + transport
    QUEUE = "queue"            # queue contents
    ACTIVITY = "activity"      # finding / downloading / loading / idle
    SETTINGS = "settings"      # settings changed
    TOAST = "toast"            # user-facing message
    COOKIES = "cookies"        # cookie health
    LEVEL = "level"            # audio level for the visualiser
    LIBRARY = "library"        # local library scan progress
