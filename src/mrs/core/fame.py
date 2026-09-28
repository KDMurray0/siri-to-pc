"""How many people listen to an act: Last.fm's listener count, cached.

The radio leans towards records people actually play. A shared genre label
let a death-metal track into a country queue and a stranger's upload in beside
the hits; a band with a few hundred listeners is what those look like. Known
acts get a lift, near-unknown ones a real penalty, and anything not yet looked
up is fetched behind the scenes, like the tags.
"""

from __future__ import annotations

import json
import math
import queue
import threading

from ..config import config
from ..logging_setup import get
from ..models import Track
from ..paths import data_dir, write_atomic

log = get("fame")

WEIGHT = 1.3            # at most this much either way
_MID = 4.7              # log10 listeners that counts as neither: ~50,000
_SPAN = 1.8             # ~3 million is a full lift; ~800 is a full penalty
MAX_ENTRIES = 6000


class Fame:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._known: dict[str, int] = {}
        self._queued: set[str] = set()
        self._loaded = False
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _key(track: Track | None) -> str:
        return track.primary_artist() if track else ""

    def enabled(self) -> bool:
        return bool(config.get("lastfm_api_key") and config.get("use_tags", True))

    def _file(self):
        return data_dir() / "fame.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            got = json.loads(self._file().read_text("utf-8"))
            if isinstance(got, dict):
                self._known.update({k: int(v) for k, v in got.items()})
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            snap = dict(list(self._known.items())[-MAX_ENTRIES:])
        try:
            write_atomic(self._file(), json.dumps(snap))
        except Exception as exc:
            log.debug("couldn't save listener counts: %s", exc)

    def listeners(self, track: Track | None) -> int | None:
        """Known count, or None -- in which case it's looked up for next time."""
        if not self.enabled():
            return None
        key = self._key(track)
        if not key:
            return None
        self.load()
        with self._lock:
            if key in self._known:
                return self._known[key]
            if key in self._queued or len(self._queued) > 400:
                return None
            self._queued.add(key)
        self._q.put((key, (track.artist or "").split(",")[0].strip()))
        if not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._worker, daemon=True, name="fame")
            self._thread.start()
        return None

    def boost(self, track: Track | None) -> float:
        """How much being well known (or not) is worth to this track's score."""
        n = self.listeners(track)
        if n is None:
            return 0.0
        if n <= 0:
            return -WEIGHT
        x = (math.log10(n) - _MID) / _SPAN
        return WEIGHT * max(-1.2, min(1.0, x))

    def _worker(self) -> None:
        from .tags import tagstore
        dirty = 0
        while True:
            try:
                key, name = self._q.get(timeout=20)
            except queue.Empty:
                break
            try:
                info = tagstore._call({"method": "artist.getInfo", "artist": name})
                n = int(((info.get("artist") or {}).get("stats") or {}).get("listeners") or 0)
                with self._lock:
                    self._known[key] = n
                    self._queued.discard(key)
                dirty += 1
            except Exception as exc:
                log.debug("listeners for %s: %s", name, exc)
                with self._lock:
                    self._queued.discard(key)
            if dirty >= 25:
                self.save()
                dirty = 0
        if dirty:
            self.save()


fame = Fame()
