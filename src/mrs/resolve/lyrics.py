"""Synced lyrics from LRCLIB (free, no key)."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from ..logging_setup import get

log = get("lyrics")

API = "https://lrclib.net/api/get"
SEARCH = "https://lrclib.net/api/search"
UA = {"User-Agent": "MusicRequestServer/2.0 (personal music player)"}
_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_cache: dict[str, dict] = {}


def _fetch(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _parse_synced(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        stamps = _TIME.findall(line)
        if not stamps:
            continue
        words = _TIME.sub("", line).strip()
        for mins, secs in stamps:
            out.append({"t": int(mins) * 60 + float(secs), "text": words})
    out.sort(key=lambda r: r["t"])
    return out


def get_lyrics(title: str, artist: str, duration: int = 0) -> dict | None:
    if not title:
        return None
    key = f"{artist}|{title}"
    if key in _cache:
        return _cache[key]

    params = {"track_name": title, "artist_name": artist or ""}
    if duration:
        params["duration"] = str(int(duration))
    data = _fetch(f"{API}?{urllib.parse.urlencode(params)}")
    if not data:
        rows = _fetch(f"{SEARCH}?{urllib.parse.urlencode({'q': f'{artist} {title}'})}")
        data = rows[0] if isinstance(rows, list) and rows else None
    if not data:
        return None

    result = {
        "synced": _parse_synced(data.get("syncedLyrics") or ""),
        "plain": data.get("plainLyrics") or "",
        "title": data.get("trackName") or title,
        "artist": data.get("artistName") or artist,
    }
    if not result["synced"] and not result["plain"]:
        return None
    if len(_cache) > 100:
        _cache.clear()
    _cache[key] = result
    return result
