"""Live radio.

Stations come from radio-browser.info, which is a community directory with no
key and no account. A station is played straight from its stream URL — nothing
to download, nothing to cache.

Most stations announce what's playing over ICY metadata, so the player can show
"Olivia Rodrigo - Stupid Song" with Capital's logo beside it. The ones that
don't — the BBC's HLS streams among them — just show the station name, which
is the honest answer for speech radio anyway.

Radio is famously unreliable: streams move, time out and lie about their
bitrate. Nothing here retries hard or blocks the player if a station is down.
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

# the directory round-robins across mirrors; any of them will answer
BASE = "https://all.api.radio-browser.info/json"
UA = {"User-Agent": "MusicRequestServer/2.0 (personal LAN music player)"}
TIMEOUT = 8


def _call(path: str) -> list:
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


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
    q = urllib.parse.urlencode({"name": query, "limit": limit * 3,
                                "hidebroken": "true", "order": "votes",
                                "reverse": "true"})
    try:
        rows = _call(f"/stations/search?{q}")
    except Exception as exc:
        log.debug("station search failed for %r: %s", query, exc)
        return []
    out, seen = [], set()
    for row in rows:
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

    def start(self, mpv, station: str) -> None:
        self.stop()
        self.station = station
        self.title = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(mpv,),
                                        daemon=True, name="icy")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.title = ""

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
                    log.info("%s: %s", self.station, found)
                    misses = 0
                elif not found:
                    misses += 1
            except Exception:
                misses += 1
            # speech radio never sets it; stop asking rather than poll forever
            self._stop.wait(20 if misses > 6 else 5)


now_playing = NowPlaying()
