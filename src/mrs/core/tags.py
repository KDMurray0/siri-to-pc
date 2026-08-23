"""What a song actually sounds like.

YouTube Music gives us a title, an artist and sometimes an album. That's not
enough to tell that Song 2 isn't representative of Blur, or that Minutes to
Midnight is a different band-era to Hybrid Theory. Last.fm's community tags
are, and they only need the API key we already have.

    One Step Closer vs Crawling          0.99   (same album)
    One Step Closer vs Shadow of the Day 0.70   (six years later)
    One Step Closer vs Song 2            0.44   (different band entirely)

Lookups happen on a background thread and every answer is cached to disk, so
ranking never waits on the network — it just gets smarter as the cache fills.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Track
from ..paths import data_dir

log = get("tags")

API = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "MusicRequestServer/2.0"}
MIN_COUNT = 5          # ignore the long tail of one-off tags
TOP_N = 10
MAX_ENTRIES = 4000     # keep the cache file from growing forever
PACE = 0.2             # Last.fm asks for <=5 requests a second


def _clean(name: str) -> str:
    return (name or "").strip().lower()


class TagStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, int]] = {}
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
            log.info("%d tagged tracks cached", len(self._cache))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("tag cache unreadable: %s", exc)

    def save(self) -> None:
        try:
            with self._lock:
                items = list(self._cache.items())[-MAX_ENTRIES:]
                data = {"tags": dict(items), "missing": list(self._missing)[-MAX_ENTRIES:]}
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._file())
        except Exception as exc:
            log.debug("couldn't save tags: %s", exc)

    # -- keys ----------------------------------------------------------
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


tagstore = TagStore()
