"""Picking what could play next.

score = source + taste + cohesion - staleness + a bit of randomness
"""

from __future__ import annotations

import random
import re
import time

from ..config import config
from ..logging_setup import get
from ..models import (Candidate, Track, is_channel_act, is_derivative,
                      norm_title, _fold,
                      _strip_article)
from .era import era, gap as era_gap
from .kin import kin as kinstore
from .tags import _CATCH_ALL, _flatten, tagstore
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
    # The two editorial lanes. Both sit above a genre lane that has matched
    # the label and collected PRIMARY_GENRE for it, because a shared label is
    # a weaker claim than either of these makes and it kept winning by a
    # tenth of a point: Pyramid Song is tagged alternative rock, and so are
    # Hoobastank, Staind, Creed and Hinder.
    #
    # Records by the artists Deezer files next to this one. Collects
    # KIN_WEIGHT on top, so 3.6 in total.
    "kin": 2.2,
    # The records people play alongside this one, asked for by name. The most
    # precise thing here — not a guess from a label, thirty tracks somebody's
    # listening history actually put together.
    "near": 3.6,
}
# Last.fm has been asked and has never heard of them. Every real act has a
# page, so what's left is AI piano and stock-library uploads.
NOBODY_PENALTY = 2.5
# Carrying the genre that was actually asked for, rather than the second one
# we accept to keep the pool from running dry. Enough to lead, not enough to
# shut the second tag out.
PRIMARY_GENRE = 1.1
# Half a century between two artists, in a genre that's run continuously for
# all of it. Tags can't see this at all — Jolene and Morgan Wallen are both
# country and nothing else in the data disagrees.
ERA_WEIGHT = 2.0
# Deezer says these two acts belong together. Coarser than Last.fm's
# track-level affinity but present for everybody, where affinity is usually
# silent — a Dolly Parton request knows Loretta Lynn and Willie Nelson belong
# without anyone having tagged the individual songs.
KIN_WEIGHT = 1.4


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


RUN_LOOK_BACK = 10        # how much of the recent run a candidate has to suit
# Tempo is deliberately not part of this. Measured against the case it was
# meant to fix — a metal run sagging into Nickelback — Deezer's bpm turned out
# anti-correlated with it: Creed's ballad sits at 127 next to Chop Suey's 129
# and scores a near-perfect match, while Down with the Sickness reads 180 and
# scores zero. Speed isn't weight. It still sets the crossfade length, which
# is what it's actually good for.


def _run_fit(run: list[Track], track: Track) -> float:
    """How well this sits with the last few records, 0 to 1.

    The newest counts most, but the older ones still get a say — that's the
    point. One song only has to follow the song before it, so a chain of
    perfectly reasonable steps still walks from Fela Kuti to Depeche Mode.
    Asking it to suit ten at once leaves nowhere for that walk to go.

    Averaging the *tag* similarity over the same ten was tried and made drift
    worse, not better: the average is the run's own centre, which moves with
    the run, so it anchors to nothing and quietly penalises anything that
    still sounds like the song you asked for. Similarity stays measured
    against what's on and against the anchor, which don't move.
    """
    total = weight = 0.0
    for i, past in enumerate(run):
        w = 1.0 - (i * 0.07)          # newest first, gently decayed
        val = tagstore.affinity(past, track)
        if val is None:
            continue                  # not looked up yet; don't count it
        total += w * val
        weight += w
    return (total / weight) if weight else 0.0


