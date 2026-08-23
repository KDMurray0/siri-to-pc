"""Live radio, from radio-browser.info. No key needed.

Played straight off the stream — nothing downloads. Most stations announce the
song over ICY metadata, so the player shows that with the station underneath;
speech radio doesn't, and just shows its own name.

Streams move, time out and lie. Nothing here retries hard or blocks the player.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..models import Track

log = get("radio")

# all.api round-robins across mirrors and drops the connection if you ask it
# twice quickly — searching for eighteen stations in a row lost half of them
# to "connection forcibly closed". Named mirrors, tried in turn, fix it.
HOSTS = ("https://de2.api.radio-browser.info/json",
         "https://nl1.api.radio-browser.info/json",
         "https://at1.api.radio-browser.info/json",
         "https://all.api.radio-browser.info/json")
UA = {"User-Agent": "MusicRequestServer/2.0 (personal LAN music player)"}
TIMEOUT = 8
CACHE_TTL = 600
MIN_GAP = 0.4          # the search box fires on every keystroke
COOLDOWN = 15.0        # after it keeps refusing, leave it alone a while

_cache: dict[str, tuple[float, list]] = {}
_last_call = 0.0
_blocked_until = 0.0
_misses = 0
_gate = threading.Lock()


def _call(path: str) -> list:
    """One request, spaced out, with the mirrors as backup.

    The directory drops connections if you ask it twice in quick succession,
    and the search box asks on every keystroke. Left unchecked that loses
    perfectly good stations to "connection forcibly closed" — Virgin Radio
    among them.
    """
    global _last_call, _blocked_until, _misses
    with _gate:
        now = time.time()
        if now < _blocked_until:
            raise RuntimeError("station directory is cooling off")
        wait = MIN_GAP - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

    last = None
    for host in HOSTS:
        try:
            req = urllib.request.Request(host + path, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            with _gate:
                _misses = 0
            return data if isinstance(data, list) else []
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    with _gate:
        # one blip is just a blip; back off only when it keeps happening
        _misses += 1
        if _misses >= 3:
            _blocked_until = time.time() + COOLDOWN
            _misses = 0
    raise last or RuntimeError("no mirror answered")


def _to_track(row: dict) -> Track | None:
    url = (row.get("url_resolved") or row.get("url") or "").strip()
    name = (row.get("name") or "").strip()
    if not url or not name:
        return None
    return Track(
        title=name,
        artist=(row.get("country") or "Radio").strip() or "Radio",
        art=(row.get("favicon") or "").strip(),
        url=url,
        source="radio",
        origin="request",
        reason="asked",
    )


def search(query: str, limit: int = 4) -> list[Track]:
    """Stations matching a name. Most-voted first, dead ones excluded."""
    query = (query or "").strip()
    if len(query) < 3:
        return []
    ck = f"{query.lower()}:{limit}"
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    q = urllib.parse.urlencode({"name": query, "limit": limit * 6,
                                "hidebroken": "true", "order": "votes",
                                "reverse": "true"})
    try:
        rows = _call(f"/stations/search?{q}")
    except Exception as exc:
        log.debug("station search failed for %r: %s", query, exc)
        return []

    want = query.lower()

    def rank(row: dict) -> tuple:
        """Asking for Virgin Radio should offer Virgin Radio, not Virgin Radio
        Classic Rock, however many votes the spin-off has."""
        name = (row.get("name") or "").lower()
        votes = int(row.get("votes") or 0)
        return (name != want, not name.startswith(want), -votes)

    out, seen = [], set()
    for row in sorted(rows, key=rank):
        t = _to_track(row)
        if not t:
            continue
        key = t.title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    _cache[ck] = (time.time(), out)
    return out


def is_station(track: Track | None) -> bool:
    return bool(track and track.source == "radio" and track.url)


# ── what's playing on it ──────────────────────────────────────────────

def _clean(title: str) -> str:
    t = (title or "").strip()
    # stations pad the title with their own name and jingle text
    for junk in (" - Powered by", " | ", " :: "):
        if junk in t:
            t = t.split(junk)[0].strip()
    return t


def split_title(text: str) -> tuple[str, str]:
    """"Artist - Title" off the wire, into its two halves.

    Stations are inconsistent about the dash and about padding, and some send
    only a show name with no separator at all — that isn't a song and gets
    treated as one.
    """
    t = _clean(text)
    for dash in (" - ", " – ", " — ", " -", "- "):
        if dash in t:
            left, _, right = t.partition(dash)
            left, right = left.strip(" -"), right.strip(" -")
            if left and right:
                return left, right
    return "", ""


class NowPlaying:
    """Polls mpv for the stream's ICY title while a station is on.

    mpv already parses the metadata out of the stream, so this is one cheap
    property read rather than a second connection to the station.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.title = ""
        self.station = ""
        self.artist = ""
        self.song = ""

    def start(self, mpv, station: str) -> None:
        self.stop()
        self.station = station
        self.title = ""
        self.artist = ""
        self.song = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(mpv,),
                                        daemon=True, name="icy")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.title = ""
        self.artist = ""
        self.song = ""

    def _loop(self, mpv) -> None:
        misses = 0
        while not self._stop.is_set():
            try:
                meta = mpv.get("metadata", None) or {}
                found = ""
                if isinstance(meta, dict):
                    for k, v in meta.items():
                        if k.lower() in ("icy-title", "streamtitle", "title"):
                            found = _clean(str(v))
                            break
                if found and found != self.title:
                    self.title = found
                    self.artist, self.song = split_title(found)
                    log.info("%s: %s", self.station, found)
                    misses = 0
                elif not found:
                    misses += 1
            except Exception:
                misses += 1
            # speech radio never sets it; stop asking rather than poll forever
            self._stop.wait(20 if misses > 6 else 5)


now_playing = NowPlaying()
