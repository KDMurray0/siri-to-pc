"""Plan -> actual tracks."""

from __future__ import annotations

import time
from dataclasses import replace
from itertools import zip_longest

from ..config import config
from ..logging_setup import get
from ..models import Plan, Track, _fold, _strip_article
from . import catalog

log = get("resolve")


class Resolution:
    def __init__(self, tracks: list[Track], spoken: str, *,
                 hold_radio: bool = False, alternates: list[str] | None = None,
                 error: str = "", anchors: list[Track] | None = None) -> None:
        self.tracks = tracks
        # What the radio should steer by. One per thing that was asked for,
        # so "nirvana and foo fighters" keeps hearing from both.
        self.anchors = anchors or ([tracks[0]] if tracks else [])
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
    if catalog.offline():
        return Resolution([], "I can't reach the internet. I'll keep playing "
                              "what's already downloaded.", error="offline")
    wait = catalog.throttled()
    if wait:
        # This gets spoken back, so it reads as a sentence rather than a
        # status code.
        return Resolution([], f"YouTube is throttling us. Try again in about "
                              f"{int(wait) + 1} seconds.", error="throttled")
    return Resolution([], said, error="no results")


def _one_act(query: str) -> bool:
    """Is the whole phrase somebody's name, rather than two things?

    The splitter catches the shapes it knows — "X and the Y", a short list
    of the usual suspects — and will happily cut a band name it has never
    heard of in half. This is the check that stops it: if YouTube knows an
    artist by that exact name, it's one act.
    """
    want = _strip_article(_fold(query.strip().lower()))
    if not want:
        return False
    for row in catalog.search_artists(query, limit=3):
        if _strip_article(_fold((row.get("name") or "").lower())) == want:
            return True
    return False


