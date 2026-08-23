"""Play something I'd like.

Built from what you ask for and what you've liked, several seeds at once
rather than one — so it opens on something familiar and doesn't spend an hour
on one band. Needs nothing configured; Last.fm sharpens it.
"""

from __future__ import annotations

import random

from ..logging_setup import get
from ..models import Track
from .taste import taste

log = get("foryou")

WANT = 24              # tracks to hand back
PER_SEED = 6


def _seed_tracks(catalog) -> list[Track]:
    """A spread of starting points: liked songs first, then what you request."""
    seeds: list[Track] = []
    liked = list(taste.liked())
    random.shuffle(liked)
    for row in liked[:4]:
        vid = row.get("video_id")
        if vid:
            seeds.append(Track(video_id=vid, title=row.get("title", ""),
                               artist=row.get("artist", ""), origin="request"))

    for row in taste.top_artists(6):
        name = row.get("artist")
        if not name:
            continue
        try:
            found = catalog.artist_tracks(name, 4) or []
        except Exception:
            continue
        if found:
            seeds.append(random.choice(found))
    random.shuffle(seeds)
    return seeds[:6]


def build(catalog, limit: int = WANT) -> list[Track]:
    """A queue that looks like your listening, not like one song."""
    from .tags import tagstore

    seeds = _seed_tracks(catalog)
    if not seeds:
        return []

    picked: list[Track] = []
    seen: set[str] = set()
    keys: set[str] = set()
    titles: set[str] = set()
    per_artist: dict[str, int] = {}

    def take(t: Track, cap: int = 3) -> bool:
        vid = t.video_id
        if not vid or vid in seen:
            return False
        key = t.key()
        if key and key in keys:
            return False
        # a knockoff channel reuploading Duality is still Duality
        bare = (t.title or "").strip().lower()
        if bare and bare in titles:
            return False
        a = t.primary_artist()
        if a and per_artist.get(a, 0) >= cap:
            return False          # no single band takes over
        if taste.recently_used(t):
            return False
        seen.add(vid)
        if key:
            keys.add(key)
        if bare:
            titles.add(bare)
        if a:
            per_artist[a] = per_artist.get(a, 0) + 1
        t.origin = "radio"
        picked.append(t)
        return True

    # open on something you know, so it doesn't start on a stranger
    opener = 1 if take(seeds[0], cap=2) else 0

    for seed in seeds:
        if not seed.video_id:
            continue
        try:
            near = catalog.related(seed.video_id, PER_SEED + 4) or []
        except Exception:
            near = []
        ranked = sorted(near, key=lambda t: -taste.score(t))
        added = 0
        for t in ranked:
            if added >= PER_SEED:
                break
            if take(t):
                added += 1
        if len(picked) >= limit:
            break

    tagstore.warm(picked)
    tail = picked[opener:]
    random.shuffle(tail)          # shuffle the rest, but keep the opener first
    picked = picked[:opener] + tail
    log.info("for-you queue: %d tracks from %d seeds across %d artists",
             len(picked), len(seeds), len(per_artist))
    return picked[:limit]
