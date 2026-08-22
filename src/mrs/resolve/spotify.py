"""Import a Spotify playlist/album/track by URL.

We never download from Spotify — `spotdl` is used purely to read the track
names, then each one is resolved through our normal YouTube Music path.

spotdl is shelled out to rather than imported: it pins older FastAPI and
ytmusicapi versions and would fight our dependencies inside one process.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..logging_setup import get
from ..models import Track

log = get("spotify")

CREATE_NO_WINDOW = 0x08000000
SPOTIFY_URL = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(playlist|album|track|artist)/([A-Za-z0-9]+)",
    re.I)


def is_spotify_url(text: str) -> bool:
    return bool(SPOTIFY_URL.search(text or ""))


def available() -> bool:
    return shutil.which("spotdl") is not None


def _parse_save_file(path: Path) -> list[Track]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read spotdl output: %s", exc)
        return []
    out: list[Track] = []
    for row in data if isinstance(data, list) else []:
        name = (row.get("name") or "").strip()
        artists = row.get("artists") or []
        artist = ", ".join(a for a in artists if a) if isinstance(artists, list) \
            else str(row.get("artist") or "")
        if not name:
            continue
        out.append(Track(
            title=name,
            artist=artist or (row.get("artist") or ""),
            album=(row.get("album_name") or row.get("album") or ""),
            art=row.get("cover_url") or "",
            duration=int(row.get("duration") or 0),
            origin="playlist",
        ))
    return out


def track_names(url: str, timeout: int = 180) -> list[Track]:
    """Read a Spotify link and return its tracks as names (no audio)."""
    exe = shutil.which("spotdl")
    if not exe:
        log.warning("spotdl not installed")
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "list.spotdl"
        try:
            proc = subprocess.run(
                [exe, "save", url, "--save-file", str(out_file)],
                capture_output=True, text=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW, cwd=tmp)
        except Exception as exc:
            log.warning("spotdl failed: %s", exc)
            return []
        if not out_file.exists():
            log.warning("spotdl produced nothing: %s",
                        (proc.stderr or proc.stdout or "")[-200:])
            return []
        tracks = _parse_save_file(out_file)
    log.info("spotify import: %d tracks from %s", len(tracks), url)
    return tracks


def resolve_imported(tracks: list[Track], limit: int = 100,
                     on_progress=None) -> list[Track]:
    """Turn imported names into playable YouTube Music tracks.

    Paced deliberately: a 60-track playlist is 60 searches, and hammering them
    back to back is how you get rate-limited.
    """
    import time as _time

    from . import catalog
    out: list[Track] = []
    for i, t in enumerate(tracks[:limit], 1):
        if on_progress:
            try:
                on_progress(i, min(len(tracks), limit), t.title)
            except Exception:
                pass
        if i > 1:
            _time.sleep(0.35)
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
