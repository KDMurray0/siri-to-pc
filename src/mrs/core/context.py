"""Picking what could play next.

score = source + taste + cohesion - staleness + a bit of randomness
"""

from __future__ import annotations

import random
import re

from ..config import config
from ..logging_setup import get
from ..models import (Candidate, Track, is_channel_act, is_derivative,
                      norm_title)
from .tags import tagstore
from .taste import taste

log = get("context")

# Taste is worth about a third of the genre term at most, on purpose.
TASTE_WEIGHT = 1.0
# Nothing vouches for it: no tags, nobody plays it alongside anything, and it
# isn't by the band that's on. Usually a re-upload or a soundalike.
UNKNOWN_PENALTY = 1.2

SOURCE_WEIGHT = {
    "artist": 2.0,     # the band gets no head start just for being the band
    "radio": 2.0,      # YouTube's related tracks
    "anchor": 2.6,     # radio from the song you actually asked for
    "theme": 2.8,      # the genre or vibe you asked for
    "liked": 1.6,      # seeded from something you liked, so it has to fit too
    # Real records in the genre the asked-for song sits in. Set to land level
    # with anchor once the discount below is applied, because which of the two
    # is better depends entirely on the genre and there's no telling in
    # advance: YouTube's related list is superb for Fela Kuti and useless for
    # Bikini Kill, and Last.fm's tag is the other way round. Level pegging
    # means the pool always holds some of each.
    "root": 2.0,
    "genre": 1.5,
}
# Last.fm has been asked and has never heard of them. Every real act has a
# page, so what's left is AI piano and stock-library uploads.
NOBODY_PENALTY = 2.5


def _fit(sim: float) -> float:
    """Tag similarity -> how much to trust the artist bonus.

    Measured against real data: same album lands ~0.98, the same band a few
    albums later ~0.70, a different band ~0.44.
    """
    return max(0.15, min(1.0, (sim - 0.65) / 0.30))