def _several(plan: Plan, kind: str) -> "Resolution | None":
    """Resolve a request that named more than one thing.

    Each seed is resolved on its own and the results are dealt out in turn,
    so the queue opens with one of each rather than all of the first.
    Returns None to mean "treat it as one thing after all".
    """
    if _one_act(plan.query):
        log.info("%r is one act, not %d", plan.query, len(plan.seeds))
        return None
    from .conjunction import looks_like_genre

    # "grunge and britpop" arrives as kind=auto, and resolving each half on
    # its own asked YouTube for an *artist* called britpop — which came back
    # with A. G. Cook, and the request then announced itself as "Alice In
    # Chains and A. G. Cook". A seed that is plainly a genre is resolved as
    # one. Only when every seed is: "bon jovi and shoegaze" is a person and
    # a genre, and each half already handles itself correctly.
    genres = [looks_like_genre(s) for s in plan.seeds[:4]]
    as_genre = kind == "genre" or (kind == "auto" and all(genres) and genres)
    if as_genre and kind != "genre":
        log.info("%r is genres, not artists", plan.query)

    parts: list[Resolution] = []
    for seed in plan.seeds[:4]:          # four is already an odd request
        sub = replace(plan, query=seed, artist="", seeds=[],
                      kind="genre" if as_genre else plan.kind)
        got = resolve(sub)
        if got and got.tracks:
            parts.append(got)
    if len(parts) < 2:
        return None                      # only one of them was real

    # A share each, then dealt out in turn.
    each = max(3, int(config.get("queue_minutes", 30)) // (2 * len(parts)) + 3)
    lanes = [p.tracks[:each] for p in parts]
    dealt: list[Track] = []
    seen: set[str] = set()
    for row in zip_longest(*lanes):
        for t in row:
            if t is not None and t.video_id not in seen:
                seen.add(t.video_id)
                dealt.append(t)
    # Name a genre request after the genres, not after whoever happened to
    # come back first — "nu metal and rap rock" announcing itself as
    # "Deftones and Olivia Rodrigo" is both wrong and unhelpful.
    names = (list(plan.seeds) if as_genre
             else [p.tracks[0].artist or s for p, s in zip(parts, plan.seeds)])
    said = " and ".join(names[:2]) + ("…" if len(names) > 2 else "")
    return Resolution(dealt, f"Playing {said}",
                      hold_radio=any(p.hold_radio for p in parts),
                      anchors=[p.tracks[0] for p in parts])


def first_minutes(tracks: list[Track], minutes: float) -> list[Track]:
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

    # Named more than one thing? Try it as several, and fall back to one if
    # that turns out to be wrong.
    if plan.seeds and kind in ("auto", "artist", "genre"):
        several = _several(plan, kind)
        if several is not None:
            return several
    if not query:
        return Resolution([], "I didn't catch that", error="empty")

    source = (plan.source or config.get("source") or "youtube").lower()
    if source in ("soundcloud", "bandcamp"):
        hits = _from_source(source, query)
        if not hits:
            # Asked for one source and it has nothing — the others might.
            other = _elsewhere(query, skip=(source,))
            if other:
                return other
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
            # Blocked, region-locked, taken down — YouTube having nothing
            # isn't the same as the record not existing. Try the others
            # before giving up.
            other = _elsewhere(query, skip=("youtube",))
            if other:
                return other
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
        tracks = first_minutes(tracks, float(config.get("queue_minutes", 30)))
        return Resolution(tracks, f"Playing {who}", hold_radio=True)

    if kind == "genre":
        # "nu metal and rap rock" is two genres, not one phrase. Searched
        # whole it matches neither and lands on whatever shares the words —
        # that request came back with A$AP Rocky's PUNK ROCK, which has the
        # vocabulary and none of the meaning. Each genre gets its own search
        # and they're dealt out in turn, so both are on from track one.
        from .conjunction import split_seeds
        parts = [g for g in split_seeds(query) if g.strip()]
        if len(parts) > 1:
            each = max(6, 30 // len(parts))
            lanes = []
            for g in parts:
                got = _on_theme(g, catalog.genre_tracks(g, limit=each))
                if got:
                    lanes.append(got)
            if lanes:
                dealt: list[Track] = []
                seen: set[str] = set()
                for row in zip_longest(*lanes):
                    for t in row:
                        if t is not None and t.video_id not in seen:
                            seen.add(t.video_id)
                            dealt.append(t)
                if dealt:
                    said = " and ".join(parts[:2]) + ("…" if len(parts) > 2 else "")
                    return Resolution(dealt, f"Playing {said}")

        tracks = _on_theme(query, catalog.genre_tracks(query, limit=25))
        if not tracks:
            return _nothing(f"I couldn't find anything for {query}")
        return Resolution(tracks, f"Playing some {query}")

    return Resolution([], f"I couldn't work out what {query} means", error="unknown")


def _from_source(source: str, query: str, limit: int = 5) -> list[Track]:
    fn = (catalog.search_soundcloud if source == "soundcloud"
          else catalog.search_bandcamp)
    try:
        return fn(query, limit=limit) or []
    except Exception as exc:
        log.debug("%s search failed: %s", source, exc)
        return []


def _elsewhere(query: str, skip: tuple = ()) -> "Resolution | None":
    """The same record on a source that isn't blocking it.

    A song missing from YouTube is usually a takedown or a region lock rather
    than a song that doesn't exist, and SoundCloud and Bandcamp don't share
    YouTube's blocklist. Only reached when the first choice came back empty,
    so it costs nothing on the normal path.
    """
    for alt in ("soundcloud", "bandcamp"):
        if alt in skip:
            continue
        hits = _from_source(alt, query)
        if hits:
            log.info("%r wasn't on the usual source — found it on %s", query, alt)
            return Resolution(hits[:1],
                              f"Playing {hits[0].title} from {alt}")
    return None


def _on_theme(genre: str, tracks: list[Track]) -> list[Track]:
    """On-genre first, obvious misses dropped.

    A grunge request came back with Thong Song at number one, which then
    became the anchor. Track one matters twice over, so verified ones go
    first. Unknowns are kept but demoted — with no Last.fm key nothing
    changes.
    """
    from ..core.context import _theme_words, matches_theme
    from ..core.tags import tagstore

    want = _theme_words(genre)
    if not want or not tracks or not tagstore.enabled():
        return tracks

    tagstore.warm(tracks)
    # Eight seconds, not six. Every track still unlooked-up when this expires
    # counts as "unknown", and unknowns are what get through — so the deadline
    # is directly how much drift a genre request tolerates.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if all(tagstore.get(t) is not None for t in tracks):
            break
        time.sleep(0.25)

    good, unknown, bad = [], [], []
    for t in tracks:
        tags = tagstore.get(t)
        if not tags:
            unknown.append(t)
        elif matches_theme(genre, tags):
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
    # Six confirmed is enough to fill the front of a queue on its own, and
    # ask for two genres at once and each lane only gets half the results —
    # at eight neither half ever cleared the bar, so the unverified rode
    # along and a rap-rock request kept Olivia Rodrigo in second place.
    if len(good) >= 6:
        if unknown:
            log.info("%s: also dropped %d unverified", genre, len(unknown))
        return good
    return good + unknown
