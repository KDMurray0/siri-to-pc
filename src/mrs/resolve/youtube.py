"""A YouTube link means that video. Not something like it.

Pasting a link and getting a search for the text of the link is the kind of
wrong that makes a program feel like it isn't listening. The id is right
there; the only reason it was ever searched for is that nothing looked.

Every shape YouTube hands out is here because people paste what they were
given: the desktop watch url, the share-sheet youtu.be one, the music
subdomain, shorts, an embed, and any of them with a playlist, a timestamp,
a tracking tag and a locale bolted on.
"""

from __future__ import annotations

import re
import urllib.parse

from ..logging_setup import get
from ..models import Track
from . import catalog

log = get("youtube")

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
         "music.youtube.com", "youtu.be", "www.youtu.be",
         "youtube-nocookie.com", "www.youtube-nocookie.com"}

# Eleven characters of base64url. Tight on purpose: loose patterns match
# half of every url they are pointed at.
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID = re.compile(r"^/(?:shorts|embed|v|live)/([A-Za-z0-9_-]{11})")
_LIST = re.compile(r"^[A-Za-z0-9_-]{12,}$")
_URLISH = re.compile(r"\bhttps?://\S+|\b(?:www\.)?youtu\.be/\S+"
                     r"|\b(?:www\.|m\.|music\.)?youtube\.com/\S+", re.I)


def find_url(text: str) -> str:
    """The first YouTube link in a sentence, or "".

    People paste a link with words round it — "play this
    https://youtu.be/... it's great" — and a request that only recognises a
    url when it is the entire message is a request that mostly doesn't.
    """
    for hit in _URLISH.findall(text or ""):
        got = hit if "://" in hit else "https://" + hit
        if is_url(got):
            return got.rstrip(".,)]}>\"'")
    return ""


def _parts(url: str):
    try:
        got = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    if got.scheme not in ("http", "https", ""):
        return None
    host = (got.hostname or "").lower()
    return got if host in HOSTS else None


def is_url(text: str) -> bool:
    return _parts(text) is not None


def video_id(url: str) -> str:
    """The video this link points at, or ""."""
    got = _parts(url)
    if got is None:
        return ""
    host = (got.hostname or "").lower()
    if host.endswith("youtu.be"):
        leaf = got.path.lstrip("/").split("/")[0]
        return leaf if _ID.match(leaf) else ""
    query = urllib.parse.parse_qs(got.query)
    for key in ("v", "video_id"):
        for value in query.get(key, []):
            if _ID.match(value):
                return value
    m = _PATH_ID.match(got.path)
    return m.group(1) if m else ""


def playlist_id(url: str) -> str:
    """The list this link points at, or "".

    A watch url with a list on it is a video that happens to sit in a
    playlist, and playing the video is what was meant. Only /playlist is
    unambiguously the list itself.
    """
    got = _parts(url)
    if got is None or not got.path.startswith("/playlist"):
        return ""
    for value in urllib.parse.parse_qs(got.query).get("list", []):
        if _LIST.match(value):
            return value
    return ""


def start_at(url: str) -> int:
    """The t= on a share link, in seconds. 0 when there isn't one."""
    got = _parts(url)
    if got is None:
        return 0
    for key in ("t", "start"):
        for raw in urllib.parse.parse_qs(got.query).get(key, []):
            secs = _seconds(raw)
            if secs:
                return secs
    if got.fragment.startswith("t="):
        return _seconds(got.fragment[2:])
    return 0


def _seconds(raw: str) -> int:
    raw = (raw or "").strip().lower()
    if raw.isdigit():
        return int(raw)
    total = 0
    for count, unit in re.findall(r"(\d+)([hms])", raw):
        total += int(count) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def track_for(vid: str) -> Track | None:
    """What this video actually is — its name, who made it, how long.

    Without this the queue shows a row called by its own id until the file
    lands. The name is the whole reason anybody looks at the queue.
    """
    if not vid:
        return None
    try:
        got = catalog.client().get_song(vid) or {}
        row = got.get("videoDetails") or {}
    except Exception as exc:
        log.debug("couldn't look up %s: %s", vid, exc)
        row = {}
    title = (row.get("title") or "").strip()
    if not title:
        # A video the music API won't describe — age-gated, a podcast, a
        # plain upload. It is still perfectly playable, and yt-dlp will put
        # a real name on it as it downloads.
        return Track(video_id=vid, title=vid, artist="", origin="request")
    thumbs = (row.get("thumbnail") or {}).get("thumbnails") or []
    art = thumbs[-1].get("url", "") if thumbs else ""
    try:
        secs = int(row.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        secs = 0
    return Track(video_id=vid, title=title,
                 artist=(row.get("author") or "").strip(),
                 art=art, duration=secs, origin="request")