def _carries(tag: str, tags: dict) -> bool:
    """Is this actually filed under that genre, by that exact name?

    Exact, not "contains" — the whole point is that post-grunge is not grunge
    and country is not classic country, and a substring test calls both of
    those a match. Has to be one of the track's real tags too, not a mention.
    """
    if not tag or not tags:
        return False
    want = _flatten(tag)
    top = max(tags.values()) or 1
    return any(count >= top * 0.25 and _flatten(t) == want
               for t, count in tags.items())


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
        roots: list[str] = []
        if anchor is not None:
            # One lookup each, cached for good, and both comparisons are
            # against this artist — without them there's nothing to compare to.
            self._safe(era.prime, anchor)
            self._safe(kinstore.prime, anchor)
            self._safe(tagstore.prime_near, anchor)
        if not theme and anchor is not None:
            roots = tagstore.top_tags(anchor, 2)
            if not roots:
                tagstore.prime(anchor)      # worth one blocking call
                roots = tagstore.top_tags(anchor, 2)
            # Deep on purpose, and from both tags. Thirty gets eaten by track
            # sixteen and then a riot grrrl run has nothing left to pull on and
            # slides into pop-punk. One tag isn't enough either: everything
            # Chop Suey has in common with Nickelback is the word "alternative
            # metal", and once the heavy records in that tag are used up the
            # soft ones are all that's left. Its second tag is nu metal, which
            # is ninety more of the right thing. The lookups are cached, so
            # depth is nearly free.
            for i, tag in enumerate(roots):
                for t in self._safe(self.catalog.genre_tracks, tag,
                                    90 if i == 0 else 60):
                    raw.append((t, "root"))

        # 4c. The artists a human would file next to this one. A tag lane can
        #     only fetch things wearing the same label, and the label is often
        #     wrong about who belongs together: Billie Jean is tagged pop, and
        #     the pop lane fetches Dua Lipa. Deezer's neighbours for Michael
        #     Jackson are Stevie Wonder, Prince, Diana Ross and Whitney
        #     Houston, which is the queue somebody would actually build.
        #     A rotating handful each refill, so the whole neighbourhood gets
        #     played over a long run instead of the same four names.
        if not theme and anchor is not None:
            neighbours = self._safe(kinstore.related, anchor) or []
            if neighbours:
                pick = neighbours[:8]
                random.shuffle(pick)
                for name in pick[:4]:
                    for t in self._safe(self.catalog.artist_tracks, name, 6):
                        raw.append((t, "kin"))

        # 4d. The records people actually play alongside this one. Last.fm's
        #     similar-track list is the most precise signal here — it's the
        #     only one that knows Song 2 sits with Beetlebum rather than with
        #     The Universal — and it was only ever used to score tracks some
        #     other lane had already turned up. Pyramid Song's list is Thom
        #     Yorke, Jeff Buckley, Muse and Pixies; the alternative rock lane
        #     fetched Goo Goo Dolls, Hoobastank and Three Days Grace, and
        #     with nothing to fetch the right records there was nothing to
        #     rank. Asking for them by name costs six searches, cached.
        if not theme and anchor is not None:
            mine = anchor.primary_artist()
            rows = sorted(tagstore.neighbours(anchor).items(),
                          key=lambda kv: -kv[1])
            picked = tried = 0
            for pair, _match in rows:
                # Six new ones, but keep walking past the ones already played
                # — the list belongs to the anchor and doesn't change, so
                # offering the same top six every refill means the lane dries
                # up after six tracks with twenty-four still on it. Searches
                # are cached, so walking further is nearly free.
                if picked >= 6 or tried >= 14:
                    break
                who, _, name = pair.partition("|")
                # Skip the anchor's own back catalogue: it tops every list,
                # it would use the whole allowance, and the artist lane has
                # it covered anyway.
                theirs = _strip_article(_fold(who))
                if not name or not theirs or theirs == mine:
                    continue
                found = self._safe(self.catalog.search_songs,
                                   f"{name} {who}", 1)
                tried += 1
                # Search drifts. Only take it if it's the record we asked for.
                if not found or found[0].primary_artist() != theirs:
                    continue
                hit = found[0]
                if hit.video_id in exclude or hit.key() in exclude_keys:
                    continue
                raw.append((hit, "near"))
                picked += 1

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
        era.warm([t for t, _ in raw])
        kinstore.warm([t for t, _ in raw])
        out = self._rank(raw, current, exclude, limit, exclude_keys, focus=focus,
                         anchor=anchor, theme=theme, roots=roots,
                         artist_counts=artist_counts)

        # The first pass ranks on whatever happens to be cached, which means
        # the checks that need tags — the similarity floors especially — are
        # blind to the tracks that matter most. So look the leaders up and
        # rank again knowing what they are. Only the leaders, because it
        # blocks; the rest can wait for the worker.
        #
        # And keep going until the leaders stop changing. Looking up the top
        # eight and stopping is whack-a-mole: it demoted the eight it knew
        # about and handed the queue to the ninth, which is how a stock-
        # library upload called "Psychedelic Soul Train" — by an act Last.fm
        # has never heard of, so nothing could object — came sixth in a
        # Childish Gambino run.
        #
        # Eras go in the same loop and are the reason for the deadline:
        # MusicBrainz is paced at a request a second, so from a cold cache
        # the background worker is forty seconds behind and the opening picks
        # get made with the era check switched off. A Billie Jean queue led
        # with Miley Cyrus, Ariana Grande and Britney Spears, then settled
        # into the eighties once the lookups landed. A refill runs while a
        # record is playing so a few seconds is affordable; a stalled lookup
        # is not.
        stop = time.monotonic() + 8.0
        want_era = anchor is not None and era.get(anchor)
        for _ in range(3):
            looked = 0
            for c in out[:8]:
                if time.monotonic() >= stop:
                    break
                if tagstore.get(c.track) is None:
                    self._safe(tagstore.prime, c.track)
                    looked += 1
                if want_era and not era.known(c.track):
                    self._safe(era.prime, c.track)
                    looked += 1
            if not looked:
                break
            out = self._rank(raw, current, exclude, limit, exclude_keys,
                             focus=focus, anchor=anchor, theme=theme, roots=roots,
                             artist_counts=artist_counts)

        # The genre-name test is strict on purpose, but some tags barely exist
        # at track level. Ask for drum and bass and it leaves Goldie's own back
        # catalogue and almost nothing else — twenty-three of thirty by one
        # man, which is a worse night than the drift it was there to stop. If
        # it's squeezed the field down to one artist, drop it and take the
        # wider one.
        def _squeezed(cands: list[Candidate]) -> bool:
            if len(cands) < 6:
                return True
            counts: dict[str, int] = {}
            for c in cands:
                a = c.track.primary_artist()
                counts[a] = counts.get(a, 0) + 1
            return max(counts.values()) > len(cands) * 0.5

        if out and roots and _squeezed(out):
            # Two rungs, not a cliff. The narrow tag is the one we want, but
            # going straight from "must be new wave" to "anything at all"
            # throws away the middle step — must at least be electronic —
            # which is still worth standing on.
            narrow = roots[:1] + [r for r in roots[1:] if r not in _CATCH_ALL]
            rungs = [] if narrow == list(roots) else [dict(wide_gate=True)]
            rungs.append(dict(strict=False))
            for kw in rungs:
                wider = self._rank(raw, current, exclude, limit, exclude_keys,
                                   focus=focus, anchor=anchor, theme=theme,
                                   roots=roots, artist_counts=artist_counts,
                                   **kw)
                if len(wider) > len(out):
                    out = wider
                if not _squeezed(out):
                    break

        # a thin pool is how the queue used to die — widen out first
        if len(out) < 8:
            out += self._widen(current, exclude, exclude_keys,
                               have={c.track.video_id for c in out}, focus=focus)
        if len(out) < 4:
            # Last resort: allow songs heard a while ago rather than run dry.
            out += self._rank(raw, current, exclude, limit, exclude_keys,
                              ignore_recency=True, focus=focus, anchor=anchor,
                              theme=theme, roots=roots, artist_counts=artist_counts)
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
              anchor: Track | None = None, theme: str = "",
              roots: list[str] | None = None, strict: bool = True,
              wide_gate: bool = False,
              artist_counts: dict[str, int] | None = None) -> list[Candidate]:
        # Skipping late, again and again, means the run has gone stale rather
        # than any one song being wrong. Loosen the grip a little when that
        # happens — at worst it halves.
        slack = 1.0 - min(0.5, taste.fatigue() * 0.18)
        cohesion = float(config.get("artist_cohesion", 1.0)) * focus * slack
        cur_artist = current.primary_artist() if current else ""
        anchor_artist = anchor.primary_artist() if anchor else ""
        anchor_era = era.get(anchor) if anchor is not None else None
        anchor_kin = kinstore.related(anchor) if anchor is not None else []
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        # Seeded from what's already been on, not just what's in this pool.
        # The artist+title dedupe lets a different act's version straight
        # through, so a Marvin Gaye request played Ain't No Mountain High
        # Enough again four tracks later, by Diana Ross.
        seen_titles: set[str] = {norm_title(r.get("title") or "")
                                 for r in taste.recent(60)}
        seen_titles.discard("")
        # The last few records, newest first, for the run-fit tests below.
        # What's on hasn't been recorded yet, so it goes on the front itself.
        recent_run = [Track(video_id=r.get("video_id") or "",
                            title=r.get("title") or "",
                            artist=r.get("artist") or "")
                      for r in taste.recent(RUN_LOOK_BACK)]
        run_now = ([current] if current else []) + [
            t for t in recent_run
            if not current or t.video_id != current.video_id]
        run_now = run_now[:RUN_LOOK_BACK]
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
        # Either tag counts. A nu metal record that never picked up the
        # alternative metal tag still belongs in the lane it came from.
        # A broad tag in second place can find records but it can't vouch for
        # them. Teardrop is trip-hop and electronic, and the second one let
        # the gate below wave through LMFAO, Calvin Harris and Avicii — all
        # electronic, none trip-hop, and the last four tracks of the run.
        #
        # The first tag always vouches, broad or not. It's the answer to what
        # the song is: Johnny Cash is country before he's anything else, and
        # dropping country for being a common word left Ring of Fire gated on
        # classic rock.
        gate = list(roots or []) if wide_gate else (
            (roots or [])[:1] + [r for r in (roots or [])[1:]
                                 if r not in _CATCH_ALL])
        root_words: set[str] = set()
        for r in (roots or []):
            root_words |= _theme_words(r)
        searched = {_flatten(t) for t in ([theme] if theme else []) + list(roots or [])
                    if t}
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
            # Somebody other than the tag cache says this belongs here
            vouched = source == "near"

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
                if sim < 0.20 and source != "theme":
                    continue

            # And the same test against the song you asked for, which is the
            # one reference that doesn't move. Measuring only against what's on
            # is how the tail of a run goes: blink-182 scores well after Fall
            # Out Boy and Fall Out Boy scored well after the thing before it,
            # and thirty steps like that leave a metal request playing pop
            # punk. Against Chop Suey blink-182 is 0.09, and that settles it.
            if anchor is not None and source != "theme":
                far = tagstore.similarity(anchor, track)
                if far is not None and far < 0.18:
                    continue
                # And it has to be the same genre by name, not merely close on
                # the numbers. Grunge and post-grunge score 0.55 against each
                # other because they share rock, alternative rock and hard
                # rock — which is how a Pearl Jam request reached Nickelback
                # and Hoobastank by track 25 with nothing looking wrong. The
                # band that's on is exempt; so is anything we've no tags for.
                own = tagstore.get(track)
                # The kin lane is exempt: its whole point is that the label
                # disagrees. Stevie Wonder is soul and Billie Jean is pop, and
                # a name check drops him — which is the one call Deezer got
                # right and the tags got wrong. The similarity floors above
                # still apply, so it can't let nonsense through.
                if (strict and roots and source not in ("kin", "near")
                        and track.primary_artist() != anchor_artist):
                    # No tags is not a free pass. Every guard here needs them,
                    # so one untagged upload gets in and all three go quiet at
                    # once — a Take Five run was faultless for nineteen tracks,
                    # picked up an untagged "Private Jazz Piano" upload, and
                    # spent the last ten in South African house with nothing
                    # able to object. If we know what was asked for, a track
                    # has to say what it is.
                    # Either of its genres will do. Cool jazz on its own is a
                    # small tag, and insisting on it gave thirty tracks by
                    # four people; jazz piano lets the rest of the room in.
                    if not own or not any(_carries(r, own) for r in gate):
                        continue
                    # Equal footing goes too far the other way, though. Tarkus
                    # is progressive rock and classic rock, and letting the
                    # second stand in for the first filled a prog request with
                    # the Eagles, Survivor and Deep Purple — no Yes, no
                    # Genesis, no King Crimson. The first tag is the one that
                    # was asked for, so carrying it is worth something.
                    if _carries(gate[0], own):
                        score += PRIMARY_GENRE

                # Same genre, different half-century. Only bites once both
                # artists are known, and not at all under twenty years apart.
                if anchor_era:
                    score -= ERA_WEIGHT * era_gap(anchor_era, era.get(track))
                if kinstore.is_kin(anchor_kin, track):
                    vouched = True
                    score += KIN_WEIGHT

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
                vouched = True
                score += 3.0 * near

            # How well it sits with the last few records, not just the one
            # that's on. Matching only the current track is how a run wanders:
            # every single step is a fair follow-on and thirty of them end up
            # somewhere else entirely. Something that belongs with four of the
            # last ten can't be the far end of a slow walk.
            flow = _run_fit(run_now, track)
            if flow:
                score += 3.0 * flow


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
                lift = 3.0 * (sim - 0.55)        # genre and mood lead
                # Unless somebody who knows better has already vouched for it.
                # This term is what a queue is mostly made of, and it's a
                # measure of shared labels — so a record that doesn't wear the
                # same words gets docked most of a point. Which is exactly
                # backwards when the reason we're looking at it is that people
                # play it alongside the anchor, or that the anchor's artist is
                # filed next to this one. Pyramid Song is tagged alternative
                # rock: Foo Fighters scored 0.61 and gained, Thom Yorke scored
                # 0.28 and lost most of a point, and the tail of the run went
                # to Nickelback and 3 Doors Down. Tags disagreeing with what
                # people actually listen to is not evidence against the track.
                if lift < 0 and vouched:
                    lift *= 0.3
                score += lift
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
                soft = {"theme": 0.0, "root": 0.5}.get(source, 1.0)
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
            # Nobody names their band after the genre they're filed under.
            # Searching trip-hop returned "Trip Hop 08" by "Trip Hop", and
            # the psychedelic soul lane returned "Psychedelic Soul Train" —
            # library uploads named to match whatever you typed.
            if _flatten(track.primary_artist()) in searched:
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
