"""Reading an Apple Music link into names we can go and find.

Same shape as the Spotify importer: a link becomes a list of title/artist
pairs, and the ordinary search does the rest. Nothing here streams from
Apple — it only reads what a public playlist page already publishes.

Two steps, because neither alone is enough:

  The page carries a JSON-LD block listing every track, but only its *name*
  and a link. No artist, which makes searching for "Man I Need" on its own
  fairly hopeless.

  Each of those links ends in Apple's numeric song id, and the iTunes lookup
  endpoint turns ids into title + artist + artwork. It takes up to 200 ids at
  a time, needs no developer token, and is the same service the Store search
  box uses.

So: scrape the ids, then one lookup per 200 of them.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from ..logging_setup import get
from ..models import Track

log = get("applemusic")

APPLE_URL = re.compile(
    r"https?://music\.apple\.com/[a-z]{2}/(playlist|album|song)/[^\s]+", re.I)
_LD = re.compile(r'type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_SONG_ID = re.compile(r"/(\d+)(?:[?#]|$)")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

LOOKUP_CHUNK = 180          # the endpoint allows 200; leave some headroom


def is_apple_url(text: str) -> bool:
    return bool(APPLE_URL.search(text or ""))


def _get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _page(url: str) -> dict | None:
    """The JSON-LD block off a public Apple Music page."""
    m = APPLE_URL.search(url or "")
    if not m:
        return None
    try:
        html = _get(m.group(0))
    except Exception as exc:
        log.info("couldn't read the Apple Music page: %s", exc)
        return None
    block = _LD.search(html)
    if not block:
        log.info("no JSON-LD on that page — Apple may have changed the layout")
        return None
    try:
        return json.loads(block.group(1))
    except Exception as exc:
        log.info("Apple Music JSON-LD wouldn't parse: %s", exc)
        return None


def link_name(url: str) -> str:
    data = _page(url) or {}
    return (data.get("name") or "").strip()


def _lookup(ids: list[str]) -> dict[str, Track]:
    """Apple song ids -> tracks, in batches."""
    out: dict[str, Track] = {}
    for i in range(0, len(ids), LOOKUP_CHUNK):
        chunk = ids[i:i + LOOKUP_CHUNK]
        url = ("https://itunes.apple.com/lookup?entity=song&id="
               + urllib.parse.quote(",".join(chunk)))
        try:
            payload = json.loads(_get(url))
        except Exception as exc:
            log.info("itunes lookup failed: %s", exc)
            continue
        for row in payload.get("results") or []:
            tid = str(row.get("trackId") or "")
            title = (row.get("trackName") or "").strip()
            if not tid or not title:
                continue
            art = (row.get("artworkUrl100") or "").replace("100x100", "600x600")
            out[tid] = Track(title=title,
                             artist=(row.get("artistName") or "").strip(),
                             album=(row.get("collectionName") or "").strip(),
                             art=art, origin="playlist")
    return out


def track_names(url: str) -> list[Track]:
    """Every track behind an Apple Music link, as names."""
    data = _page(url)
    if not data:
        return []

    # Three different shapes, one per kind of page:
    #   playlist  MusicPlaylist    -> "track"  (singular)
    #   album     MusicAlbum       -> "tracks" (plural)
    #   song      MusicComposition -> "audio"  (a single MusicRecording)
    # Deliberately not "workExample": on an album page that's a list of the
    # artist's *other albums*, which would import the wrong thing entirely.
    rows = data.get("track") or data.get("tracks")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        one = data.get("audio")
        if isinstance(one, dict) and one.get("url"):
            rows = [one]
        elif data.get("@type") == "MusicRecording" and data.get("url"):
            rows = [data]
        else:
            return []

    ids, order = [], []
    for row in rows:
        m = _SONG_ID.search((row.get("url") or "").strip())
        if m:
            ids.append(m.group(1))
            order.append(m.group(1))
    if not ids:
        return []

    found = _lookup(ids)
    # Keep the playlist's own order; drop anything the lookup didn't know.
    out = [found[i] for i in order if i in found]
    log.info("Apple Music: %d of %d tracks resolved to names",
             len(out), len(order))
    return out
