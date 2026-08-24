"""One queue per outside service, so everything waits its turn.

Each store used to pace itself, and only in its background worker: the tag
worker slept 0.2s between jobs, the era worker 1.1s, the kin worker 0.35s.
Which left two holes.

The blocking lookups didn't pace at all. Priming a refill asks MusicBrainz
for the anchor's era and then up to eight more, one after another with
nothing in between, against a service that asks for one request a second —
and 503s were exactly what came back.

And nothing knew about anything else. Four services, four schedules, no idea
that a refill fires all of them at once.

So the wait moved down to the call itself. A caller reserves the next slot
for that service and sleeps until it comes round, which means the background
worker and a blocking prime queue up together in the order they arrive.
"""

from __future__ import annotations

import threading
import time

# Seconds between calls. What each service asks for, mostly:
# Last.fm says five a second, MusicBrainz says one, Deezer doesn't publish a
# number and is happy at three. YouTube is unofficial, so it gets a light
# pace on top of the circuit breaker that already watches it.
RATES = {
    "lastfm": 0.2,
    "musicbrainz": 1.1,
    "deezer": 0.35,
    "youtube": 0.1,
}


class Gate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._served: dict[str, int] = {}
        self._waited: dict[str, float] = {}

    def wait(self, service: str) -> None:
        """Block until it's this caller's turn to talk to `service`."""
        rate = RATES.get(service)
        if not rate:
            return
        with self._lock:
            now = time.monotonic()
            due = max(now, self._next.get(service, 0.0))
            # Reserve the slot here, sleep outside: two callers arriving
            # together get consecutive slots instead of the same one, and
            # nothing sleeps while holding the lock.
            self._next[service] = due + rate
            self._served[service] = self._served.get(service, 0) + 1
        delay = due - time.monotonic()
        if delay > 0:
            with self._lock:
                self._waited[service] = self._waited.get(service, 0.0) + delay
            time.sleep(delay)

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {s: {"calls": n,
                        "waited_s": round(self._waited.get(s, 0.0), 1),
                        "queue_s": round(max(0.0, self._next.get(s, 0.0) - now), 1)}
                    for s, n in sorted(self._served.items())}


gate = Gate()
