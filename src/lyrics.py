"""Synced lyrics via LRCLIB (free, no API key, ~3M songs).

get_lyrics tries an exact match, then a fuzzy search, and returns synced
(timed) lines when available plus a plain-text fallback.
"""

import json
import re
import urllib.parse
import urllib.request

_GET = "https://lrclib.net/api/get"
_SEARCH = "https://lrclib.net/api/search"
_UA = "MusicRequestServer/1.0 (personal LAN player)"

_LRC_RE = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode())


def _parse_lrc(lrc):
    # "[mm:ss.xx] text" lines -> [{t: seconds, text}], sorted
    out = []
    for line in (lrc or "").splitlines():
        stamps = list(_LRC_RE.finditer(line))
        if not stamps:
            continue
        text = _LRC_RE.sub("", line).strip()
        for m in stamps:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append({"t": round(t, 2), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


def _shape(d):
    if not d:
        return None
    synced, plain = d.get("syncedLyrics") or "", d.get("plainLyrics") or ""
    if not synced and not plain:
        return None
    return {
        "synced": _parse_lrc(synced),
        "plain": plain,
        "title": d.get("trackName"),
        "artist": d.get("artistName"),
    }


def get_lyrics(title, artist, album="", duration=0):
    if not title:
        return None
    params = {"track_name": title, "artist_name": artist or ""}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    try:
        return _shape(_fetch(_GET + "?" + urllib.parse.urlencode(params)))
    except Exception:
        pass
    # Fuzzy fallback
    try:
        arr = _fetch(_SEARCH + "?" + urllib.parse.urlencode(
            {"q": f"{title} {artist}".strip()}))
        return _shape(arr[0]) if isinstance(arr, list) and arr else None
    except Exception:
        return None
