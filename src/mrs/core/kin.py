"""Who belongs next to whom, from Deezer. No key.

Last.fm's affinity answers this at track level and answers it well, but it's
sparse: most pairs come back zero because neither track is in the other's
neighbour list, so it only ever helps the handful it knows about. Deezer keeps
a related-artists list for everybody, which is coarser but always there.

It's also era-aware for free, which nothing else keyless managed:

    Dolly Parton -> Kris Kristofferson, Willie Nelson, Loretta Lynn
    Television   -> Wire, The Clash, The Stranglers, Talking Heads, Devo
    Ayub Ogada   -> Seckou Keita, Geoffrey Oryema, Boubacar Traore

Two requests per artist, cached to disk and filled in behind you.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..models import Track, _fold
from ..paths import data_dir

log = get("kin")

API = "https://api.deezer.com"
UA = {"User-Agent": "MusicRequestServer/3.0"}
PACE = 0.35
MAX_ENTRIES = 1500


def _key(name: str) -> str:
    return _fold((name or "").strip().lower())


class KinStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._near: dict[str, list[str]] = {}   # artist -> related artists
        self._queued: set[str] = set()
        self._jobs: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._loaded = False

    def _file(self):
        return data_dir() / "kin.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
            with self._lock:
                self._near = {k: list(v) for k, v in raw.items()
                              if isinstance(v, list)}
            log.info("%d artists with neighbours cached", len(self._near))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("kin cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                items = dict(list(self._near.items())[-MAX_ENTRIES:])
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps(items), encoding="utf-8")
            tmp.replace(self._file())
        except Exception as exc:
            log.debug("couldn't save kin: %s", exc)

    def related(self, track: Track | None) -> list[str]:
        """Neighbours we already hold. Anything unknown is queued."""
        if not track:
            return []
        who = _key(track.primary_artist())
        if not who:
            return []
        self.load()
        with self._lock:
            if who in self._near:
                return self._near[who]
            if who in self._queued:
                return []
            self._queued.add(who)
        self._jobs.put(who)
        self._start()
        return []

    def prime(self, track: Track | None) -> list[str]:
        """Fetch now. Worth it for the anchor, since every comparison is
        against that one artist's neighbours."""
        if not track:
            return []
        who = _key(track.primary_artist())
        if not who:
            return []
        self.load()
        with self._lock:
            if who in self._near:
                return self._near[who]
        try:
            found = self._fetch(who)
        except Exception as exc:
            log.debug("kin lookup failed for %s: %s", who, exc)
            return []
        with self._lock:
            self._near[who] = found
            self._queued.discard(who)
        self.save()
        return found

    def is_kin(self, anchor_related: list[str], track: Track | None) -> bool:
        return bool(anchor_related and track
                    and _key(track.primary_artist()) in anchor_related)

    def _start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="kin")
            self._worker.start()

    def _run(self) -> None:
        dirty = 0
        while True:
            try:
                who = self._jobs.get(timeout=30)
            except queue.Empty:
                break
            found: list[str] = []
            try:
                found = self._fetch(who)
            except Exception as exc:
                log.debug("kin lookup failed for %s: %s", who, exc)
            with self._lock:
                self._near[who] = found
                self._queued.discard(who)
            dirty += 1
            if dirty >= 15:
                self.save()
                dirty = 0
            time.sleep(PACE)
        if dirty:
            self.save()
        with self._lock:
            self._worker = None

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(API + path, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _fetch(self, who: str) -> list[str]:
        found = self._get("/search/artist?limit=1&q=" + urllib.parse.quote(who))
        rows = found.get("data") or []
        if not rows or not rows[0].get("id"):
            return []
        # A loose name match is somebody else's neighbours, which is worse
        # than having none.
        if _key(rows[0].get("name", "")) != who:
            return []
        rel = self._get(f"/artist/{rows[0]['id']}/related?limit=12")
        return [_key(a.get("name", "")) for a in (rel.get("data") or [])
                if a.get("name")]

    def warm(self, tracks: list[Track]) -> None:
        seen = set()
        for t in tracks[:40]:
            who = _key(t.primary_artist())
            if who and who not in seen:
                seen.add(who)
                self.related(t)


kin = KinStore()
