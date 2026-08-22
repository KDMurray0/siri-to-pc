"""Spotify links.

We never download from Spotify, just read the track names and find each one
through YouTube Music like any other request.

Names come from Spotify's own embed page, which carries the whole track list as
JSON. spotdl was the original plan but it bundles its own ytmusicapi and dies on
the same parser bug our searches hit, so it's only a fallback now.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from ..logging_setup import get
from ..models import Track

log = get("spotify")

CREATE_NO_WINDOW = 0x08000000
SPOTIFY_URL = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|track|artist)/([A-Za-z0-9]+)",
    re.I)
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def is_spotify_url(text: str) -> bool:
    return bool(SPOTIFY_URL.search(text or ""))


def available() -> bool:
    return True          # reading the embed page needs nothing installed


def parts(url: str):
    m = SPOTIFY_URL.search(url or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def _dig(obj, key, depth: int = 0):
    """First value for `key` anywhere in nested json."""
    if depth > 14:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _dig(v, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _dig(v, key, depth + 1)
            if found is not None:
                return found
    return None


def _embed_json(kind: str, spotify_id: str):
    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=25
        ).read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning("couldn't load the Spotify page: %s", exc)
        return None
    m = _NEXT_DATA.search(html)
    if not m:
        log.warning("no track data on that page — is the link public?")
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _from_embed(kind: str, spotify_id: str) -> list[Track]:
    data = _embed_json(kind, spotify_id)
    if not data:
        return []
    rows = _dig(data, "trackList") or []
    name = _dig(data, "name") or ""
    out: list[Track] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        out.append(Track(title=title,
                         artist=(row.get("subtitle") or "").strip(),
                         album=name if kind == "album" else "",
                         duration=int((row.get("duration") or 0) / 1000),
                         origin="playlist"))
    if out:
        log.info("read %d tracks from Spotify %s %r", len(out), kind, name)
    return out


def _from_spotdl(url: str, timeout: int = 180) -> list[Track]:
    exe = shutil.which("spotdl")
    if not exe:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "list.spotdl"
        try:
            subprocess.run([exe, "save", url, "--save-file", str(out_file)],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, cwd=tmp)
        except Exception:
            return []
        if not out_file.exists():
            return []
        try:
            rows = json.loads(out_file.read_text(encoding="utf-8"))
        except Exception:
            return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        title = (row.get("name") or "").strip()
        if not title:
            continue
        artists = row.get("artists") or []
        artist = ", ".join(a for a in artists if a) if isinstance(artists, list) \
            else str(row.get("artist") or "")
        out.append(Track(title=title, artist=artist,
                         album=row.get("album_name") or "",
                         art=row.get("cover_url") or "", origin="playlist"))
    return out


def link_name(url: str) -> str:
    """What Spotify calls this playlist/album, for naming an import."""
    got = parts(url)
    if not got:
        return ""
    data = _embed_json(*got)
    return (_dig(data, "name") or "") if data else ""


def track_names(url: str) -> list[Track]:
    """Every track behind a Spotify link, as names."""
    got = parts(url)
    if not got:
        return []
    kind, spotify_id = got
    tracks = _from_embed(kind, spotify_id)
    if not tracks:
        log.info("embed gave nothing, falling back to spotdl")
        tracks = _from_spotdl(url)
    return tracks


def resolve_imported(tracks: list[Track], limit: int = 200,
                     on_progress=None) -> list[Track]:
    """Match imported names to playable tracks, paced so we don't get limited."""
    from . import catalog
    out: list[Track] = []
    total = min(len(tracks), limit)
    for i, t in enumerate(tracks[:limit], 1):
        if on_progress:
            try:
                on_progress(i, total, t.title)
            except Exception:
                pass
        if i > 1:
            time.sleep(0.3)
        query = f"{t.title} {t.artist}".strip()
        hits = catalog.search_songs(query, limit=3)
        if not hits:
            hits = catalog.search_songs(t.title, limit=3)
        if hits:
            best = hits[0]
            best.origin = "playlist"
            best.art = best.art or t.art
            out.append(best)
    return out
