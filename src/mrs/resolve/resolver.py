"""Plan -> actual tracks."""

from __future__ import annotations

import time
from dataclasses import replace
from itertools import zip_longest

from ..config import config
from ..logging_setup import get
from ..models import Plan, Track, _fold, _strip_article
from . import catalog, ranking

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



def _similar(plan: Plan, taste=None) -> "Resolution":
    """"Songs like Motorhead": the bands that sit next to them, each one's
    best-known songs, taken in turn -- not Motorhead again. The radio keeps
    steering by Motorhead's sound afterwards."""
    from concurrent.futures import ThreadPoolExecutor
    from ..core.kin import kin
    who = (plan.artist or plan.query).strip()
    near = [n for n in kin.prime(Track(title="", artist=who))
            if not ranking.artist_matches(Track(title="", artist=n), who)][:8]
    if not near:
        # Nobody known next to them: their own radio, which drifts outward.
        got = resolve(replace(plan, kind="artist"), taste)
        if got:
            got.spoken = f"Playing music like {who}"
        return got

    def best(name: str) -> list[Track]:
        tops = catalog.artist_top_tracks(name, limit=30)
        return ranking.likely(tops, taste, variety=0.2)[:4] if tops else []
    with ThreadPoolExecutor(max_workers=6) as pool:
        lanes = [lane for lane in pool.map(best, near) if lane]
    if not lanes:
        return _nothing(f"Couldn't find anything like {who}")
    dealt, seen = [], set()
    for row in zip_longest(*lanes):
        for t in row:
            if t is not None and t.video_id not in seen:
                seen.add(t.video_id)
                dealt.append(t)
    own = catalog.artist_top_tracks(who, limit=5)
    anchors = ([own[0]] if own else []) + [lane[0] for lane in lanes[:3]]
    return Resolution(dealt[:40], f"Playing bands like {who}", anchors=anchors)


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


def _several(plan: Plan, kind: str, taste=None) -> "Resolution | None":
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
        got = resolve(sub, taste)
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


def _mix(plan: Plan, taste=None) -> "Resolution | None":
    """Several different things at once: bands, songs, albums and genres.

    Each is resolved as what it is -- a band's likeliest songs, the song
    itself, the album, the genre -- and they're dealt out in turn, so the
    queue opens with one of each. Every one of them is an anchor the radio
    keeps coming back to, and a genre that ties them together (if the reader
    gave one) is the theme it stays inside when those run out.
    """
    from concurrent.futures import ThreadPoolExecutor
    items = [i for i in plan.items[:MIX_MAX] if (i.get("name") or "").strip()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = list(pool.map(lambda i: _mix_item(i, plan, taste), items))
    songs = [r for i, r in zip(items, got) if r and i.get("kind") == "song"]
    rest = [r for i, r in zip(items, got) if r and i.get("kind") != "song"]
    parts = songs + rest
    if not parts:
        return None
    # Every named song once, in the order given; the bands and genres after.
    first = [r.tracks[0] for r in songs]
    each = max(3, int(config.get("queue_minutes", 30)) // (2 * max(1, len(rest))) + 3)
    if getattr(plan, "count", 0) and rest:
        each = max(1, max(0, plan.count - len(first)) // len(rest) + 1)
    lanes = [p.tracks[:each] for p in rest]
    dealt: list[Track] = []
    seen: set[str] = set()
    for t in first + [t for row in zip_longest(*lanes) for t in row]:
        if t is not None and t.video_id not in seen and not (t.key() and t.key() in seen):
            seen.add(t.video_id)
            if t.key():
                seen.add(t.key())
            dealt.append(t)
    if getattr(plan, "count", 0):
        dealt = dealt[:max(plan.count, len(first))]
    missed = len(items) - len(parts)
    if len(songs) > 3:
        said = f"{len(songs)} songs" + (f" and {len(rest)} more" if rest else "")
    else:
        names = [i.get("name") for i in items if i.get("name")]
        said = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    if missed:
        said += f" (couldn't find {missed})"
    return Resolution(dealt, f"Playing {said}",
                      anchors=[p.tracks[0] for p in parts],
                      hold_radio=all(p.hold_radio for p in parts))


MIX_MAX = 40


def _mix_item(item: dict, plan: Plan, taste=None) -> "Resolution | None":
    kind = item.get("kind") or "auto"
    name = (item.get("name") or "").strip()
    artist = item.get("artist", "") or (name if kind == "artist" else "")
    got = resolve(Plan(kind=kind, query=name, artist=artist, via=plan.via,
                       spoken=plan.spoken), taste)
    if kind == "song" and item.get("either") and artist:
        # "X - Y" is written both ways round. Keep the reading whose song is
        # by the band it names.
        if not (got and got.tracks and ranking.artist_matches(got.tracks[0], artist)):
            other = resolve(Plan(kind="song", query=artist, artist=name, via=plan.via,
                                 spoken=plan.spoken), taste)
            if other and other.tracks and ranking.artist_matches(other.tracks[0], name):
                got = other
    return got if got and got.tracks else None


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


def resolve(plan: Plan, taste=None) -> Resolution:
    """`taste` is the listener's, so "most likely wanted" means them."""
    kind = plan.kind
    query = (plan.query or "").strip()

    if kind == "similar" and query:
        return _similar(plan, taste)

    if kind == "mix" and getattr(plan, "items", None):
        mixed = _mix(plan, taste)
        if mixed is not None:
            return mixed

    # Named more than one thing? Try it as several, and fall back to one if
    # that turns out to be wrong.
    if plan.seeds and kind in ("auto", "artist", "genre"):
        several = _several(plan, kind, taste)
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
        hits = []
        if plan.artist:
            # "X by Y" means Y's X. Searching the title alone and reordering
            # the top eight lost whenever Y's recording wasn't among them --
            # which for a common title is most of the time -- and somebody
            # else's song played. Ask for both, and take Y's first.
            both = catalog.search_songs(f"{query} {plan.artist}", limit=8,
                                        allow_variant=plan.variant)
            theirs = [t for t in both if ranking.artist_matches(t, plan.artist)]
            named = [t for t in theirs if ranking.title_matches(t, query)]
            hits = named + [t for t in theirs if t not in named]
        if not hits:
            hits = catalog.search_songs(query, limit=8, allow_variant=plan.variant)
            if plan.artist:
                preferred = [t for t in hits if ranking.artist_matches(t, plan.artist)]
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
        # Their best-known songs, weighed against what this listener plays
        # and skips -- not side one of the debut.
        head = ranking.likely(tracks[:100], taste)
        tracks = head + [t for t in tracks[100:] if t not in head]
        if getattr(plan, "count", 0):
            tracks = tracks[:plan.count]
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
