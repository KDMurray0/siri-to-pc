"""Picking what could play next.

score = source + taste + cohesion - staleness + a bit of randomness
"""

from __future__ import annotations

import random
import re

from ..config import config
from ..logging_setup import get
from ..models import Candidate, Track, is_derivative
from .tags import tagstore
from .taste import taste

log = get("context")

SOURCE_WEIGHT = {
    "artist": 2.0,     # the band gets no head start just for being the band
    "radio": 2.0,      # YouTube's related tracks
    "anchor": 2.6,     # radio from the song you actually asked for
    "theme": 2.8,      # the genre or vibe you asked for
    "liked": 2.5,      # seeded from something you liked
    "genre": 1.5,
}


def _fit(sim: float) -> float:
    """Tag similarity -> how much to trust the artist bonus.

    Measured against real data: same album lands ~0.98, the same band a few
    albums later ~0.70, a different band ~0.44.
    """
    return max(0.15, min(1.0, (sim - 0.65) / 0.30))


def _theme_words(theme: str) -> set[str]:
    """The words a track's tags would have to carry to count as this genre."""
    t = (theme or "").strip().lower()
    if not t:
        return set()
    words = {t}
    words.update(w for w in re.split(r"[^a-z0-9]+", t) if len(w) > 2)
    return words


# Tags that say something about the listener rather than the song. Ignored,
# but harmless.
_USELESS_TAGS = ("seen live", "favourite", "favorite", "albums i own",
                 "check out", "spotify")
# Tags left behind by automated tagging. A track carrying these has had its
# tags written by a script, so none of them can be trusted — Taylor Swift's
# Anti-Hero has "grunge" at full weight next to "test-tag" and "automated".
_POISON_TAGS = ("test-tag", "batch-test", "automated", "testtag")


def _matches(want: set[str], tags: dict) -> bool:
    """Does this track really carry the genre, not just mention it?

    A tag has to be one of the track's stronger ones. Half of Last.fm has
    "rock" on it somewhere, and a tag with three votes against a top tag with
    two hundred says nothing.
    """
    if not tags:
        return False
    top = max(tags.values()) or 1
    if any(p in tag for tag in tags for p in _POISON_TAGS):
        return False              # scripted tags; the whole set is worthless
    for tag, count in tags.items():
        if count < top * 0.4 or any(u in tag for u in _USELESS_TAGS):
            continue
        for w in want:
            if w in tag or tag in w:
                return True
    return False


