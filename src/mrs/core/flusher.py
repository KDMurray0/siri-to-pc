"""Everything counted in memory reaches disk within seconds.

The counters and the taste stores write lazily -- a skip shouldn't rewrite a
file -- and each relied on the next event to push out the last one. When the
music stopped, the last half-minute stayed in memory until something else
happened, which could be the next day. Other things read these files (the
desktop stats card, anything on the home screen), so a quiet house has to be
an up-to-date one too.
"""

from __future__ import annotations

import threading
import time

from ..logging_setup import get

log = get("flusher")

EVERY = 10.0
_started = False


def tick() -> None:
    """Write whatever is waiting. Cheap when nothing is."""
    from . import stats
    from .profile import profiles
    from .taste import taste
    for name, fn in (("stats", stats.flush), ("taste", taste.flush)):
        try:
            fn()
        except Exception as exc:
            log.debug("couldn't flush %s: %s", name, exc)
    with profiles._lock:
        people = list(profiles._by_id.values())
    for who in people:
        try:
            who.taste.flush()
        except Exception as exc:
            log.debug("couldn't flush a profile: %s", exc)


def _loop() -> None:
    while True:
        time.sleep(EVERY)
        tick()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="flusher").start()
