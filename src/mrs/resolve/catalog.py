"""YouTube Music lookups.

Two rules everywhere: prefer the original over remixes, and never return the
30-second preview clips.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

from ..config import config
from ..logging_setup import get
from ..models import Track, is_derivative

log = get("catalog")

CREATE_NO_WINDOW = 0x08000000
_client = None
_client_lock = threading.Lock()
_prefs: list[str] = []

_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 1800


def client():
    global _client
    with _client_lock:
        if _client is None:
            from ytmusicapi import YTMusic
            _client = YTMusic()
        return _client


def validate() -> bool:
    """Confirm we can talk to YouTube Music.

    Filtered on purpose: an unfiltered search can return an artist card that
    ytmusicapi 1.8.1 fails to parse, which says nothing about connectivity.
    """
    rows = client().search("daft punk", filter="songs", limit=1)
    return bool(rows)


def set_preferences(artists: list[str]) -> None:
    global _prefs
    _prefs = [a.lower() for a in artists if a]


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value):
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = (time.time(), value)
    return value


# -- conversion --------------------------------------------------------

def _duration(row: dict) -> int:
    secs = row.get("duration_seconds")
    if isinstance(secs, int) and secs:
        return secs
    text = row.get("duration") or ""
    if isinstance(text, str) and ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
            out = 0
            for p in parts:
                out = out * 60 + p
            return out
        except Exception:
            return 0
    return 0


def _thumb(row: dict) -> str:
    thumbs = row.get("thumbnails") or []
    if not thumbs:
        return ""
    try:
        return sorted(thumbs, key=lambda t: t.get("width", 0))[-1].get("url", "")
    except Exception:
        return thumbs[-1].get("url", "")


def to_track(row: dict, origin: str = "radio") -> Track:
    artists = row.get("artists") or []
    names = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    album = row.get("album")
    album_name = album.get("name", "") if isinstance(album, dict) else (album or "")
    return Track(
        video_id=row.get("videoId") or "",
        title=(row.get("title") or "").strip(),
        artist=names.strip(),
        album=album_name,
        art=_thumb(row),
        duration=_duration(row),
        origin=origin,
    )


def _acceptable(t: Track, *, allow_variant: bool = False) -> bool:
    if not t.video_id or not t.title:
        return False
    floor = int(config.get("min_duration", 60) or 0)
    # duration 0 means "unknown" — let the downloader's filter catch it
    if floor and t.duration and t.duration < floor:
        return False
    if not allow_variant and is_derivative(t.title):
        return False
    return True


def _prefer(tracks: list[Track]) -> list[Track]:
    """Nudge artists the listener actually plays to the front."""
    if not _prefs:
        return tracks
    def key(t: Track) -> int:
        return 0 if t.primary_artist() in _prefs else 1
    return sorted(tracks, key=key)


# -- searches ----------------------------------------------------------

def search_songs(query: str, limit: int = 12, *, allow_variant: bool = False) -> list[Track]:
    key = f"songs:{query}:{limit}:{allow_variant}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        rows = client().search(query, filter="songs", limit=max(limit, 10))
    except Exception as exc:
        log.warning("search failed for %r: %s", query, exc)
        return []
    out = [to_track(r, "request") for r in rows]
    out = [t for t in out if _acceptable(t, allow_variant=allow_variant)]
    return _store(key, _prefer(out)[:limit])


def search_candidates(query: str, limit: int = 12) -> list[Track]:
    """For the UI's pick-a-result dropdown."""
    return search_songs(query, limit=limit, allow_variant=True)


def artist_tracks(artist: str, limit: int = 20) -> list[Track]:
    key = f"artist:{artist}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    tracks: list[Track] = []
    try:
        found = client().search(artist, filter="artists", limit=1)
        if found:
            info = client().get_artist(found[0]["browseId"])
            songs = (info.get("songs") or {}).get("results") or []
            tracks = [to_track(r, "request") for r in songs]
    except Exception as exc:
        log.debug("artist lookup failed for %r: %s", artist, exc)
    if len(tracks) < limit:
        tracks += search_songs(artist, limit=limit)
    seen, out = set(), []
    for t in tracks:
        if t.video_id and t.video_id not in seen and _acceptable(t):
            seen.add(t.video_id)
            out.append(t)
    return _store(key, out[:limit])


