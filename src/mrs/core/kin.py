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
import re
import threading
import time
import urllib.parse
import urllib.request
from functools import lru_cache

from ..logging_setup import get
from ..models import Track, _fold, _strip_article
from ..paths import data_dir

log = get("kin")

API = "https://api.deezer.com"
UA = {"User-Agent": "MusicRequestServer/3.0"}
PACE = 0.35
MAX_ENTRIES = 1500
VERSION = 2       # v1 stored keyed names, which can't be searched for


@lru_cache(maxsize=4096)
def _key(name: str) -> str:
    """Punctuation stripped as well as accents, so Guns N' Roses and Guns N
    Roses are one band however either side spells it."""
    return re.sub(r"[^a-z0-9]", "", _fold((name or "").strip().lower()))


# How a band's name gets extended on a credit. "Bob Marley" and "Bob Marley &
# The Wailers" are one act; "Bob Marley's Legend In Sax" is a covers record.
_JOINS = (" & ", " and ", " with ", " feat", " featuring", ";", " + ")


def _same(ours: str, theirs: str) -> bool:
    """Two spellings of one act."""
    a, b = (ours or "").strip().lower(), (theirs or "").strip().lower()
    if not a or not b:
        return False
    # article-insensitive, because primary_artist() drops it on our side only
    # and "The Chieftains" then failed to match The Chieftains
    ka = {_key(a), _key(_strip_article(a))}
    kb = {_key(b), _key(_strip_article(b))}
    if ka & kb:
        return True
    long, short = (b, a) if len(b) > len(a) else (a, b)
    return long.startswith(short) and long[len(short):].startswith(_JOINS)


def _billing(track: Track) -> str:
    """The artist as written, for searching with. primary_artist() folds the
    accents off and drops the article, which is right for a cache key and
    wrong for a query — Deezer knows Sigur Rós, not sigurros."""
    return (track.artist or "").split(",")[0].strip()


class KinStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._near: dict[str, list[str]] = {}   # artist -> related artists
        self._queued: set[str] = set()
        self._jobs: queue.Queue[str] = queue.Queue()   # display names
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
            # v1 held names already stripped to keys, which match fine but
            # can't be searched for. Drop it rather than carry two shapes.
            rows = raw.get("near") if raw.get("v") == VERSION else {}
            with self._lock:
                self._near = {k: list(v) for k, v in (rows or {}).items()
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
            tmp.write_text(json.dumps({"v": VERSION, "near": items}),
                           encoding="utf-8")
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
        self._jobs.put(_billing(track) or who)
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
            found = self._fetch(_billing(track) or who)
        except Exception as exc:
            log.debug("kin lookup failed for %s: %s", who, exc)
            return []
        with self._lock:
            self._near[who] = found
            self._queued.discard(who)
        self.save()
        return found

    def is_kin(self, anchor_related: list[str], track: Track | None) -> bool:
        """Both sides keyed, so how either spells the name doesn't matter."""
        if not anchor_related or not track:
            return False
        who = _key(track.primary_artist())
        return bool(who) and any(_key(n) == who for n in anchor_related)

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
                name = self._jobs.get(timeout=30)
            except queue.Empty:
                break
            who = _key(_strip_article(name.lower()))
            found: list[str] = []
            try:
                found = self._fetch(name)
            except Exception as exc:
                log.debug("kin lookup failed for %s: %s", name, exc)
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

    def _fetch(self, name: str) -> list[str]:
        found = self._get("/search/artist?limit=10&q=" + urllib.parse.quote(name))
        rows = [r for r in (found.get("data") or [])
                if r.get("id") and _same(name, r.get("name", ""))]
        if not rows:
            return []
        # Deezer carries duplicate entries and the search doesn't rank them:
        # Michael Jackson came back as a stub with 140 fans and no relations
        # at all, so the best-connected act in pop contributed nothing. Most
        # fans first — and if that one turns out to be a stub too, try the
        # next, which is how Godspeed You! Black Emperor got a list.
        rows.sort(key=lambda r: -int(r.get("nb_fan") or 0))
        for row in rows[:2]:
            rel = self._get(f"/artist/{row['id']}/related?limit=12")
            # Names as Deezer writes them, not keys: the lane searches these.
            names = [a["name"] for a in (rel.get("data") or []) if a.get("name")]
            if names:
                return names
        return []

    def warm(self, tracks: list[Track]) -> None:
        seen = set()
        for t in tracks[:40]:
            who = _key(t.primary_artist())
            if who and who not in seen:
                seen.add(who)
                self.related(t)


kin = KinStore()
