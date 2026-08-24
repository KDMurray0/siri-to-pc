"""Roughly when an artist is from, out of MusicBrainz. No key.

Tags say what a song sounds like but never when it's from, and country has
meant something different every decade for seventy years — ask for Jolene and
tags happily hand back Morgan Wallen, because both are country and nothing in
the data disagrees.

Three other sources were tried for this and all three were wrong:

    Last.fm decade tags   too sparse. Kenny Rogers (1978) and Luke Combs
                          (2023) both carry none, so absence says nothing.
    Deezer release_date   the release in *their* catalogue. The Gambler
                          comes back 2007. It files a 1978 record as modern.
    MusicBrainz recording the same problem — a recording search returns
                          whichever reissue it feels like. Jolene reads 1984
                          and Marquee Moon 1989.

What does work is the artist's own start. For a person it's their birth year
and for a band it's when they formed, which is inconsistent on paper and
perfectly good in practice, because only the gap between two of them matters:
Merle Haggard 1937 against Morgan Wallen 1993 is the distance we're looking
for, and Kenny Rogers 1938 against Merle Haggard 1937 is not.

One request a second, which is MusicBrainz's limit, so this fills in behind
you. Ranking never waits for it.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..models import Track
from ..paths import data_dir

log = get("era")

API = "https://musicbrainz.org/ws/2/artist"
# They ask for a real one that identifies the app, and hand out 503s otherwise.
UA = {"User-Agent": "MusicRequestServer/3.0 (personal LAN music player)"}
PACE = 1.1             # their published limit is one a second
MAX_ENTRIES = 3000


class EraStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._year: dict[str, int] = {}      # artist -> year, 0 means "asked, nothing"
        self._queued: set[str] = set()
        self._jobs: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._loaded = False

    # -- disk ----------------------------------------------------------
    def _file(self):
        return data_dir() / "eras.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
            with self._lock:
                self._year = {k: int(v) for k, v in raw.items()
                              if isinstance(v, (int, float))}
            log.info("%d artist eras cached", len(self._year))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("era cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                items = dict(list(self._year.items())[-MAX_ENTRIES:])
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps(items), encoding="utf-8")
            tmp.replace(self._file())
        except Exception as exc:
            log.debug("couldn't save eras: %s", exc)

    # -- lookup --------------------------------------------------------
    def get(self, track: Track | None) -> int | None:
        """The artist's start year, or None until we've looked."""
        if not track:
            return None
        who = track.primary_artist()
        if not who:
            return None
        self.load()
        with self._lock:
            if who in self._year:
                got = self._year[who]
                return got if got > 0 else None
            if who in self._queued:
                return None
            self._queued.add(who)
        self._jobs.put(who)
        self._start()
        return None

    def prime(self, track: Track | None) -> int | None:
        """Look one up now. Worth it for the song that was actually asked for,
        because the whole comparison is against that one."""
        if not track:
            return None
        who = track.primary_artist()
        if not who:
            return None
        self.load()
        with self._lock:
            if who in self._year:
                got = self._year[who]
                return got if got > 0 else None
        try:
            year = self._fetch(who)
        except Exception as exc:
            # never let a lookup take a request down with it
            log.debug("era lookup failed for %s: %s", who, exc)
            return None
        with self._lock:
            self._year[who] = year or 0
            self._queued.discard(who)
        self.save()
        return year

    def _start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="era")
            self._worker.start()

    def _run(self) -> None:
        dirty = 0
        while True:
            try:
                who = self._jobs.get(timeout=30)
            except queue.Empty:
                break
            year = 0
            try:
                year = self._fetch(who) or 0
            except Exception as exc:
                log.debug("era lookup failed for %s: %s", who, exc)
            with self._lock:
                self._year[who] = year
                self._queued.discard(who)
            dirty += 1
            if dirty >= 10:
                self.save()
                dirty = 0
            time.sleep(PACE)
        if dirty:
            self.save()
        with self._lock:
            self._worker = None

    def _fetch(self, who: str) -> int | None:
        q = urllib.parse.quote(f'artist:"{who}"')
        req = urllib.request.Request(f"{API}?query={q}&fmt=json&limit=1",
                                     headers=UA)
        # 503 is how MusicBrainz says "slow down", so slowing down is the
        # answer rather than giving up on the artist.
        for wait in (0, 2.0, 5.0):
            if wait:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                if e.code != 503 or wait == 5.0:
                    raise
        else:
            return None
        rows = data.get("artists") or []
        if not rows:
            return None
        # a weak name match is somebody else's birthday, which is worse than
        # not knowing
        if int(rows[0].get("score") or 0) < 90:
            return None
        began = ((rows[0].get("life-span") or {}).get("begin") or "")[:4]
        return int(began) if began.isdigit() else None

    def warm(self, tracks: list[Track]) -> None:
        for t in tracks[:40]:
            self.get(t)


era = EraStore()


def gap(a: int | None, b: int | None) -> float:
    """0 to 1 on how far apart two artists are in time, 0 when we can't tell.

    Nothing below twenty years counts at all — a band that formed in 1969 and
    one that formed in 1974 are the same era and shouldn't be nudged apart.
    """
    if not a or not b:
        return 0.0
    years = abs(a - b)
    return min(1.0, max(0.0, (years - 20) / 40.0))