def search_artists(query: str, limit: int = 3) -> list[dict]:
    """Artist hits for the search dropdown."""
    try:
        rows = client().search(query, filter="artists", limit=limit)
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        name = r.get("artist") or r.get("title")
        if name:
            out.append({"kind": "artist", "name": name, "art": _thumb(r),
                        "subtitle": r.get("subscribers") or "Artist"})
    return out


def search_albums(query: str, limit: int = 3) -> list[dict]:
    """Album hits for the search dropdown."""
    try:
        rows = client().search(query, filter="albums", limit=limit)
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        title = r.get("title")
        if not title:
            continue
        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or [])
                            if a.get("name"))
        out.append({"kind": "album", "name": title, "artist": artists,
                    "art": _thumb(r),
                    "subtitle": f"Album{' · ' + artists if artists else ''}"})
    return out


def artist_all_tracks(artist: str, cap: int = 200) -> list[Track]:
    """Everything we can find by one artist — singles plus every album track.

    Asking for a band should play the band, not five songs and then drift, so
    this walks their albums instead of taking the handful `get_artist` returns.
    """
    key = f"artist_all:{artist}"
    hit = _cached(key)
    if hit is not None:
        return hit

    out: list[Track] = []
    seen: set[str] = set()
    seen_names: set[str] = set()

    def add(tracks: list[Track]) -> None:
        for t in tracks:
            if not t.video_id or t.video_id in seen or not _acceptable(t):
                continue
            # A discography walk hits the same song on the album, the deluxe
            # edition and the greatest hits — keep one.
            name = t.key()
            if name and name in seen_names:
                continue
            seen.add(t.video_id)
            if name:
                seen_names.add(name)
            out.append(t)

    browse_id = None
    try:
        found = client().search(artist, filter="artists", limit=1)
        if found:
            browse_id = found[0].get("browseId")
    except Exception as exc:
        log.debug("artist search failed for %r: %s", artist, exc)

    if browse_id:
        try:
            info = client().get_artist(browse_id)
            add([to_track(r, "request")
                 for r in (info.get("songs") or {}).get("results") or []])
            # Walk the discography for the rest.
            albums = (info.get("albums") or {}).get("results") or []
            singles = (info.get("singles") or {}).get("results") or []
            for entry in (albums + singles)[:25]:
                if len(out) >= cap:
                    break
                bid = entry.get("browseId")
                if not bid:
                    continue
                try:
                    al = client().get_album(bid)
                    art = _thumb(al)
                    rows = []
                    for r in al.get("tracks") or []:
                        t = to_track(r, "request")
                        t.artist = t.artist or artist
                        t.album = al.get("title", "")
                        t.art = t.art or art
                        rows.append(t)
                    add(rows)
                except Exception:
                    continue
        except Exception as exc:
            log.debug("artist walk failed for %r: %s", artist, exc)

    if len(out) < 20:
        add(search_songs(artist, limit=25))

    log.info("artist catalogue for %r: %d tracks", artist, len(out))
    return _store(key, out[:cap])


def album_tracks(album: str, artist: str = "", limit: int = 0) -> list[Track]:
    query = f"{album} {artist}".strip()
    try:
        rows = client().search(query, filter="albums", limit=1)
        if not rows:
            return []
        info = client().get_album(rows[0]["browseId"])
        art = _thumb(info)
        out = []
        for r in info.get("tracks") or []:
            t = to_track(r, "request")
            if not t.artist:
                t.artist = artist or ", ".join(
                    a.get("name", "") for a in (info.get("artists") or []))
            t.album = info.get("title", album)
            t.art = t.art or art
            if t.video_id:
                out.append(t)
        return out[:limit] if limit else out
    except Exception as exc:
        log.debug("album lookup failed for %r: %s", query, exc)
        return []


