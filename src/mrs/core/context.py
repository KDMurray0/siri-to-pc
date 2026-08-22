"""Deciding what could play next.

Builds a scored pool of candidates from several sources so the queue has real
options instead of whatever one radio call returned. Score is deliberately
readable — every term is one idea:

    score = source weight
          + taste (liked / played-through / skipped)
          + cohesion   (same artist as what's playing, scaled by a setting)
          - staleness  (heard recently)
          - derivative (remixes, sped-up, lyric videos)
          + jitter     (so it isn't identical every time)
"""

from __future__ import annotations

import random

from ..config import config
from ..logging_setup import get
from ..models import Candidate, Track, is_derivative
from .taste import taste

log = get("context")

SOURCE_WEIGHT = {
    "artist": 3.0,     # more from the band you're playing
    "radio": 2.0,      # YouTube's related tracks
    "liked": 2.5,      # seeded from something you liked
    "genre": 1.5,
}


class ContextBuilder:
    """Turns 'what's playing' into 'what might play next'."""

    def __init__(self, catalog) -> None:
        self.catalog = catalog

    def build(self, current: Track | None, exclude: set[str] | None = None,
              exclude_keys: set[str] | None = None,
              limit: int = 40) -> list[Candidate]:
        exclude = exclude or set()
        exclude_keys = exclude_keys or set()
        seeds = self._seeds(current)
        raw: list[tuple[Track, str]] = []

        # 1. The current artist's own catalogue — the strongest cohesion signal.
        if current and current.artist:
            for t in self._safe(self.catalog.artist_tracks, current.artist, 30):
                raw.append((t, "artist"))

        # 2. Related tracks seeded from recent context.
        for seed in seeds[:3]:
            for t in self._safe(self.catalog.related, seed, 12):
                raw.append((t, "radio"))

        # 3. Occasionally pull from something you liked, to widen it out.
        if random.random() < 0.35:
            liked = taste.liked_seed()
            if liked:
                for t in self._safe(self.catalog.related, liked, 6):
                    raw.append((t, "liked"))

        out = self._rank(raw, current, exclude, limit, exclude_keys)

        # A thin pool is how the queue used to die: everything got excluded and
        # there was nothing left to download. Widen out before giving up.
        if len(out) < 8:
            out += self._widen(current, exclude, exclude_keys,
                               have={c.track.video_id for c in out})
        if len(out) < 4:
            # Last resort: allow songs heard a while ago rather than run dry.
            out += self._rank(raw, current, exclude, limit, exclude_keys,
                              ignore_recency=True)
            seen, merged = set(), []
            for c in out:
                if c.track.video_id not in seen:
                    seen.add(c.track.video_id)
                    merged.append(c)
            out = merged
        return out[:limit]

    def _widen(self, current, exclude: set[str], exclude_keys: set[str],
               have: set[str]) -> list[Candidate]:
        """Pull in neighbouring artists and your own favourites."""
        raw: list[tuple[Track, str]] = []
        for artist in taste.top_artists(4):
            name = artist.get("artist")
            if name and (not current or name != current.primary_artist()):
                for t in self._safe(self.catalog.artist_tracks, name, 8):
                    raw.append((t, "radio"))
        liked = taste.liked_seed()
        if liked:
            for t in self._safe(self.catalog.related, liked, 10):
                raw.append((t, "liked"))
        if current and current.album:
            for t in self._safe(self.catalog.search_songs,
                                f"{current.primary_artist()} similar artists", 10):
                raw.append((t, "radio"))
        widened = self._rank(raw, current, exclude | have, 25, exclude_keys)
        if widened:
            log.info("pool was thin — widened out to %d more", len(widened))
        return widened

    def for_genre(self, genre: str, limit: int = 25) -> list[Candidate]:
        raw = [(t, "genre") for t in self._safe(self.catalog.genre_tracks, genre, limit)]
        return self._rank(raw, None, set(), limit, set())

    # -- internals -----------------------------------------------------
    def _seeds(self, current: Track | None) -> list[str]:
        seeds: list[str] = []
        if current and current.video_id:
            seeds.append(current.video_id)
        for vid in reversed(taste.history_ids()):
            if vid not in seeds:
                seeds.append(vid)
            if len(seeds) >= 4:
                break
        return seeds

    @staticmethod
    def _safe(fn, *args) -> list[Track]:
        try:
            return fn(*args) or []
        except Exception as exc:
            log.debug("%s failed: %s", getattr(fn, "__name__", fn), exc)
            return []

    def _rank(self, raw: list[tuple[Track, str]], current: Track | None,
              exclude: set[str], limit: int, exclude_keys: set[str],
              ignore_recency: bool = False) -> list[Candidate]:
        cohesion = float(config.get("artist_cohesion", 1.0))
        cur_artist = current.primary_artist() if current else ""
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        out: list[Candidate] = []

        for track, source in raw:
            vid = track.video_id
            if not vid or vid in exclude or vid in seen_ids:
                continue
            key = track.key()
            if key and (key in seen_keys or key in exclude_keys):
                continue
            if is_derivative(track.title):
                continue                      # keep the queue on real releases
            if not ignore_recency and taste.recently_used(track):
                continue

            score = SOURCE_WEIGHT.get(source, 1.0)
            score += taste.score(track)
            if cur_artist and track.primary_artist() == cur_artist:
                score += 3.0 * cohesion
            score += random.random() * 0.8

            seen_ids.add(vid)
            if key:
                seen_keys.add(key)
            track.origin = "radio"
            out.append(Candidate(track=track, score=score, reason=source))

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:limit]
