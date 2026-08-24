"""How fast a song is, from Deezer's public API. No key.

Cached to disk and looked up in the background; callers get None until it
lands and carry on regardless.

Tempo only decides how long a crossfade runs. It does not touch what gets
queued — that picks songs the way it always has.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..models import Track
from ..paths import data_dir, write_atomic

log = get("tempo")

API = "https://api.deezer.com"
UA = {"User-Agent": "MusicRequestServer/2.0"}
PACE = 0.25           # Deezer allows ~50 calls per 5s; nowhere near it
MAX_ENTRIES = 3000


def _key(track: Track | None) -> str:
    if not track:
        return ""
    t = (track.title or "").strip().lower()
    return f"{track.primary_artist()}|{t}" if t else ""


class TempoStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bpm: dict[str, float] = {}      # key -> bpm, 0.0 means "asked, unknown"
        self._queued: set[str] = set()
        self._jobs: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._loaded = False

    # -- disk ----------------------------------------------------------
    def _file(self):
        return data_dir() / "tempo.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
            with self._lock:
                self._bpm = {k: float(v) for k, v in raw.items()}
            log.info("%d tempos cached", len(self._bpm))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("tempo cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                data = dict(list(self._bpm.items())[-MAX_ENTRIES:])
            write_atomic(self._file(), json.dumps(data))
        except Exception as exc:
            log.debug("couldn't save tempos: %s", exc)

    # -- lookup --------------------------------------------------------
    def get(self, track: Track | None) -> float | None:
        """Known bpm, or None. Never blocks; unknowns are fetched behind you."""
        k = _key(track)
        if not k:
            return None
        self.load()
        with self._lock:
            if k in self._bpm:
                bpm = self._bpm[k]
                return bpm if bpm > 0 else None
            if k in self._queued:
                return None
            self._queued.add(k)
        self._jobs.put((k, track.primary_artist(), track.title or ""))
        self._start()
        return None

    def _start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="tempo")
            self._worker.start()

    def _run(self) -> None:
        dirty = 0
        while True:
            try:
                key, artist, title = self._jobs.get(timeout=20)
            except queue.Empty:
                break
            bpm = 0.0
            try:
                bpm = self._fetch(artist, title)
            except Exception as exc:
                log.debug("tempo lookup failed for %s: %s", key, exc)
            with self._lock:
                self._bpm[key] = bpm
                self._queued.discard(key)
            dirty += 1
            if dirty >= 15:
                self.save()
                dirty = 0
            time.sleep(PACE)
        if dirty:
            self.save()
        with self._lock:
            self._worker = None

    def _call(self, path: str) -> dict:
        req = urllib.request.Request(API + path, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _fetch(self, artist: str, title: str) -> float:
        if not title:
            return 0.0
        q = f'artist:"{artist}" track:"{title}"' if artist else title
        found = self._call("/search?" + urllib.parse.urlencode({"q": q, "limit": 1}))
        rows = found.get("data") or []
        if not rows:
            return 0.0
        full = self._call(f"/track/{rows[0]['id']}")
        try:
            return float(full.get("bpm") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def warm(self, tracks: list[Track]) -> None:
        for t in tracks[:12]:
            self.get(t)


tempo = TempoStore()


def blend_seconds(base: int, outgoing: Track | None, incoming: Track | None) -> int:
    """How long to crossfade these two, given the tempo change.

    Two tracks at a similar pace can overlap for a while and sound deliberate.
    A slow song into a fast one can't — the two rhythms fight, so get it over
    with. Falls back to exactly what you configured when either bpm is unknown,
    which is most of the time until the cache fills.
    """
    if base <= 0:
        return base
    a, b = tempo.get(outgoing), tempo.get(incoming)
    if not a or not b:
        return base
    gap = abs(a - b)
    if gap <= 8:
        return min(base + 2, 12)      # near enough the same pulse
    if gap >= 35:
        return max(1, base // 2)      # very different; don't dwell on the clash
    return base
