"""What a song actually sounds like, from Last.fm tags.

A title and an artist can't tell you Song 2 isn't much like the rest of Blur.
Tags can:

    One Step Closer vs Crawling          0.99   same album
    One Step Closer vs Shadow of the Day 0.70   six years later
    One Step Closer vs Song 2            0.44   different band

Looked up on a background thread and cached to disk, so ranking never waits.
"""

from __future__ import annotations

import json
import math
import re
import queue
import threading
import time
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Track, _strip_article
from ..paths import data_dir

log = get("tags")

API = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "MusicRequestServer/2.0"}
MIN_COUNT = 5          # ignore the long tail of one-off tags
TOP_N = 10
MAX_ENTRIES = 4000     # keep the cache file from growing forever
PACE = 0.2             # Last.fm asks for <=5 requests a second

# Tags that describe half of music. Fine as a description, useless as a steer:
# ask Last.fm for "piano" and it hands you Billy Joel and Bruno Mars.
_CATCH_ALL = {"rock", "pop", "electronic", "indie", "alternative", "classical",
              "jazz", "metal", "hip-hop", "hip hop", "rap", "dance", "soul",
              "folk", "instrumental", "piano", "guitar", "acoustic", "chillout",
              "ambient", "experimental", "singer-songwriter", "female vocalists",
              "male vocalists", "british", "american", "oldies", "favorites"}


def _clean(name: str) -> str:
    return (name or "").strip().lower()


class TagStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, int]] = {}
        self._near: dict[str, dict[str, float]] = {}   # track -> its neighbours
        self._missing: set[str] = set()      # looked up, genuinely has none
        self._queued: set[str] = set()
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loaded = False

    # -- disk ----------------------------------------------------------
    def _file(self):
        return data_dir() / "tags.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
            with self._lock:
                self._cache = {k: v for k, v in (raw.get("tags") or {}).items()
                               if isinstance(v, dict)}
                self._missing = set(raw.get("missing") or [])
                self._near = {k: v for k, v in (raw.get("near") or {}).items()
                              if isinstance(v, dict)}
            log.info("%d tagged tracks cached, %d with neighbours",
                     len(self._cache), len(self._near))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("tag cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                items = list(self._cache.items())[-MAX_ENTRIES:]
                near = list(self._near.items())[-MAX_ENTRIES:]
                data = {"tags": dict(items), "near": dict(near),
                        "missing": list(self._missing)[-MAX_ENTRIES:]}
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._file())
        except Exception as exc:
            log.debug("couldn't save tags: %s", exc)

    # -- keys ----------------------------------------------------------
    @staticmethod
    def near_key(track: Track | None) -> str:
        if not track:
            return ""
        t = (track.title or "").strip().lower()
        return f"{track.primary_artist()}|{t}" if t else ""

    def affinity(self, seed: Track | None, other: Track | None) -> float | None:
        """Do people play these two together? None if we haven't looked yet."""
        if not self.enabled() or not seed or not other:
            return None
        self.load()
        sk, ok = self.near_key(seed), self.near_key(other)
        if not sk or not ok:
            return None
        with self._lock:
            near = self._near.get(sk)
        if near is None:
            self._enqueue("n:" + sk, seed)
            return None
        return near.get(ok, 0.0)

    def prime(self, track: Track | None) -> None:
        """Look this up now instead of queueing it.

        The worker takes things in order, so a fresh request's own tags sit
        behind the few hundred the last run queued — and by the time they
        arrive the radio has already picked twenty tracks without knowing what
        it was meant to sound like. The song you actually asked for is worth
        one blocking call.
        """
        if not track or not self.enabled():
            return
        self.load()
        tk, ak = self._track_key(track), self._artist_key(track)
        with self._lock:
            if self._cache.get(tk) or self._cache.get(ak):
                return
            if tk in self._missing and ak in self._missing:
                return
        artist, title = track.primary_artist(), track.title
        try:
            song = self._fetch_track(artist, title)
            band = {} if song else self._fetch_artist(artist)
        except Exception as exc:
            log.debug("prime failed for %s: %s", artist, exc)
            return
        with self._lock:
            if song:
                self._cache[tk] = song
            elif band:
                self._cache[ak] = band
                self._missing.add(tk)
            else:
                self._missing.add(tk)
                self._missing.add(ak)

    def top_tag(self, track: Track | None) -> str:
        """The tag worth fetching more records by, or "" if we don't know yet.

        Not the commonest one. Nuvole Bianche is tagged piano 100, classical
        67, contemporary classical 44 — and asking for "piano" gets you Billy
        Joel and Bruno Mars, which is not what was wanted. The specific tag is
        the useful one, so among the tags with real weight behind them the
        wordiest wins.
        """
        tags = self.get(track) if track else None
        if not tags:
            return ""
        skip = ("seen live", "favourite", "favorite", "albums i own",
                "check out", "spotify", "awesome", "beautiful")
        # People tag tracks with the band's name. Feeding that to a genre
        # search asks for songs *called* Bob Marley, and you get five of them
        # by five different artists.
        who = (track.primary_artist() or "").lower()
        parts = {w for w in re.split(r"[^a-z0-9]+", who) if len(w) > 2}
        top = max(tags.values()) or 1
        best, best_rank = "", ()
        for tag, count in tags.items():
            # 0.22, not a third: Jolene is country 1.0 and classic country
            # 0.24, and the difference between those two is Kenny Rogers or
            # Morgan Wallen.
            if len(tag) < 3 or count < top * 0.22 or any(s in tag for s in skip):
                continue
            if tag[:2].isdigit():
                continue                      # "90s", "80s": an era, not a sound
            if who and (tag in who or who in tag
                        or any(p in tag.split() for p in parts)):
                continue                      # the band's own name
            # Blur is tagged rock 100, britpop 83. Britpop is the answer.
            rank = (tag not in _CATCH_ALL, len(tag.split()), count)
            if rank > best_rank:
                best, best_rank = tag, rank
        return best

    def nobody(self, track: Track | None) -> bool:
        """Last.fm has been asked about this artist and has never heard of them.

        Different from "we haven't looked yet", which is most of the pool most
        of the time. An artist only lands in _missing after a call that came
        back empty, and every real act — however obscure — has a page. What's
        left is the AI piano and stock-library uploads that fill YouTube's
        related list for anything soft: Dennis Korn, Frozen Silence, and the
        rest of the names you've never heard because nobody has.
        """
        if not track or not self.enabled():
            return False
        self.load()
        with self._lock:
            return self._artist_key(track) in self._missing

    def neighbours(self, seed: Track | None) -> dict[str, float]:
        """Names of what sits alongside this, once we've looked."""
        if not self.enabled() or not seed:
            return {}
        self.load()
        sk = self.near_key(seed)
        if not sk:
            return {}
        with self._lock:
            near = self._near.get(sk)
        if near is None:
            self._enqueue("n:" + sk, seed)
            return {}
        return dict(near)

    @staticmethod
    def _track_key(track: Track) -> str:
        return f"t:{_clean(track.primary_artist())}|{_clean(track.title)}"

    @staticmethod
    def _artist_key(track: Track) -> str:
        return f"a:{_clean(track.primary_artist())}"

    # -- lookup --------------------------------------------------------
    def enabled(self) -> bool:
        return bool(config.get("lastfm_api_key") and config.get("use_tags", True))

    def get(self, track: Track) -> dict[str, int] | None:
        """Tags we already hold. Anything unknown is queued for later."""
        if not track or not self.enabled():
            return None
        self.load()
        tk, ak = self._track_key(track), self._artist_key(track)
        with self._lock:
            hit = self._cache.get(tk)
            if hit:
                return hit
            artist_hit = self._cache.get(ak)
            known_missing = tk in self._missing
        if not known_missing:
            self._enqueue(tk, track)
        if artist_hit:
            return artist_hit          # the band's sound, until we know the song's
        self._enqueue(ak, track)
        return None

    def _enqueue(self, key: str, track: Track) -> None:
        with self._lock:
            if key in self._queued or len(self._queued) > 500:
                return
            self._queued.add(key)
        self._q.put((key, f"{track.primary_artist()}\x00{track.title}"))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, daemon=True, name="tags")
        self._thread.start()

    def _worker(self) -> None:
        dirty = 0
        while True:
            try:
                key, payload = self._q.get(timeout=20)
            except queue.Empty:
                break
            artist, _, title = payload.partition("\x00")
            try:
                if key.startswith("n:"):
                    # who people play alongside this: a different question, and
                    # a different cache from the tags
                    with self._lock:
                        self._near[key[2:]] = self._fetch_near(artist, title)
                        self._queued.discard(key)
                    dirty += 1
                    time.sleep(PACE)
                    continue
                found = (self._fetch_artist(artist) if key.startswith("a:")
                         else self._fetch_track(artist, title))
                with self._lock:
                    if found:
                        self._cache[key] = found
                    else:
                        self._missing.add(key)
                    self._queued.discard(key)
                dirty += 1
            except Exception as exc:
                log.debug("tag lookup failed for %s: %s", key, exc)
                with self._lock:
                    self._queued.discard(key)
            if dirty >= 20:
                self.save()
                dirty = 0
            time.sleep(PACE)
        if dirty:
            self.save()

    def _call(self, params: dict) -> dict:
        params = {**params, "api_key": config.get("lastfm_api_key"),
                  "format": "json", "autocorrect": "1"}
        url = API + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=15) as r:
            return json.loads(r.read().decode())

    def _parse(self, rows, drop: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows[:TOP_N]:
            name = _clean(row.get("name"))
            try:
                count = int(row.get("count", 0))
            except (TypeError, ValueError):
                continue
            # the band's own name is a tag on half of Last.fm; it tells us
            # nothing about the sound and would make every same-artist pair
            # look identical
            if not name or name == drop or count < MIN_COUNT:
                continue
            out[name] = count
        return out

    def _fetch_track(self, artist: str, title: str) -> dict[str, int]:
        if not artist or not title:
            return {}
        data = self._call({"method": "track.getTopTags",
                           "artist": artist, "track": title})
        rows = (data.get("toptags") or {}).get("tag") or []
        return self._parse(rows if isinstance(rows, list) else [], _clean(artist))

    def _fetch_near(self, artist: str, title: str) -> dict[str, float]:
        """Tracks people actually play alongside this one.

        Tags describe a band, so every Blur song looks like every other Blur
        song — Song 2 and The Universal score 0.86 on tags alone. What people
        listen to together knows the difference: Song 2 sits with Beetlebum and
        Parklife, not the orchestral one.
        """
        if not artist or not title:
            return {}
        data = self._call({"method": "track.getSimilar", "artist": artist,
                           "track": title, "limit": 30})
        rows = (data.get("similartracks") or {}).get("track") or []
        out: dict[str, float] = {}
        for row in rows if isinstance(rows, list) else []:
            name = _clean(row.get("name"))
            who = _clean(((row.get("artist") or {}) or {}).get("name"))
            try:
                match = float(row.get("match") or 0.0)
            except (TypeError, ValueError):
                continue
            if name and who and match > 0:
                out[f"{_strip_article(who)}|{name}"] = round(match, 3)
        return out

    def _fetch_artist(self, artist: str) -> dict[str, int]:
        if not artist:
            return {}
        data = self._call({"method": "artist.getTopTags", "artist": artist})
        rows = (data.get("toptags") or {}).get("tag") or []
        return self._parse(rows if isinstance(rows, list) else [], _clean(artist))

    # -- comparison ----------------------------------------------------
    def similarity(self, a: Track | None, b: Track | None) -> float | None:
        """Weighted cosine of two tag clouds, or None if either is unknown."""
        ta, tb = self.get(a), self.get(b)
        if not ta or not tb:
            return None
        keys = set(ta) | set(tb)
        va = [ta.get(k, 0) for k in keys]
        vb = [tb.get(k, 0) for k in keys]
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(x * x for x in vb))
        if not na or not nb:
            return None
        return sum(x * y for x, y in zip(va, vb)) / (na * nb)

    def warm(self, tracks: list[Track]) -> None:
        """Queue lookups for a batch without caring about the answers yet."""
        for t in tracks[:60]:
            self.get(t)

    def top_tracks_for_tag(self, tag: str, limit: int = 30) -> list[tuple[str, str]]:
        """Songs people have actually filed under this genre, as (title, artist).

        The alternative was searching YouTube for the words "grunge music" and
        taking whatever came back, which is a text match and knows nothing
        about genre. This is the real question being asked.
        """
        if not tag or not self.enabled():
            return []
        try:
            data = self._call({"method": "tag.getTopTracks", "tag": tag,
                               "limit": max(10, min(100, limit))})
        except Exception as exc:
            log.debug("tag lookup failed for %r: %s", tag, exc)
            return []
        rows = (data.get("tracks") or {}).get("track") or []
        out = []
        for row in rows if isinstance(rows, list) else []:
            title = (row.get("name") or "").strip()
            artist = ((row.get("artist") or {}) or {}).get("name", "").strip()
            if title and artist:
                out.append((title, artist))
        return out


tagstore = TagStore()