def genre_tracks(genre: str, limit: int = 25) -> list[Track]:
    key = f"genre:{genre}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    out: list[Track] = []
    try:
        cats = client().get_mood_categories()
        target = _squash(genre)
        params = None
        for group in cats.values():
            for cat in group:
                if _squash(cat.get("title", "")) == target:
                    params = cat.get("params")
                    break
            if params:
                break
        if params:
            for pl in (client().get_mood_playlists(params) or [])[:2]:
                try:
                    items = client().get_playlist(pl["playlistId"], limit=limit)
                    out += [to_track(r) for r in items.get("tracks", [])]
                except Exception:
                    continue
    except Exception as exc:
        log.debug("mood lookup failed for %r: %s", genre, exc)
    if len(out) < limit:
        out += search_songs(f"{genre} music", limit=limit)
    seen, res = set(), []
    for t in out:
        if t.video_id and t.video_id not in seen and _acceptable(t):
            seen.add(t.video_id)
            res.append(t)
    return _store(key, res[:limit])


def _watch_rows(video_id: str, limit: int) -> list[dict]:
    """Radio rows for a track.

    ytmusicapi reads the Related tab's browse id before it parses the tracks,
    and YouTube stopped sending that key — so a KeyError('endpoint') threw away
    a response that had 50 perfectly good tracks in it. Try the library first
    (in case it gets fixed), then parse the same response ourselves.
    """
    try:
        wp = client().get_watch_playlist(videoId=video_id, radio=True, limit=limit + 10)
        return wp.get("tracks") or []
    except Exception as exc:
        log.debug("watch playlist fell back for %s: %s", video_id, exc)

    from ytmusicapi.navigation import TAB_CONTENT, nav
    from ytmusicapi.parsers.watch import parse_watch_playlist
    body = {"enablePersistentPlaylistPanel": True, "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
            "videoId": video_id, "playlistId": "RDAMVM" + video_id,
            "params": "wAEB"}
    resp = client()._send_request("next", body)
    watch = nav(resp, ["contents", "singleColumnMusicWatchNextResultsRenderer",
                       "tabbedRenderer", "watchNextTabbedResultsRenderer"])
    results = nav(watch, [*TAB_CONTENT, "musicQueueRenderer", "content",
                          "playlistPanelRenderer"], True)
    return parse_watch_playlist(results["contents"]) if results else []


def related(video_id: str, limit: int = 10) -> list[Track]:
    """The 'radio' continuation for a track."""
    if not video_id:
        return []
    key = f"radio:{video_id}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        rows = _watch_rows(video_id, limit)
    except Exception as exc:
        log.debug("radio failed for %s: %s", video_id, exc)
        return []
    out = []
    for r in rows:
        t = to_track(r)
        if t.video_id != video_id and _acceptable(t):
            out.append(t)
    return _store(key, out[:limit])


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# -- other sources -----------------------------------------------------

def search_soundcloud(query: str, limit: int = 3) -> list[Track]:
    return _ytdlp_search(f"scsearch{limit}:{query}", "soundcloud")


def search_bandcamp(query: str, limit: int = 3) -> list[Track]:
    return _ytdlp_search(f"bcsearch{limit}:{query}", "bandcamp")


def _ytdlp_search(spec: str, source: str) -> list[Track]:
    exe = shutil.which("yt-dlp")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "--dump-json", "--flat-playlist", "--no-warnings", spec],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        out.append(Track(
            video_id=d.get("id") or "",
            title=d.get("title") or "",
            artist=d.get("uploader") or d.get("channel") or "",
            url=d.get("url") or d.get("webpage_url") or "",
            duration=int(d.get("duration") or 0),
            art=d.get("thumbnail") or "",
            source=source,
        ))
    return out
