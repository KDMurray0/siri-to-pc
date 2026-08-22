"""Plan -> actual tracks."""

from __future__ import annotations

from ..config import config
from ..logging_setup import get
from ..models import Plan, Track
from . import catalog

log = get("resolve")


class Resolution:
    def __init__(self, tracks: list[Track], spoken: str, *,
                 hold_radio: bool = False, alternates: list[str] | None = None,
                 error: str = "") -> None:
        self.tracks = tracks
        self.spoken = spoken
        self.hold_radio = hold_radio          # play these purely before radio
        self.alternates = alternates or []
        self.error = error

    def __bool__(self) -> bool:
        return bool(self.tracks)


def _artist_exact(query: str) -> str | None:
    """Is this phrase simply the name of a band?"""
    try:
        rows = catalog.client().search(query, limit=3)
    except Exception:
        return None
    q = query.strip().lower()
    for r in rows:
        if r.get("resultType") == "artist" and (r.get("artist") or "").lower() == q:
            return r.get("artist")
    return None


def resolve(plan: Plan) -> Resolution:
    kind = plan.kind
    query = (plan.query or "").strip()
    if not query:
        return Resolution([], "I didn't catch that", error="empty")

    source = (plan.source or config.get("source") or "youtube").lower()
    if source in ("soundcloud", "bandcamp"):
        fn = catalog.search_soundcloud if source == "soundcloud" else catalog.search_bandcamp
        hits = fn(query, limit=5)
        if not hits:
            return Resolution([], f"Nothing found on {source}", error="no results")
        return Resolution(hits[:1], f"Playing {hits[0].title} from {source}")

    if kind == "auto":
        name = _artist_exact(query)
        kind = "artist" if name else "song"
        if name:
            plan.artist = name

    if kind == "song":
        hits = catalog.search_songs(query, limit=8, allow_variant=plan.variant)
        if plan.artist:
            wanted = plan.artist.lower()
            preferred = [t for t in hits if wanted in (t.artist or "").lower()]
            hits = preferred + [t for t in hits if t not in preferred]
        if not hits:
            return Resolution([], f"I couldn't find {query}", error="no results")
        best = hits[0]
        return Resolution([best], f"Playing {best.title} by {best.artist}",
                          alternates=[t.video_id for t in hits[1:4]])

    if kind == "album":
        tracks = catalog.album_tracks(query, plan.artist)
        if not tracks:
            return Resolution([], f"I couldn't find the album {query}",
                              error="no results")
        who = tracks[0].artist or plan.artist
        return Resolution(tracks, f"Playing {query} by {who}", hold_radio=True)

    if kind == "artist":
        who = plan.artist or query
        tracks = catalog.artist_tracks(who, limit=int(config.get("artist_track_count", 20)))
        if not tracks:
            return Resolution([], f"I couldn't find {who}", error="no results")
        return Resolution(tracks, f"Playing {who}", hold_radio=True)

    if kind == "genre":
        tracks = catalog.genre_tracks(query, limit=25)
        if not tracks:
            return Resolution([], f"I couldn't find anything for {query}",
                              error="no results")
        return Resolution(tracks, f"Playing some {query}")

    return Resolution([], f"I couldn't work out what {query} means", error="unknown")
