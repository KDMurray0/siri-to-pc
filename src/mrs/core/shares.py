"""A song, shared: a link anybody can open to hear it, and its Spotify twin.

A share is one song, not a way in. Its link plays that song -- no account, no
sign-in, no search, no queue -- and does nothing else, and it expires. It has
the tags a chat app reads (Discord, iMessage, WhatsApp), so pasted anywhere it
turns into a card with the cover and the name.

The Spotify link comes from Spotify's own search, with the free developer app
the owner sets up under Connections (song.link used to do this without one and
has closed its public door). Asked once per song and remembered.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("shares")

KEEP_DAYS = 30
MAX_SHARES = 2000
_lock = threading.Lock()


def _path():
    return data_dir() / "shares.json"


def _read() -> dict:
    try:
        got = json.loads(_path().read_text("utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(rows: dict) -> None:
    write_atomic(_path(), json.dumps(rows))


def create(track: dict, by: str = "") -> dict:
    """Share one song. The same song shared again by the same person is the same link."""
    vid = str(track.get("video_id") or "").strip()
    if not vid or len(vid) > 40:
        raise ValueError("no song to share")
    now = int(time.time())
    with _lock:
        rows = {k: v for k, v in _read().items() if v.get("expires", 0) > now}
        for sid, row in rows.items():
            if row["video_id"] == vid and row.get("by") == by:
                row["expires"] = now + KEEP_DAYS * 86400
                _write(rows)
                return dict(row, id=sid)
        if len(rows) >= MAX_SHARES:
            for sid in sorted(rows, key=lambda k: rows[k].get("created", 0))[:len(rows) - MAX_SHARES + 1]:
                rows.pop(sid, None)
        sid = secrets.token_urlsafe(8)
        rows[sid] = {"video_id": vid, "title": str(track.get("title") or "")[:200],
                     "artist": str(track.get("artist") or "")[:200],
                     "art": str(track.get("art") or "")[:500],
                     "duration": int(track.get("duration") or 0), "by": by[:40],
                     "created": now, "expires": now + KEEP_DAYS * 86400, "spotify": ""}
        _write(rows)
        return dict(rows[sid], id=sid)


def get(sid: str) -> dict | None:
    row = _read().get(sid or "")
    if not row or row.get("expires", 0) < time.time():
        return None
    return dict(row, id=sid)


def forget_by(who: str) -> int:
    """Every share somebody made. For when their account goes."""
    with _lock:
        rows = _read()
        mine = [k for k, v in rows.items() if v.get("by") == who]
        for k in mine:
            rows.pop(k, None)
        if mine:
            _write(rows)
        return len(mine)


_token: dict = {"value": "", "until": 0.0}


def spotify_ready() -> bool:
    from ..config import config
    return bool(str(config.get("spotify_client_id") or "").strip()
                and str(config.get("spotify_client_secret") or "").strip())


def _spotify_token() -> str:
    """An app token from Spotify's client-credentials flow, kept until it lapses."""
    import base64
    from ..config import config
    if _token["value"] and time.time() < _token["until"] - 60:
        return _token["value"]
    pair = f"{config.get('spotify_client_id', '').strip()}:{config.get('spotify_client_secret', '').strip()}"
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(pair.encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=8) as r:
        got = json.loads(r.read().decode("utf-8"))
    _token.update(value=got.get("access_token", ""),
                  until=time.time() + float(got.get("expires_in") or 3600))
    return _token["value"]


def _songlink(row: dict) -> str:
    """The same recording on Spotify: searched by title and artist, the artist checked."""
    from ..models import Track
    from ..resolve import ranking
    if not spotify_ready():
        return ""
    q = f'track:"{row.get("title", "")}"' + (f' artist:"{row["artist"].split(",")[0]}"'
                                               if row.get("artist") else "")
    url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode({"q": q, "type": "track", "limit": 5})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _spotify_token()})
    with urllib.request.urlopen(req, timeout=8) as r:
        items = ((json.loads(r.read().decode("utf-8")).get("tracks") or {}).get("items")) or []
    for it in items:
        names = ", ".join(a.get("name", "") for a in it.get("artists") or [])
        got = Track(title=it.get("name", ""), artist=names)
        if (not row.get("artist") or ranking.artist_matches(got, row["artist"].split(",")[0])) \
                and ranking.title_matches(got, row.get("title", "").split(" (")[0]):
            return (it.get("external_urls") or {}).get("spotify", "")
    return ""


def spotify(sid: str) -> str:
    """The same song on Spotify, or "" if it isn't there or there's no Spotify app."""
    row = get(sid)
    if not row:
        return ""
    if row.get("spotify"):
        return row["spotify"]
    try:
        link = _songlink(row)
    except Exception as exc:
        log.info("Spotify search failed for %s: %s", row["video_id"], exc)
        return ""
    if link.startswith("https://open.spotify.com/"):
        with _lock:
            rows = _read()
            if sid in rows:
                rows[sid]["spotify"] = link
                _write(rows)
        return link
    return ""
