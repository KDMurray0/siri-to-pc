"""Plan -> actual tracks."""

from __future__ import annotations

import time

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



def _nothing(said: str) -> "Resolution":
    """Nothing came back — but say which kind of nothing it was.

    YouTube's circuit breaker returns None for everything while it's open,
    and reporting that as "I couldn't find it" is a lie that sends you off
    rewording a query that was fine.
    """
    wait = catalog.throttled()
    if wait:
        # This gets spoken back, so it reads as a sentence rather than a
        # status code.
        return Resolution([], f"YouTube is throttling us. Try again in about "
                              f"{int(wait) + 1} seconds.", error="throttled")
    return Resolution([], said, error="no results")


def _first_minutes(tracks: list[Track], minutes: float) -> list[Track]:
    """Take roughly `minutes` worth off the front of a track list."""
    budget = minutes * 60
    out: list[Track] = []
    for t in tracks:
        out.append(t)
        budget -= (t.duration or 210)
        if budget <= 0:
            break
    return out or tracks[:8]


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
            return _nothing(f"Nothing found on {source}")
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
            return _nothing(f"I couldn't find {query}")
        best = hits[0]
        return Resolution([best], f"Playing {best.title} by {best.artist}",
                          alternates=[t.video_id for t in hits[1:4]])

    if kind == "album":
        tracks = catalog.album_tracks(query, plan.artist)
        if not tracks:
            return _nothing(f"I couldn't find the album {query}")
        who = tracks[0].artist or plan.artist
        return Resolution(tracks, f"Playing {query} by {who}", hold_radio=True)

    if kind == "artist":
        who = plan.artist or query
        # Asking for a band plays the band: the whole catalogue, and only when
        # it runs out does the radio take over (hold_radio).
        tracks = catalog.artist_all_tracks(who)
        if not tracks:
            return _nothing(f"I couldn't find {who}")
        # Queue about half an hour of them rather than the whole discography;
        # the queue tops itself up from the same catalogue as you listen.
        tracks = _first_minutes(tracks, float(config.get("queue_minutes", 30)))
        return Resolution(tracks, f"Playing {who}", hold_radio=True)

    if kind == "genre":
        tracks = _on_theme(query, catalog.genre_tracks(query, limit=25))
        if not tracks:
            return _nothing(f"I couldn't find anything for {query}")
        return Resolution(tracks, f"Playing some {query}")

    return Resolution([], f"I couldn't work out what {query} means", error="unknown")


def _on_theme(genre: str, tracks: list[Track]) -> list[Track]:
    """On-genre first, obvious misses dropped.

    A grunge request came back with Thong Song at number one, which then
    became the anchor. Track one matters twice over, so verified ones go
    first. Unknowns are kept but demoted — with no Last.fm key nothing
    changes.
    """
    from ..core.context import _matches, _theme_words
    from ..core.tags import tagstore

    want = _theme_words(genre)
    if not want or not tracks or not tagstore.enabled():
        return tracks

    tagstore.warm(tracks)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if all(tagstore.get(t) is not None for t in tracks):
            break
        time.sleep(0.25)

    good, unknown, bad = [], [], []
    for t in tracks:
        tags = tagstore.get(t)
        if not tags:
            unknown.append(t)
        elif _matches(want, tags):
            good.append(t)
        else:
            bad.append(f"{t.title} — {t.artist}")
    if bad:
        log.info("%s: dropped %d off-genre (%s)", genre, len(bad),
                 "; ".join(bad[:4]))
    if not good:
        return tracks          # tags told us nothing useful; leave it alone
    # Zero drift when we can manage it: once there are enough confirmed
    # tracks, the ones we couldn't check are dropped rather than trusted.
    # Anti-Hero got into a grunge queue by being unverified, not by being
    # wrong-but-close.
    if len(good) >= 8:
        if unknown:
            log.info("%s: also dropped %d unverified", genre, len(unknown))
        return good
    return good + unknown