def _dash_credit(title: str) -> bool:
    """"Artist - Song" typed into the title, which the catalogue never does.

    Real rows keep the artist in its own field and use a colon for movements —
    "Debussy: Preludes", "Carnival of the Animals: XIII". A dash means somebody
    typed the lot into an upload box, so it's a re-upload of someone else's
    record. Only held against tracks nothing else vouches for.
    """
    return " - " in (title or "")


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
        seeds = self._seeds(current, anchor)
        raw: list[tuple[Track, str]] = []

        # 1. The current artist's own catalogue. Pull fewer of them when you
        #    only asked for a song — otherwise every request becomes an artist
        #    request.
        if current and current.artist:
            want = 30 if focus >= 0.8 else 10
            # Only for a band that belongs here. Something generic drifts in,
            # and pulling its catalogue turns one stray track into six — the
            # radio ends up playing an artist you never asked for because it
            # played them once. Unknown still gets the benefit of the doubt;
            # a measured bad fit doesn't.
            here = tagstore.similarity(anchor, current) if anchor is not None else None
            if focus >= 0.8 or here is None or here >= 0.45:
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

        # 4b. No genre was asked for, but the song you asked for has one, so
        #     keep a lane of real records open. YouTube's related list for
        #     solo piano is almost entirely AI uploads — thirty tracks off
        #     Nuvole Bianche and not one is by anybody who exists. Re-ranking
        #     can only reorder what it's given, so it has to be given better.
        #     It also has to be deep. The anchor's own related list is about
        #     twenty videos long and the exclusions eat it in ten tracks —
        #     that's why a Fela Kuti run reached Depeche Mode by track 30,
        #     with nothing pulling back once the anchor lane ran dry.
        root = ""
        if not theme and anchor is not None:
            root = tagstore.top_tag(anchor)
            if not root:
                tagstore.prime(anchor)      # worth one blocking call
                root = tagstore.top_tag(anchor)
            if root:
                # Deep on purpose. Thirty gets eaten by track sixteen and then
                # a riot grrrl run has nothing left to pull on and slides into
                # pop-punk; the lookup is cached, so depth is nearly free.
                fresh = self._safe(self.catalog.genre_tracks, root, 60)
                raw.extend((t, "root") for t in fresh)

        # 5. Occasionally pull from something you liked, to widen it out — but
        #    only when nothing was actually asked for. One of five likes being
        #    a piano piece shouldn't mean one refill in three of a metal queue
        #    arrives full of it.
        if anchor is None and not theme and random.random() < 0.35:
            liked = taste.liked_seed()
            if liked:
                for t in self._safe(self.catalog.related, liked, 6):
                    raw.append((t, "liked"))

        tagstore.warm([t for t, _ in raw])     # next refill will know more
        out = self._rank(raw, current, exclude, limit, exclude_keys, focus=focus,
                         anchor=anchor, theme=theme, root=root,
                         artist_counts=artist_counts)

        # a thin pool is how the queue used to die — widen out first
        if len(out) < 8:
            out += self._widen(current, exclude, exclude_keys,
                               have={c.track.video_id for c in out}, focus=focus)
        if len(out) < 4:
            # Last resort: allow songs heard a while ago rather than run dry.
            out += self._rank(raw, current, exclude, limit, exclude_keys,
                              ignore_recency=True, focus=focus, anchor=anchor,
                              theme=theme, root=root, artist_counts=artist_counts)
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

    # -- internals -----------------------------------------------------
    def _seeds(self, current: Track | None,
               anchor: Track | None = None) -> list[str]:
        """Tracks worth asking YouTube for a continuation from.

        History used to go in unfiltered, and that's how a hip-hop request came
        back full of ambient piano: the last three things you heard seeded the
        pool regardless of what you'd just asked for, so an hour-old session
        kept steering the new one. A past track only earns a seed now if it
        sounds like what's on. Unknown counts as no.
        """
        seeds: list[str] = []
        if current and current.video_id:
            seeds.append(current.video_id)
        ref = anchor or current
        if ref is None:
            return seeds
        for row in taste.recent(12):
            vid = row.get("video_id")
            if not vid or vid in seeds:
                continue
            past = Track(video_id=vid, title=row.get("title") or "",
                         artist=row.get("artist") or "")
            if (tagstore.similarity(ref, past) or 0.0) < 0.70:
                continue
            seeds.append(vid)
            if len(seeds) >= 3:
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
              anchor: Track | None = None, theme: str = "", root: str = "",
              artist_counts: dict[str, int] | None = None) -> list[Candidate]:
        # Skipping late, again and again, means the run has gone stale rather
        # than any one song being wrong. Loosen the grip a little when that
        # happens — at worst it halves.
        slack = 1.0 - min(0.5, taste.fatigue() * 0.18)
        cohesion = float(config.get("artist_cohesion", 1.0)) * focus * slack
        cur_artist = current.primary_artist() if current else ""
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        # Seeded from what's already been on, not just what's in this pool.
        # The artist+title dedupe lets a different act's version straight
        # through, so a Marvin Gaye request played Ain't No Mountain High
        # Enough again four tracks later, by Diana Ross.
        seen_titles: set[str] = {norm_title(r.get("title") or "")
                                 for r in taste.recent(60)}
        seen_titles.discard("")
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
        root_words = _theme_words(root)
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
                # Not "a bit different" — nothing whatsoever in common. Scoring
                # that down wasn't enough, because once the disco ran out a
                # track on 0.35 won by default and Bee Gees was followed by
                # a-ha. Better to widen the search than to play it.
                if sim < 0.12 and source not in ("theme", "genre"):
                    continue

            # Taste breaks ties, it doesn't choose. Liking a song is a reason
            # to prefer it over something equally fitting — not a reason to
            # play it in a queue it has no business in. A dislike still bites.
            ts = taste.score(track)
            score += (TASTE_WEIGHT * ts * fit) if ts > 0 else (TASTE_WEIGHT * ts)

            # What people play alongside this, which is the only signal that
            # can tell Song 2 from The Universal — tags call those 0.86 alike
            # because tags describe Blur, not the song.
            near = tagstore.affinity(anchor or current, track)
            if near:
                score += 3.0 * near

            # Whether it follows the record that's actually on, which is a
            # different question from whether it suits the request. With an
            # anchor set the line above only ever asked about the anchor, so
            # nothing was judging the join between one track and the next —
            # the thing a DJ is actually doing.
            flow = (tagstore.affinity(current, track)
                    if anchor is not None and current is not anchor else None)
            if flow:
                score += 1.5 * flow

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
            elif not near and not flow and track.primary_artist() != cur_artist:
                # Nobody has tagged it and nobody plays it next to anything —
                # that's what a Thunderstruck cover by a stranger looks like.
                # Double it if the title credits somebody in the title itself:
                # unknown and hand-typed is a re-upload nearly every time.
                #
                # A genre you asked for by name is exempt; the one we guessed
                # from the song only gets a discount. Exempting that outright
                # handed it a point and a bit over every other lane, and since
                # most tracks are untagged when the pool is built, it quietly
                # became the only lane that ever won — a Fela Kuti queue made
                # of Drake and Selena Gomez.
                soft = {"theme": 0.0, "genre": 0.0, "root": 0.5}.get(source, 1.0)
                if soft:
                    score -= (UNKNOWN_PENALTY * soft
                              * (2.0 if _dash_credit(track.title) else 1.0))
            if tagstore.nobody(track) and track.primary_artist() != cur_artist:
                score -= NOBODY_PENALTY

            # The implicit-genre lane is only as good as the tag it drew
            # from, and Last.fm's afrobeat tag has K-pop filed under it.
            # A track that came from that lane and carries tags saying it
            # belongs to something else is exactly what it looks like.
            if source == "root" and root_words:
                own = tagstore.get(track)
                if own and not _matches(root_words, own):
                    continue

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

            if is_channel_act(track.artist, track.title):
                continue

            # One version of a song is enough. The pool routinely holds the
            # real Thunderstruck and three covers of it; they all pass the
            # artist+title dedupe because the artist differs.
            bare = norm_title(track.title)
            if bare and bare in seen_titles:
                continue
            seen_ids.add(vid)
            if bare:
                seen_titles.add(bare)
            if key:
                seen_keys.add(key)
            a = track.primary_artist()
            if a:
                per_artist[a] = per_artist.get(a, 0) + 1
            track.origin = "radio"
            out.append(Candidate(track=track, score=score, reason=source))

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:limit]