class ContextBuilder:
    """Turns 'what's playing' into 'what might play next'."""

    def __init__(self, catalog) -> None:
        self.catalog = catalog

    def build(self, current: Track | None, exclude: set[str] | None = None,
              exclude_keys: set[str] | None = None,
              limit: int = 40, focus: float = 1.0,
              anchor: Track | None = None, theme: str = "",
              artist_counts: dict[str, int] | None = None) -> list[Candidate]:
        exclude = exclude or set()
        exclude_keys = exclude_keys or set()
        seeds = self._seeds(current)
        raw: list[tuple[Track, str]] = []

        # 1. The current artist's own catalogue. Pull fewer of them when you
        #    only asked for a song — otherwise every request becomes an artist
        #    request.
        if current and current.artist:
            want = 30 if focus >= 0.8 else 10
            for t in self._safe(self.catalog.artist_tracks, current.artist, want):
                raw.append((t, "artist"))

        # 2. Related tracks seeded from recent context.
        for seed in seeds[:3]:
            for t in self._safe(self.catalog.related, seed, 12 if focus >= 0.8 else 18):
                raw.append((t, "radio"))

        # 3. Radio from the song that started this, so the pool keeps being
        #    offered things that sound like what was actually asked for.
        #    Re-ranking can only reorder what it's given, and by track 100
        #    everything on offer is whatever the last track dragged in.
        if anchor is not None and anchor.video_id and float(config.get("anchor_pull", 0.35)):
            here = tagstore.similarity(anchor, current)
            if here is None or here < 0.75:      # only once it's actually strayed
                for t in self._safe(self.catalog.related, anchor.video_id, 12):
                    raw.append((t, "anchor"))

        # 4. If a genre was asked for, keep drawing from it. Twenty-five
        #    tracks run out in an hour and a half; the radio should still be
        #    playing that genre afterwards, not whatever it wandered into.
        if theme:
            for t in self._safe(self.catalog.genre_tracks, theme, 20):
                raw.append((t, "theme"))

        # 5. Occasionally pull from something you liked, to widen it out.
        if random.random() < 0.35:
            liked = taste.liked_seed()
            if liked:
                for t in self._safe(self.catalog.related, liked, 6):
                    raw.append((t, "liked"))

        tagstore.warm([t for t, _ in raw])     # next refill will know more
        out = self._rank(raw, current, exclude, limit, exclude_keys, focus=focus,
                         anchor=anchor, theme=theme, artist_counts=artist_counts)

        # a thin pool is how the queue used to die — widen out first
        if len(out) < 8:
            out += self._widen(current, exclude, exclude_keys,
                               have={c.track.video_id for c in out}, focus=focus)
        if len(out) < 4:
            # Last resort: allow songs heard a while ago rather than run dry.
            out += self._rank(raw, current, exclude, limit, exclude_keys,
                              ignore_recency=True, focus=focus, anchor=anchor,
                              theme=theme, artist_counts=artist_counts)
            seen, merged = set(), []
            for c in out:
                if c.track.video_id not in seen:
                    seen.add(c.track.video_id)
                    merged.append(c)
            out = merged
        return out[:limit]

    def _widen(self, current, exclude: set[str], exclude_keys: set[str],
               have: set[str], focus: float = 1.0) -> list[Candidate]:
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
        widened = self._rank(raw, current, exclude | have, 25, exclude_keys,
                             focus=focus)
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
              ignore_recency: bool = False, focus: float = 1.0,
              anchor: Track | None = None, theme: str = "",
              artist_counts: dict[str, int] | None = None) -> list[Candidate]:
        # Skipping late, again and again, means the run has gone stale rather
        # than any one song being wrong. Loosen the grip a little when that
        # happens — at worst it halves.
        slack = 1.0 - min(0.5, taste.fatigue() * 0.18)
        cohesion = float(config.get("artist_cohesion", 1.0)) * focus * slack
        cur_artist = current.primary_artist() if current else ""
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        # Ask for one song and you get one song by that band, not their
        # discography. Artist and album requests want the opposite.
        stack_penalty = 0.0 if focus >= 0.8 else 1.6
        # count what this session already played, not just this pool — an
        # artist that got six tracks an hour ago has had its turn
        per_artist: dict[str, int] = dict(artist_counts or {})
        # How far this session has already wandered from what was asked for.
        # Nothing to correct at the start; the further out it gets, the more
        # the opening track is allowed to argue.
        want = _theme_words(theme)
        pull = float(config.get("anchor_pull", 0.35)) if anchor is not None else 0.0
        drift = 0.0
        if pull:
            here = tagstore.similarity(anchor, current)
            drift = 0.0 if here is None else max(0.0, 1.0 - here)
        pull = min(0.8, pull * (0.5 + drift))
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

            # How much this actually sounds like what's playing. None until the
            # tag cache catches up, in which case fall back to artist-only.
            sim = tagstore.similarity(current, track)
            # Every step being close to the last one still walks a long way:
            # Song 2 reached Ed Sheeran in 180 tracks, each hop reasonable. So
            # blend in how much this sounds like the song that started it —
            # everything downstream then works off that instead.
            if pull:
                back = tagstore.similarity(anchor, track)
                if back is not None:
                    sim = back if sim is None else (1 - pull) * sim + pull * back
            if sim is None:
                # no tags yet: trust the band on an artist request, stay wary
                # on a song request until the cache catches up
                fit = 1.0 if focus >= 0.8 else 0.5
            else:
                fit = _fit(sim)

            ts = taste.score(track)
            if ts > 0 and sim is not None:
                ts *= 0.5 + 0.5 * fit      # you replay it, but is it this mood
            score += ts

            # What people play alongside this, which is the only signal that
            # can tell Song 2 from The Universal — tags call those 0.86 alike
            # because tags describe Blur, not the song.
            near = tagstore.affinity(anchor or current, track)
            if near:
                score += 3.0 * near

            if cur_artist and track.primary_artist() == cur_artist:
                # A nudge, not a rule. Tags can't separate two songs by one
                # band — Song 2 and Girls & Boys read as the same britpop — so
                # lean on whether people actually play them together. Song 2's
                # neighbours are Beetlebum and Parklife; Girls & Boys isn't
                # among them, and it shouldn't ride in on the band name.
                if near is None:
                    kin = fit               # no neighbour data yet
                else:
                    kin = 0.2 + min(0.8, near * 2.5)
                score += 0.9 * cohesion * kin
                if current and current.album and track.album == current.album:
                    score += 0.8 * cohesion      # same record, same era
            if sim is not None:
                score += 3.0 * (sim - 0.55)      # genre and mood lead

            # You asked for a genre, so the genre is the brief. Tracks whose
            # own tags say they belong get a real push; ones we can't confirm
            # only drift in if nothing better is going.
            # Ask for a genre and you get that genre, full stop. Anything
            # drawn from the genre itself qualifies by construction; anything
            # else has to prove it. Scoring it down wasn't enough — a low
            # score still plays once the good ones run out.
            # Everything gets checked, including tracks the genre lookup
            # itself supplied — that list is part Apple search now, and it
            # offered Blur's Tender for grunge.
            if want:
                tags = tagstore.get(track)
                if not tags or not _matches(want, tags):
                    continue
                score += 3.5
            score -= stack_penalty * per_artist.get(track.primary_artist(), 0)
            score += random.random() * 0.8

            seen_ids.add(vid)
            if key:
                seen_keys.add(key)
            a = track.primary_artist()
            if a:
                per_artist[a] = per_artist.get(a, 0) + 1
            track.origin = "radio"
            out.append(Candidate(track=track, score=score, reason=source))

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:limit]
