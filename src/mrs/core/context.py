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
            for t in self._safe(self.catalog.artist_tracks, current.artist, 12):
                raw.append((t, "artist"))

        # 2. Related tracks seeded from recent context.
        for seed in seeds[:3]:
            for t in self._safe(self.catalog.related, seed, 10):
                raw.append((t, "radio"))

        # 3. Occasionally pull from something you liked, to widen it out.
        if random.random() < 0.35:
            liked = taste.liked_seed()
            if liked:
                for t in self._safe(self.catalog.related, liked, 6):
                    raw.append((t, "liked"))

        return self._rank(raw, current, exclude, limit, exclude_keys)

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
              exclude: set[str], limit: int,
              exclude_keys: set[str]) -> list[Candidate]:
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
            if taste.recently_used(track):
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
