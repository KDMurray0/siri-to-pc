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
from ..paths import data_dir, write_atomic

log = get("era")

API = "https://musicbrainz.org/ws/2/artist"
# They ask for a real one that identifies the app, and hand out 503s otherwise.
UA = {"User-Agent": "MusicRequestServer/3.0 (personal LAN music player)"}
PACE = 1.1
BORN_TO_CAREER = 20   # roughly how long after birth a first record lands
VERSION = 2           # v1 cached birth years for people, career years for bands             # their published limit is one a second
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
            # v1 mixed birth years and formation years in one column, so the
            # numbers can't be compared with the ones we write now.
            rows = raw.get("years") if raw.get("v") == VERSION else {}
            with self._lock:
                self._year = {k: int(v) for k, v in (rows or {}).items()
                              if isinstance(v, int)}
            log.info("%d artist eras cached", len(self._year))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("era cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                items = dict(list(self._year.items())[-MAX_ENTRIES:])
            write_atomic(self._file(), json.dumps({"v": VERSION, "years": items}))
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

    def stats(self) -> dict:
        self.load()      # lazy, so a cold read would report zeroes
        with self._lock:
            known = sum(1 for v in self._year.values() if v)
            return {
                "artists": known,
                "nothing_known": len(self._year) - known,
                "queued": len(self._queued),
                "worker": bool(self._worker and self._worker.is_alive()),
            }

    def known(self, track: Track | None) -> bool:
        """Have we looked this artist up? Unlike get(), asking doesn't queue."""
        if not track:
            return True
        who = track.primary_artist()
        if not who:
            return True
        self.load()
        with self._lock:
            return who in self._year

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
            # Never let a lookup take a request down with it — but don't drop
            # it either. MusicBrainz hands out 503s freely, and one of them
            # landing on the anchor turned the era check off for a whole run:
            # nothing else asks for that artist, so nothing ever retried.
            log.debug("era lookup failed for %s: %s", who, exc)
            self._enqueue(who)
            return None
        with self._lock:
            self._year[who] = year or 0
            self._queued.discard(who)
        self.save()
        return year

    def _enqueue(self, who: str) -> None:
        with self._lock:
            if who in self._year or who in self._queued:
                return
            self._queued.add(who)
        self._jobs.put(who)
        self._start()

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
        if not began.isdigit():
            return None
        # For a group that date is when they formed; for a person it's when
        # they were born, and nobody releases a record at nought. Read raw,
        # the two aren't comparable and the signal came out backwards:
        # Michael Jackson (1958) sat 25 years off a-ha (1983) and got docked
        # for it, while Nicki Minaj (1982) was 24 years away and got waved
        # through. One of those belongs in a Billie Jean queue.
        return int(began) + (BORN_TO_CAREER
                             if rows[0].get("type") == "Person" else 0)

    def warm(self, tracks: list[Track]) -> None:
        for t in tracks[:40]:
            self.get(t)


era = EraStore()


def gap(a: int | None, b: int | None) -> float:
    """0 to 1 on how far apart two artists are in time, 0 when we can't tell.

    Nothing under a dozen years counts at all — a band that formed in 1969
    and one that formed in 1974 are the same era and shouldn't be nudged
    apart. It used to take twenty, which had to cover the twenty-year offset
    between a birth date and a formation date as well as any real distance,
    so it never bit: Nicki Minaj was twenty-four years off Michael Jackson,
    scored 0.1, and closed a Billie Jean queue. Both sides are career dates
    now, so the slack isn't needed and the gap can mean what it says.
    """
    if not a or not b:
        return 0.0
    years = abs(a - b)
    return min(1.0, max(0.0, (years - 12) / 28.0))
