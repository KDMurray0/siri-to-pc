"""What the full player's home page shows.

Jump back in (what you played lately), your top artists with their pictures,
the genres those artists share, and what's charting. Nothing here waits on
the network: pictures and charts are fetched in the background and the page
gets whatever is held, filling in on the next visit.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter

from ..logging_setup import get
from ..models import Track

log = get("home")

CHART_TTL = 3600.0
_charts: dict = {"at": 0.0, "rows": [], "busy": False}
_pictures: dict[str, str] = {}          # artist (lower case) -> picture url
_asked: set[str] = set()
_lock = threading.Lock()


def _offline() -> bool:
    """The hermetic checks never reach the network, even from a background thread."""
    return os.environ.get("MRS_TESTING") == "1"

# Tags that are about the listener or the band, not the sound.
_NOT_GENRES = {"seen live", "favorites", "favourites", "favorite", "favourite", "awesome",
               "love", "best", "classic", "legend", "male vocalists", "female vocalists",
               "american", "british", "english", "german", "uk", "usa", "60s", "70s", "80s",
               "90s", "00s", "10s", "20s", "all", "music", "band", "bands"}


def picture(artist: str) -> str:
    """An artist's picture if we have it; otherwise "" and it's fetched for next time."""
    key = (artist or "").strip().lower()
    if not key:
        return ""
    with _lock:
        if key in _pictures:
            return _pictures[key]
        if key in _asked or _offline():
            return ""
        _asked.add(key)
    threading.Thread(target=_fetch_picture, args=(artist, key), daemon=True,
                     name="home-picture").start()
    return ""


def _fetch_picture(artist: str, key: str) -> None:
    from ..resolve import catalog
    try:
        rows = catalog.search_artists(artist, limit=1)
        art = (rows[0].get("art") or "") if rows else ""
    except Exception as exc:
        log.debug("picture for %r: %s", artist, exc)
        art = ""
    with _lock:
        _pictures[key] = art


def charts() -> list[dict]:
    """The chart as last fetched; refreshed in the background once an hour."""
    with _lock:
        stale = (time.time() - _charts["at"] > CHART_TTL and not _charts["busy"]
                 and not _offline())
        if stale:
            _charts["busy"] = True
        rows = list(_charts["rows"])
    if stale:
        threading.Thread(target=_fetch_charts, daemon=True, name="home-charts").start()
    return rows


def _fetch_charts() -> None:
    """Last.fm's chart, each song matched to a real recording so it can play."""
    from concurrent.futures import ThreadPoolExecutor
    from ..resolve import catalog
    from .tags import tagstore
    rows: list[dict] = []
    try:
        if tagstore.enabled():
            data = tagstore._call({"method": "chart.getTopTracks", "limit": 24})
            for r in ((data.get("tracks") or {}).get("track") or [])[:24]:
                title = (r.get("name") or "").strip()
                who = ((r.get("artist") or {}).get("name") or "").strip()
                if title and who:
                    rows.append({"title": title, "artist": who})

        def match(row: dict) -> dict:
            try:
                hits = catalog.search_songs(f"{row['title']} {row['artist']}", limit=1)
            except Exception:
                hits = []
            if hits:
                t = hits[0]
                return {**row, "video_id": t.video_id, "art": t.art, "duration": t.duration}
            return row
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = [r for r in pool.map(match, rows) if r.get("video_id")]
    except Exception as exc:
        log.debug("charts: %s", exc)
    with _lock:
        _charts.update(at=time.time(), busy=False, rows=rows or _charts["rows"])


def _recent(taste, limit: int = 12) -> list[dict]:
    out, seen = [], set()
    for row in (taste.recent(60) if taste is not None else []):
        if not isinstance(row, dict):
            continue
        vid = row.get("video_id") or ""
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({"video_id": vid, "title": row.get("title", ""), "artist": row.get("artist", ""),
                    "art": row.get("art", "")})
        if len(out) >= limit:
            break
    return out


def _genres(names: list[str], limit: int = 8) -> list[dict]:
    """The genres your top artists share, each pictured by the artist it's most theirs."""
    from .tags import tagstore
    count: Counter = Counter()
    faces: dict[str, list[str]] = {}
    for name in names:
        tags = tagstore.cached(Track(title="", artist=name)) or {}
        if not tags:
            tagstore.get(Track(title="", artist=name))     # ask, for next time
        for tag, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:5]:
            if tag in _NOT_GENRES or len(tag) > 24:
                continue
            count[tag] += 1
            faces.setdefault(tag, []).append(name)
    out = []
    known = {n.casefold() for n in names}
    for tag, _ in count.most_common(limit):
        who = faces[tag][:3]
        fresh = _explore(tag, faces[tag], known)
        out.append({"name": tag, "artist": who[0], "artists": who,
                    "art": picture(who[0]), "arts": [a for a in (picture(n) for n in who) if a],
                    "explore": [{"name": n, "art": picture(n)} for n in fresh],
                    "explore_arts": [a for a in (picture(n) for n in fresh[:3]) if a]})
    return out


def _explore(tag: str, yours: list[str], known: set[str], limit: int = 10) -> list[str]:
    """Bands next to the ones you play in this genre, that share it, that you
    don't already play much: grunge with Nirvana and Pearl Jam brings Alice in
    Chains and Soundgarden. Only what's already looked up; the rest is asked
    for, so the next visit has more."""
    from .kin import kin
    from .tags import tagstore
    words = set(tag.split())
    out: list[str] = []
    for name in yours[:4]:
        for near in kin.related(Track(title="", artist=name)):
            if near.casefold() in known or near in out:
                continue
            tags = tagstore.cached(Track(title="", artist=near))
            if tags is None:
                tagstore.get(Track(title="", artist=near))       # for next time
                continue
            top = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:6]]
            if tag in top or any(words & set(t.split()) for t in top if t not in _NOT_GENRES):
                out.append(near)
            if len(out) >= limit:
                return out
    return out


def sections(taste, lists) -> dict:
    """Everything the home page shows, from what's held right now."""
    top = [r.get("artist", "") for r in (taste.top_artists(40) if taste is not None else [])
           if isinstance(r, dict) and r.get("artist")]
    top = list(dict.fromkeys(top))[:40]
    return {
        "recent": _recent(taste),
        "artists": [{"name": n, "art": picture(n)} for n in top],
        "genres": _genres(top[:16]),
        "charts": charts(),
        "lists": lists.summary() if lists is not None else [],
    }
