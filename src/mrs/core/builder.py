"""Make a playlist of a given size from a description: bands, genres, or both.

"500 songs of nu metal and glam metal" is read into anchors -- here, two genres
-- and filled from each anchor's most-played songs, weighed against the
listener's own history. A band gives its likeliest songs; a genre gives its top
artists' likeliest few each, so a big list is many bands and not one band a
hundred times. Everything is dealt out so no act runs back to back.

It takes a while for a big list -- two lookups per artist -- so it runs aside
and the list fills in when it's done.

Strict: only songs by those bands, from those albums, in those genres. Not
strict: songs that fit -- Groq names well-known songs (or, for a long list,
bands) that sound like the description, and each is checked against the real
catalogue before it goes in. When Groq is resting the job waits, says when it
will start, and starts by itself; with no Groq at all, the bands Deezer files
next to the ones named stand in.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from collections import Counter
from itertools import zip_longest

from ..logging_setup import get
from ..models import Plan, Track
from . import curator

log = get("builder")

MAX_SONGS = 1000
MAX_ACTIVE_BUILDS = 2
MAX_BUILDS_PER_HOUR = 4
# A window across all libraries, on top of the per-library one: every build
# spends real API calls, and one library's allowance must not be an
# invitation to open a hundred libraries.
MAX_GLOBAL_BUILDS_PER_HOUR = 12
MAX_JOB_HISTORY = 100
MAX_BUILD_OWNERS = 256
_jobs: dict[str, dict] = {}
_starts: dict[str, list[float]] = {}
_gstarts: list[float] = []
_lock = threading.Lock()
_INTERNAL_JOB_READ = object()


class BuildBusy(RuntimeError):
    """A listener or the server has reached its bounded build allowance."""


def _store_key(store) -> str:
    """Identity comes from the authorized playlist store, never a client field."""
    import hashlib
    if store is None:
        return "internal"
    return hashlib.sha256(str(store.root().resolve()).casefold().encode()).hexdigest()


def _admit_build(owner: str, now: float) -> None:
    """Called with _lock held, before creating a thread or spending API calls."""
    active = [r for r in _jobs.values() if r["state"] in ("building", "waiting")]
    if any(r.get("_owner") == owner for r in active):
        raise BuildBusy("Your playlist is still being made. Wait for it to finish.")
    if len(active) >= MAX_ACTIVE_BUILDS:
        raise BuildBusy("The playlist builder is busy. Try again when a list finishes.")
    for who in list(_starts):
        recent = [at for at in _starts[who] if now - at < 3600]
        if recent:
            _starts[who] = recent
        else:
            _starts.pop(who)
    recent = _starts.get(owner, [])
    if len(recent) >= MAX_BUILDS_PER_HOUR:
        raise BuildBusy(f"You can make up to {MAX_BUILDS_PER_HOUR} AI playlists an hour. Try again later.")
    _gstarts[:] = [at for at in _gstarts if now - at < 3600]
    if len(_gstarts) >= MAX_GLOBAL_BUILDS_PER_HOUR:
        raise BuildBusy("The playlist builder is busy. Try again when a list finishes.")
    if owner not in _starts and len(_starts) >= MAX_BUILD_OWNERS:
        raise BuildBusy("The playlist builder is busy. Try again later.")
    _starts[owner] = recent + [now]
    _gstarts.append(now)
    finished = sorted((key for key, row in _jobs.items()
                       if row["state"] not in ("building", "waiting")),
                      key=lambda key: _jobs[key]["at"])
    for key in finished:
        if len(_jobs) < MAX_JOB_HISTORY and now - _jobs[key]["at"] < 3600:
            break
        _jobs.pop(key)


class GroqBusy(Exception):
    """Groq can't be asked right now; try again in `wait` seconds."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"Groq is resting for {wait:.0f}s")
        self.wait = max(5.0, float(wait))


def anchors_of(what: str, *, model: bool = True) -> list[dict]:
    """What the description names, as [{"kind": "artist"|"genre"|"song", "name"}].

    model=False reads it locally only -- when the planner has already read it.
    """
    from ..resolve import grammar, parser
    from ..resolve.conjunction import looks_like_genre, split_seeds
    if model:
        plan = parser.parse(what)
    else:
        listed = grammar.song_list(what)
        plan = Plan(kind="mix", items=listed[0]) if listed else grammar.parse(what)
    if plan.kind == "mix" and plan.items:
        return [dict(i) for i in plan.items]
    if plan.kind == "album" and plan.query:
        return [{"kind": "album", "name": plan.query, "artist": plan.artist}]
    names = plan.seeds or ([plan.query] if plan.query else [])
    kind = plan.kind if plan.kind in ("artist", "genre", "song") else ""
    out = []
    for name in names or split_seeds(what):
        k = kind or ("genre" if looks_like_genre(name) else "artist")
        out.append({"kind": k, "name": name.strip(), "artist": plan.artist if k == "song" else ""})
    return [a for a in out if a["name"]]


def _genre_artists(genre: str, want: int) -> list[str]:
    """The acts a genre is made of, most-played first."""
    from ..core.tags import tagstore
    names: list[str] = []
    try:
        # Last.fm ranks a tag's artists by how often they're tagged with it,
        # not by how much they're played: glam metal came back Ratt, Cinderella,
        # Steel Panther, Nitro -- no Def Leppard, Mötley Crüe sixth. So a wide
        # pool, re-ordered by how many people actually listen to each.
        data = tagstore._call({"method": "tag.getTopArtists", "tag": genre,
                               "limit": 100}) if tagstore.enabled() else {}
        rows = (data.get("topartists") or {}).get("artist") or []
        names = [(r.get("name") or "").strip() for r in rows if isinstance(r, dict)]
        names = _by_listeners([n for n in names if n])
    except Exception as exc:
        log.debug("top artists for %r: %s", genre, exc)
    if not names:
        # No Last.fm: the artists behind the genre's tracks, in order.
        from ..resolve import catalog
        had: set[str] = set()
        for t in catalog.genre_tracks(genre, limit=40):
            who = t.primary_artist()
            if who and who not in had:
                had.add(who)
                names.append((t.artist or "").split(",")[0].strip())
    return [n for n in names if n][:want]


def _by_listeners(names: list[str]) -> list[str]:
    """Most-listened first, asked of Last.fm a few at a time."""
    from concurrent.futures import ThreadPoolExecutor
    from ..core.tags import tagstore

    def listeners(name: str) -> int:
        try:
            info = tagstore._call({"method": "artist.getInfo", "artist": name, "autocorrect": 1})
            return int(((info.get("artist") or {}).get("stats") or {}).get("listeners") or 0)
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(listeners, names))
    order = sorted(range(len(names)), key=lambda i: (-counts[i], i))
    return [names[i] for i in order]


def _lane_for_genre(genre: str, n: int, taste, progress=None) -> list[Track]:
    """A genre's share: a few likeliest songs from each of its top acts."""
    per_act = max(2, min(8, round(math.sqrt(n))))
    acts = _genre_artists(genre, max(8, math.ceil(n / per_act) + 4))
    lanes = []
    for i, act in enumerate(acts):
        got = _lane_for_artist(act, per_act, taste)
        if got:
            lanes.append(got)
        if progress:
            progress(f"{genre}: {i + 1} of {len(acts)} bands")
        if sum(len(x) for x in lanes) >= n:
            break
    return _deal(lanes)[:n]


def _spread(main: list, extra: list) -> list:
    """Both lists in their own order, the second spread evenly through the first."""
    if not extra or not main:
        return list(main or extra)
    placed = [((i + .5) / len(main), 0, x) for i, x in enumerate(main)]
    placed += [((j + .5) / len(extra), 1, x) for j, x in enumerate(extra)]
    placed.sort(key=lambda row: (row[0], row[1]))
    return [x for *_, x in placed]


def _blend(main: list[Track], extra: list[Track]) -> list[Track]:
    return _deal([_spread(main, extra)])


def _unclump(items: list, artist=lambda t: t.primary_artist()) -> list:
    """No artist twice in a row, moving as little as it can."""
    out = list(items)
    for i in range(1, len(out)):
        prev = artist(out[i - 1])
        if prev and artist(out[i]) == prev:
            k = next((k for k in range(i + 1, len(out)) if artist(out[k]) != prev), None)
            if k is not None:
                out.insert(i, out.pop(k))
    return out


def _loose(t: Track) -> str:
    """A song's key with "The" and punctuation gone: "The Toxic Waltz" is
    "Toxic Waltz"."""
    artist, _, title = t.key().partition("|")
    return artist + "|" + re.sub(r"[^a-z0-9]", "", re.sub(r"^the\s+", "", title))


def _weave(lanes: list[list[Track]], seed: str = "") -> list[Track]:
    """Each band's songs spread across the whole list, best-known first.

    Not dealt like cards -- A, B, C, A, B, C -- which is how every list used
    to read. Each song lands about its fair share of the way through, nudged
    at random, so the bands meet in a different order every time round.
    """
    rng = random.Random(seed or str(len(lanes)))
    placed = []
    for lane in lanes:
        n = len(lane)
        for j, t in enumerate(lane):
            placed.append(((j + .5 + rng.uniform(-.45, .45)) / n, rng.random(), t))
    placed.sort(key=lambda row: (row[0], row[1]))
    return _unclump(_deal([[t for *_, t in placed]]))


def _deal(lanes: list[list[Track]]) -> list[Track]:
    out, seen, keys = [], set(), set()
    for row in zip_longest(*lanes):
        for t in row:
            if t is None or t.video_id in seen or (t.key() and _loose(t) in keys):
                continue
            seen.add(t.video_id)
            if t.key():
                keys.add(_loose(t))
            out.append(t)
    return out


def _lane_for_album(name: str, artist: str, taste) -> list[Track]:
    from ..resolve import resolver
    res = resolver.resolve(Plan(kind="album", query=name, artist=artist), taste)
    return list(res.tracks) if res else []


def _found(artist: str, title: str) -> Track | None:
    """A song Groq named, if it's really out there -- by that band, that title."""
    from ..resolve import catalog, ranking
    try:
        hits = catalog.search_songs(f"{title} {artist}", limit=6)
    except Exception:
        return None
    from ..models import is_derivative
    if is_derivative(title) or is_derivative(artist):
        return None
    for t in hits or []:
        if (not is_derivative(t.title) and not is_derivative(t.artist)
                and ranking.artist_matches(t, artist)
                and ranking.title_matches(t, title)):
            return t
    return None


def _listener_context(taste) -> str:
    if taste is None:
        return ""
    try:
        artists = [r["artist"] for r in taste.top_artists(6) if r.get("artist")]
        liked = [f"{r.get('title', '')} by {r.get('artist', '')}"
                 for r in taste.liked()[-6:] if r.get("title")]
        if not artists and not liked:
            return ""
        return " Listener tends to enjoy: " + "; ".join(artists + liked)[:500]
    except (AttributeError, TypeError):
        return ""


def _brief_or_request(brief, what: str, context: str = ""):
    """The planner's brief, or the request itself when there isn't one."""
    from . import curator
    if brief is not None and brief.describe():
        return brief
    return curator.Brief(note=f"Songs that fit: {what}.{context}")


def _groq_songs(what: str, n: int, context: str = "", brief=None,
                have: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """The worker's songs for the brief, in the order it would play them."""
    from . import curator
    try:
        return curator.songs(_brief_or_request(brief, what, context), n, have=have)
    except curator.Busy as busy:
        raise GroqBusy(busy.wait) from None


def _groq_artists(what: str, n: int, context: str = "", brief=None) -> list[str]:
    from . import curator
    try:
        return curator.artists(_brief_or_request(brief, what, context), n)
    except curator.Busy as busy:
        raise GroqBusy(busy.wait) from None


# -- the shared sound ----------------------------------------------------

# Tags that say nothing about how a band sounds.
_JUNK_TAGS = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "awesome",
    "love", "best", "classic", "legend", "legends", "cult", "underrated", "all",
    "male vocalists", "female vocalists", "male vocalist", "female vocalist",
    "american", "british", "english", "german", "brazilian", "swedish", "norwegian",
    "french", "finnish", "canadian", "australian", "uk", "usa", "us", "polish",
    "dutch", "italian", "japanese", "spanish", "danish", "swiss", "irish", "scottish",
    "60s", "70s", "80s", "90s", "00s", "10s", "20s", "music", "band", "bands"}
# Words too broad to tell one lane of heavy music from another.
_BROAD = {"metal", "rock", "music", "and", "n", "roll", "the"}
# What makes a song the odd one out in its band's catalogue.
_SOFT_TAGS = {"ballad", "ballads", "power ballad", "power ballads", "acoustic",
              "love songs", "love song", "slow", "mellow", "soft rock", "chill",
              "piano", "cover", "covers", "country", "christmas", "soundtrack"}
_TAG_CHECKS = 320          # song lookups one build may spend on Last.fm


def _tags(track: Track, artist: bool = False) -> dict[str, int]:
    """Last.fm tags for a song or its band, from the shared cache when held."""
    from .tags import tagstore
    return tagstore.lookup(track, artist)


def _top(cloud: dict[str, int], n: int = 8) -> list[str]:
    return [t for t, _ in sorted(cloud.items(), key=lambda kv: -kv[1])
            if t not in _JUNK_TAGS][:n]


def _words(tag: str) -> set[str]:
    return set(re.findall(r"[a-z]+", tag.lower())) - _BROAD


def core_sound(names: list[str]) -> list[str]:
    """The subgenres most of these bands share, most shared first.

    Sodom, Slayer, Morbid Angel and Venom come out as thrash, death, speed
    and black metal: the lane a list of them stays in.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        clouds = [c for c in pool.map(lambda n: _tags(Track(title="", artist=n), artist=True),
                                      names[:40]) if c]
    if not clouds:
        return []
    if len(clouds) == 1:
        return _top(clouds[0], 4)
    # "metal" and "rock" are on every band here and tell no lane from another.
    shared = Counter(t for c in clouds for t in _top(c) if _words(t))
    need = max(2, math.ceil(len(clouds) * .3))
    core = [t for t, n in shared.most_common() if n >= need]
    return (core or [t for t, _ in shared.most_common(3)])[:8]


def _sounds_like(tags: list[str], core: list[str]) -> bool | None:
    """Do these tags sit in the core's lane? None when they can't say."""
    want = set(core)
    lane = set().union(*(_words(t) for t in core)) if core else set()
    told = False
    for t in tags:
        if t in want:
            return True
        w = _words(t)
        if w:
            told = True
            if w & lane:
                return True
    return False if told else None


def _fits_sound(track: Track, core: list[str]) -> int:
    """2 it fits, 1 can't tell, 0 it's the odd one out."""
    cloud = _tags(track)
    if not cloud:
        return 1
    ranked = sorted(cloud.items(), key=lambda kv: -kv[1])[:10]
    peak = ranked[0][1] or 1
    if sum(c for t, c in ranked if t in _SOFT_TAGS) / peak >= .35:
        return 0
    said = _sounds_like([t for t, _ in ranked if t not in _JUNK_TAGS], core)
    return 1 if said is None else (2 if said else 0)


# -- filling it ------------------------------------------------------------

def _band_songs(name: str, taste) -> list[Track]:
    """A band's songs, most played first, barely shuffled."""
    from ..models import is_derivative
    from ..resolve import catalog, ranking
    ranked = ranking.likely(catalog.artist_top_tracks(name), taste, variety=.08)
    return [t for t in ranked if not is_derivative(t.title)]


def _lane_for_artist(name: str, n: int, taste, **_) -> list[Track]:
    return _band_songs(name, taste)[:n]


_VET = ("You vet playlists. Reply with JSON only: {\"odd\":[numbers]}. List only "
        "clear cases; most lists have few or none.")
_VET_MAX = 240
_WORKER = curator.WORKER           # the small, busy model


def _odd_ones(tracks: list[Track], core: list[str], what: str = "") -> set[str]:
    """Which of these don't belong: a thrash band's ballad, an acoustic single,
    one band covering another's song. Groq knows; Last.fm's song tags mostly
    don't (Nothing Else Matters has none at all)."""
    from ..resolve import llm
    rows = tracks[:_VET_MAX]
    if len(rows) < 3 or not llm.available():
        return set()
    sound = ", ".join(core[:5]) or what[:200]
    lines = "\n".join(f"{i}. {t.title} - {(t.artist or '').split(',')[0]}"
                      for i, t in enumerate(rows))
    got = llm.ask_json(_VET, f"A playlist in the sound of: {sound}. Which of these are "
                       "the odd ones out: ballads and slow songs (even famous ones), acoustic "
                       "songs, a band's cover of someone else's song, or a different style "
                       "from the rest? A band's usual-sounding hits are not odd.\n" + lines,
                       timeout=30, model=_WORKER) or {}
    odd = got.get("odd") if isinstance(got.get("odd"), list) else []
    return {rows[i].key() for i in odd if isinstance(i, int) and 0 <= i < len(rows)}


def _listeners(track: Track) -> int:
    from .tags import tagstore
    if not tagstore.enabled():
        return 0
    try:
        info = tagstore._call({"method": "track.getInfo", "track": track.title,
                               "artist": (track.artist or "").split(",")[0].strip()})
        return int((info.get("track") or {}).get("listeners") or 0)
    except Exception:
        return 0


def _covers(bands: list[list[Track]]) -> set[str]:
    """Keys of one band's version of another band's song: Motorhead's Enter
    Sandman. The original is the one more people listen to; without Last.fm,
    the one higher up its own band's list."""
    seen: dict[str, list[tuple[int, Track]]] = {}
    for band in bands:
        for rank, t in enumerate(band):
            title = t.key().partition("|")[2]
            if title:
                seen.setdefault(title, []).append((rank, t))
    out = set()
    for versions in seen.values():
        if len({t.primary_artist() for _, t in versions}) < 2:
            continue
        heard = [_listeners(t) for _, t in versions]
        best = max(range(len(versions)), key=lambda i: (heard[i], -versions[i][0]))
        out |= {t.key() for i, (_, t) in enumerate(versions) if i != best}
    return out


def _fill(names: list[str], songs: int, taste, core: list[str], progress=None,
          label: str = "bands") -> tuple[list[Track], list[Track]]:
    """(fitting, held back) for a set of bands.

    Each band's best-known songs that sound like the rest of the list, dealt
    out so no band runs on, going deeper into each only while there aren't
    enough. Songs that don't fit the shared sound are held back for when
    there still aren't.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not names or songs <= 0:
        return [], []
    per = max(2, math.ceil(songs * 1.15 / len(names)))
    depth = per * 3
    budget = _TAG_CHECKS
    found: list[list[Track]] = []
    for i, name in enumerate(names):
        found.append(_band_songs(name, taste)[:depth])
        if progress:
            progress(f"{name}: {i + 1} of {len(names)} {label}", found[-1][:per])
    # The likeliest few of each band are what gets vetted: those are the ones
    # that would go in.
    look = min(per + 6, max(3, _VET_MAX // max(1, len(found))))
    front = [t for band in found for t in band[:look]]
    if progress and core:
        progress(f"Checking {len(front)} songs fit the sound")
    odd = _odd_ones(front, core, ", ".join(names[:12])) | _covers(found)
    lanes: list[list[Track]] = []
    held: list[Track] = []
    for ranked in found:
        n = min(len(ranked), look, max(0, budget)) if core else 0
        budget -= n
        if n:
            with ThreadPoolExecutor(max_workers=4) as pool:
                marks = list(pool.map(lambda t: _fits_sound(t, core), ranked[:n]))
        else:
            marks = []
        marks += [1] * (len(ranked) - len(marks))
        marks = [0 if t.key() in odd else m for t, m in zip(ranked, marks)]
        good = [t for t, m in zip(ranked, marks) if m]
        held += [t for t, m in zip(ranked, marks) if not m]
        if good:
            lanes.append(good)
    take = per
    while take < songs and sum(min(len(lane), take) for lane in lanes) < songs \
            and any(len(lane) > take for lane in lanes):
        take += max(1, per // 2)
    return _weave([lane[:take] for lane in lanes], seed=",".join(names)), held


def _kin_bands(named: list[str], core: list[str], limit: int) -> list[str]:
    """Bands filed next to the named ones that share their sound."""
    from concurrent.futures import ThreadPoolExecutor
    from .kin import kin
    have = {n.casefold() for n in named}
    out: list[str] = []
    for name in named:
        for k in kin.prime(Track(title="", artist=name))[:8]:
            if k.casefold() not in have:
                have.add(k.casefold())
                out.append(k)
    if core and out:
        with ThreadPoolExecutor(max_workers=6) as pool:
            clouds = list(pool.map(lambda n: _tags(Track(title="", artist=n), artist=True),
                                   out[:limit * 2]))
        out = [n for n, c in zip(out, clouds) if not c or _sounds_like(_top(c, 6), core) is not False]
    return out[:limit]


def _is_band(name: str) -> bool:
    from ..resolve import catalog, ranking
    want = ranking._name(name).replace(" ", "")
    return bool(want) and any(ranking._name(r.get("name", "")).replace(" ", "") == want
                              for r in catalog.search_artists(name, limit=3))


def _unmerge(anchors: list[dict]) -> list[dict]:
    """"hellhammer Motorhead": a comma that went missing between two bands."""
    from concurrent.futures import ThreadPoolExecutor
    multi = [a["name"] for a in anchors
             if a["kind"] == "artist" and 2 <= len(a["name"].split()) <= 5]
    if not multi:
        return anchors
    with ThreadPoolExecutor(max_workers=6) as pool:
        real = dict(zip(multi, pool.map(_is_band, multi)))
    out = []
    for a in anchors:
        if a["kind"] == "artist" and real.get(a["name"]) is False:
            words = a["name"].split()
            for cut in range(1, len(words)):
                left, right = " ".join(words[:cut]), " ".join(words[cut:])
                if _is_band(left) and _is_band(right):
                    out += [{**a, "name": left}, {**a, "name": right}]
                    break
            else:
                out.append(a)
        else:
            out.append(a)
    return out


def _named_songs(anchors: list[dict], taste) -> list[Track]:
    """Every song they named that's out there, in the order they named it."""
    from concurrent.futures import ThreadPoolExecutor
    from ..resolve import resolver
    wanted = [a for a in anchors if a["kind"] == "song"]
    if not wanted:
        return []
    plan = Plan(kind="mix", via="builder")
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = list(pool.map(lambda a: resolver.resolve_item(a, plan, taste), wanted))
    return _deal([[r.tracks[0] for r in got if r]])


def _bands_of(anchors: list[dict], first: list[Track]) -> list[str]:
    names = [a["name"] for a in anchors if a["kind"] == "artist"]
    names += [a["artist"] for a in anchors if a["kind"] == "album" and a.get("artist")]
    names += [t.artist.split(",")[0].strip() for t in first if t.artist]
    return list(dict.fromkeys(n for n in names if n))


def _fits(what: str, anchors: list[dict], songs: int, taste, progress,
          context: list[Track] | None = None, first: list[Track] | None = None,
          core: list[str] | None = None, brief=None) -> list[Track]:
    """Songs that fit rather than songs by: the worker's picks for the brief, in
    the order it would play them, checked against the catalogue and the shared
    sound; the named bands' best-known songs, then bands that sound like them,
    spread through when that isn't enough."""
    from concurrent.futures import ThreadPoolExecutor
    from ..resolve import llm, ranking
    first = list(first or [])
    core = list(core or [])
    named = _bands_of(anchors, first)
    picked: list[Track] = []
    held: list[Track] = []
    artists: list[str] = []
    clue = (f" Shared sound: {', '.join(core[:6])}." if core else "") + _listener_context(taste)
    if context:
        clue += " Existing playlist's theme: " + "; ".join(
            f"{t.title} by {t.artist}" for t in context[:20])[:700]
    room = max(0, songs - len(first))
    if llm.available() and room:
        if room <= 80:
            if progress:
                progress("Asking Groq for songs that fit")
            asks = _groq_songs(what, round(room * 1.3), clue, brief,
                               have=[(t.artist, t.title) for t in first])
            if progress:
                progress(f"Checking {len(asks)} songs")
            with ThreadPoolExecutor(max_workers=6) as pool:
                found = [t for t in pool.map(lambda at: _found(*at), asks) if t]
                marks = list(pool.map(lambda t: _fits_sound(t, core), found)) if core \
                    else [1] * len(found)
            odd = _odd_ones(found, core, what)
            marks = [0 if t.key() in odd else m for t, m in zip(found, marks)]
            picked = [t for t, m in zip(found, marks) if m]
            held = [t for t, m in zip(found, marks) if not m]
            if progress:
                progress(f"Matched {len(picked)} real recordings", picked)
        else:
            if progress:
                progress("Asking Groq for bands that fit")
            artists = _groq_artists(what, max(12, room // 5), clue, brief)
    keep = {t.key() for t in first}
    rest = [t for t in _deal([picked]) if t.key() not in keep]
    # Every band they named gets a look in, whatever Groq thought.
    missing = [n for n in named if not any(ranking.artist_matches(t, n) for t in first + rest)]
    if missing and picked:
        got, off = _fill(missing, 2 * len(missing), taste, core, progress)
        rest = _blend(rest, got)
        held += off
    if len(first) + len(rest) < songs and named:
        got, off = _fill(named, songs - len(first) - len(rest), taste, core, progress)
        rest = _blend(rest, got)
        held += off
    if len(first) + len(rest) < songs:
        more = [a for a in artists if a.casefold() not in {n.casefold() for n in named}]
        more = more or _kin_bands(named, core, max(8, (songs - len(first) - len(rest)) // 3))
        got, off = _fill(more, songs - len(first) - len(rest), taste, core, progress,
                         label="more bands")
        rest = _blend(rest, got)
        held += off
    if len(first) + len(rest) < songs:
        rest = _blend(rest, held)
    # The songs they named lead, in their order; the rest keeps the worker's
    # running order, with only back-to-back repeats broken up.
    rest = [t for t in rest if t.key() not in keep]
    return first + _unclump(rest)


def build(what: str, *, songs: int = 0, minutes: int = 0, taste=None,
          progress=None, strict: bool = False,
          context: list[Track] | None = None, notes: dict | None = None) -> list[Track]:
    """The tracks, not yet saved anywhere. `notes`, if given, gets the
    planner's name for the list.

    With Groq: the planner reads the request into a brief first, and the
    worker picks to it. Without: the request is read locally and the named
    bands' best-known songs carry it.
    """
    from ..resolve import llm
    songs = wanted_size(songs, minutes)
    brief = None
    if llm.available():
        if progress:
            progress("Reading what you asked for")
        hint = _listener_context(taste)
        if context:
            hint += " Adding to a list with: " + "; ".join(
                f"{t.title} by {t.artist}" for t in context[:15])[:500]
        try:
            brief = curator.plan(what, hint=hint)
        except curator.Busy as busy:
            raise GroqBusy(busy.wait) from None
    anchors = _unmerge(anchors_of(what, model=brief is None))
    if brief is not None:
        anchors = _with_brief(anchors, brief, what)
    if not anchors and not (brief and brief.describe()):
        return []
    first = _named_songs(anchors, taste)
    songs = max(songs, len(first))
    named = _bands_of(anchors, first)
    strict = strict or bool(brief and brief.strict and named)
    core = list(brief.genres) if brief and brief.genres else (
        core_sound(named) if len(named) >= 2 or not strict else [])
    if progress and core:
        progress("Keeping to " + ", ".join(core[:4]))
    if not strict:
        tracks = _fits(what, anchors, songs, taste, progress, context, first, core, brief)
    else:
        rest = [a for a in anchors if a["kind"] in ("genre", "album")]
        share = math.ceil(songs * 1.15 / max(1, len(rest) + len(named)))
        bands, held = _fill(named, share * len(named), taste, core, progress)
        mixed = bands
        for a in rest:
            lane = (_lane_for_genre(a["name"], share, taste, progress) if a["kind"] == "genre"
                    else _lane_for_album(a["name"], a.get("artist", ""), taste))
            if progress:
                progress(f"{a['name']}: {len(lane)} songs", lane)
            mixed = _blend(mixed, lane)
        keep = {t.key() for t in first}
        mixed = [t for t in mixed if t.key() not in keep]
        if len(first) + len(mixed) < songs:
            mixed = _blend(mixed, [t for t in held if t.key() not in keep])
        tracks = first + _unclump(mixed)
    if taste is not None:
        tracks = [t for t in tracks if not taste.is_blocked(t)]
    if notes is not None and brief is not None:
        notes["title"] = brief.title
    return tracks[:songs]


def _with_brief(anchors: list[dict], brief, what: str) -> list[dict]:
    """The local reading plus what the planner adds -- never minus. A song or
    band the request doesn't actually mention is left out."""
    out = list(anchors)
    songs = {(a.get("artist", "").casefold(), a["name"].casefold())
             for a in out if a["kind"] == "song"}
    for artist, title in brief.literal:
        if (artist.casefold(), title.casefold()) not in songs:
            out.append({"kind": "song", "name": title, "artist": artist})
            songs.add((artist.casefold(), title.casefold()))
    bands = {a["name"].casefold() for a in out if a["kind"] == "artist"}
    bands |= {a.get("artist", "").casefold() for a in out if a["kind"] == "song"}
    for name in brief.artists:
        if name.casefold() not in bands and curator.named_in(name, what):
            out.append({"kind": "artist", "name": name, "artist": ""})
            bands.add(name.casefold())
    return out


def wanted_size(songs: int = 0, minutes: int = 0) -> int:
    if not songs:
        songs = max(10, round((minutes or 60) * 60 / 215))
    return max(1, min(MAX_SONGS, int(songs)))


def start(what: str, *, songs: int = 0, minutes: int = 0, name: str = "",
          store=None, taste=None, on_done=None, strict: bool = False,
          target: str = "", job_id: str = "") -> str:
    """Build in the background and save it as a playlist. Returns a job id."""
    import secrets
    owner = _store_key(store)
    job = job_id or secrets.token_hex(16)
    with _lock:
        now = time.time()
        if job_id:
            prior = _jobs.get(job_id)
            if (not prior or prior.get("_owner") != owner or
                    prior.get("state") != "waiting"):
                raise BuildBusy("That playlist build can no longer be resumed.")
        else:
            _admit_build(owner, now)
        _jobs[job] = {"what": what, "state": "building", "detail": "Reading what you asked for",
                      "at": now, "name": name, "target": target, "_owner": owner,
                      "count": 0, "found": 0, "previews": [], "strict": strict}

    def note(detail: str, tracks: list[Track] | None = None) -> None:
        with _lock:
            _jobs[job]["detail"] = detail
            if tracks:
                _jobs[job]["found"] += len(tracks)
                _jobs[job]["previews"] = [t.title for t in tracks[-6:] if t.title]

    def work() -> None:
        try:
            context = store.tracks(target) if store is not None and target else []
            notes: dict = {}
            tracks = build(what, songs=songs, minutes=minutes, taste=taste,
                           progress=note, strict=strict, context=context, notes=notes)
            if not tracks:
                raise RuntimeError(f"couldn't find anything for {what}")
            planned = notes.get("title", "")
            if "playlist" in planned.casefold() or planned.casefold() == what.strip().casefold():
                planned = ""
            title = target or name or planned or _creative_title(what, tracks)
            if store is not None:
                if not target:
                    title = _free(store, title)
                    store.create(title)
                saved = store.add_many(title, tracks)
                added = (saved or {}).get("added", len(tracks))
            else:
                added = len(tracks)
            mins = round(sum(t.duration or 215 for t in tracks) / 60)
            asked = wanted_size(songs, minutes)
            short = (f" -- {len(tracks)} of {asked} was all that fit"
                     if len(tracks) < asked else "")
            with _lock:
                _jobs[job].update(state="done", name=title, count=added, minutes=mins,
                                  detail=(f"{added} songs added" if target else
                                          f"{len(tracks)} songs, about {mins} minutes") + short)
            log.info("built %r: %d songs for %r", title, len(tracks), what)
            _forget_waiting(job)
            if on_done:
                on_done(title, tracks)
        except GroqBusy as busy:
            # Cooking, not failed: it goes again the moment Groq will answer.
            at = time.time() + busy.wait
            with _lock:
                _jobs[job].update(state="waiting", ready_at=at,
                                  detail=f"Cooking that up -- Groq is busy, starting in about {busy.wait:.0f}s")
            _keep_waiting(job, what, songs, minutes, name, strict, store, target)
            timer = threading.Timer(busy.wait, lambda: start(
                what, songs=songs, minutes=minutes, name=name, store=store, taste=taste,
                on_done=on_done, strict=strict, target=target, job_id=job))
            timer.daemon = True
            timer.start()
        except Exception as exc:
            with _lock:
                _jobs[job].update(state="failed", detail=str(exc)[:160])
            log.warning("playlist build failed for %r: %s", what, exc)

    try:
        threading.Thread(target=work, daemon=True, name="playlist-build").start()
    except RuntimeError:
        with _lock:
            _jobs[job].update(state="failed", detail="Could not start the playlist builder")
        raise BuildBusy("Could not start the playlist builder. Try again later.") from None
    return job


def _alike(a: str, b: str) -> float:
    """How alike two bands' Last.fm tags are, 0 to 1; 0 when either is unknown."""
    ta = {k: v for k, v in _tags(Track(title="", artist=a), artist=True).items() if k not in _JUNK_TAGS}
    tb = {k: v for k, v in _tags(Track(title="", artist=b), artist=True).items() if k not in _JUNK_TAGS}
    if not ta or not tb:
        return 0.0
    dot = sum(ta[k] * tb.get(k, 0) for k in ta)
    norm = math.sqrt(sum(v * v for v in ta.values())) * math.sqrt(sum(v * v for v in tb.values()))
    return dot / norm if norm else 0.0


def change(name: str, what: str, *, store, taste=None, progress=None) -> dict:
    """Change a list the way they asked: "more energetic", "no more Vampire
    Weekend or anything like them". The planner says what goes and what
    should come in; the worker judges the rows and finds the new songs; the
    list is rewritten once, with the old one kept to undo to."""
    from concurrent.futures import ThreadPoolExecutor
    from ..resolve import ranking
    pairs = store.rows(name)
    if not pairs:
        raise RuntimeError("That playlist is empty")
    tracks = [t for _, t in pairs]
    if progress:
        progress("Reading what you want changed")
    plan = curator.change(what, tracks)
    gone_bands = list(plan.drop_artists)
    if re.search(r"\b(like|similar|sound(s|ing)? like)\b", what, re.I):
        # "...or anything like them": the bands filed next to the ones named.
        # Deezer's neighbours first; then anyone in the list whose Last.fm
        # tags read like theirs (Death Cab isn't Vampire Weekend's neighbour
        # on Deezer, but the tags know).
        from .kin import kin
        named = [a for a in plan.drop_artists if curator.named_in(a, what)]
        for band in named:
            gone_bands += kin.prime(Track(title="", artist=band))[:25]
        here = list(dict.fromkeys((t.artist or "").split(",")[0].strip() for t in tracks))
        gone_bands += [b for b in here if any(_alike(b, n) >= .6 for n in named)]
    drop = {i for i, t in enumerate(tracks)
            if any(ranking.artist_matches(t, a) for a in gone_bands)}
    if plan.drop_rule:
        left = [i for i in range(len(tracks)) if i not in drop]
        if progress:
            progress(f"Checking {len(left)} songs: {plan.drop_rule}")
        drop |= {left[k] for k in curator.matching(plan.drop_rule, [tracks[i] for i in left])}
    kept = [p for i, p in enumerate(pairs) if i not in drop]
    added: list[Track] = []
    if plan.add:
        if progress:
            progress(f"Finding {plan.add} songs to add", [])
        asks = curator.songs(plan.brief, round(plan.add * 1.3),
                             have=[(t.artist, t.title) for _, t in kept[:40]])
        with ThreadPoolExecutor(max_workers=6) as pool:
            found = [t for t in pool.map(lambda at: _found(*at), asks) if t]
        have = {_loose(t) for _, t in kept}
        added = [t for t in _deal([found]) if _loose(t) not in have
                 and not (taste is not None and taste.is_blocked(t))][:plan.add]
        if progress and added:
            progress(f"Found {len(added)} songs", added)
    if not drop and not added:
        return {"dropped": 0, "added": 0, "summary": "Nothing needed changing"}
    mixed = _unclump(_spread(kept, [(t.to_dict(), t) for t in added]),
                     artist=lambda pair: pair[1].primary_artist())
    rows = [r for r, _ in mixed]
    saved = store.rewrite(name, rows)
    if not saved.get("ok"):
        raise RuntimeError(saved.get("message") or "Couldn't save the change")
    gone = [tracks[i] for i in sorted(drop)]
    return {"dropped": len(drop), "added": len(added), "summary": plan.summary,
            "gone": [f"{t.title} · {t.artist}" for t in gone[:8]]}


def start_change(name: str, what: str, *, store, taste=None, job_id: str = "") -> str:
    """change(), in the background. Returns a job id, watched like a build."""
    import secrets
    owner = _store_key(store)
    job = job_id or secrets.token_hex(16)
    with _lock:
        now = time.time()
        if job_id:
            prior = _jobs.get(job_id)
            if not prior or prior.get("_owner") != owner or prior.get("state") != "waiting":
                raise BuildBusy("That change can no longer be resumed.")
        else:
            _admit_build(owner, now)
        _jobs[job] = {"what": what, "state": "building", "detail": "Reading what you want changed",
                      "at": now, "name": name, "target": name, "kind": "change", "_owner": owner,
                      "count": 0, "found": 0, "previews": []}

    def note(detail: str, tracks: list[Track] | None = None) -> None:
        with _lock:
            _jobs[job]["detail"] = detail
            if tracks:
                _jobs[job]["found"] += len(tracks)
                _jobs[job]["previews"] = [t.title for t in tracks[-6:] if t.title]

    def work() -> None:
        try:
            got = change(name, what, store=store, taste=taste, progress=note)
            bits = []
            if got["dropped"]:
                bits.append(f"took out {got['dropped']}")
            if got["added"]:
                bits.append(f"added {got['added']}")
            detail = (got.get("summary") or "Changed it") + (f" ({' and '.join(bits)})" if bits else "")
            with _lock:
                _jobs[job].update(state="done", count=got["added"], dropped=got["dropped"],
                                  gone=got.get("gone", []), undo=bool(bits), detail=detail)
            log.info("changed %r for %r: -%d +%d", name, what, got["dropped"], got["added"])
        except curator.Busy as busy:
            with _lock:
                _jobs[job].update(state="waiting", ready_at=time.time() + busy.wait,
                                  detail=f"Groq is busy -- starting in about {busy.wait:.0f}s")
            timer = threading.Timer(busy.wait, lambda: start_change(
                name, what, store=store, taste=taste, job_id=job))
            timer.daemon = True
            timer.start()
        except Exception as exc:
            with _lock:
                _jobs[job].update(state="failed", detail=str(exc)[:160])
            log.warning("playlist change failed for %r: %s", name, exc)

    threading.Thread(target=work, daemon=True, name="playlist-change").start()
    return job


# Waiting jobs for the owner's own lists are written down, so a restart while
# Groq rests doesn't lose them; a listener's are held in memory.
def _waiting_file():
    from ..paths import data_dir
    return data_dir() / "playlist_jobs.json"


def _keep_waiting(job, what, songs, minutes, name, strict, store, target="") -> None:
    from .playlists import playlists
    if store is not playlists:
        return
    import json
    from ..paths import write_atomic
    rows = _read_waiting()
    rows[job] = {"what": what, "songs": songs, "minutes": minutes, "name": name,
                 "strict": strict, "target": target}
    try:
        write_atomic(_waiting_file(), json.dumps(rows))
    except Exception as exc:
        log.debug("couldn't keep a waiting playlist job: %s", exc)


def _read_waiting() -> dict:
    import json
    try:
        got = json.loads(_waiting_file().read_text("utf-8"))
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _forget_waiting(job) -> None:
    rows = _read_waiting()
    if rows.pop(job, None) is not None:
        import json
        from ..paths import write_atomic
        try:
            write_atomic(_waiting_file(), json.dumps(rows))
        except Exception:
            pass


def resume_waiting() -> int:
    """At start-up: anything still cooking when the app last stopped."""
    from .playlists import playlists
    from .taste import taste
    rows = _read_waiting()
    resumed = 0
    for job_id, r in rows.items():
        try:
            # Restored work is a fresh admission. Old saved job ids must not
            # bypass current resource limits or collide with another owner.
            start(r.get("what", ""), songs=int(r.get("songs") or 0), minutes=int(r.get("minutes") or 0),
                  name=r.get("name", ""), store=playlists, taste=taste,
                  strict=bool(r.get("strict", False)), target=r.get("target", ""))
            _forget_waiting(job_id)
            resumed += 1
        except BuildBusy:
            break
    return resumed


def job(job_id: str, *, store=_INTERNAL_JOB_READ) -> dict | None:
    owner = _store_key(store) if store is not _INTERNAL_JOB_READ else None
    with _lock:
        got = _jobs.get(job_id)
        if not got or owner is not None and got.get("_owner") != owner:
            return None
        return {key: value for key, value in got.items() if not key.startswith("_")}


def _title(what: str, n: int) -> str:
    return f"{what.strip().title()} ({n})"[:60]


def _creative_title(what: str, tracks: list[Track]) -> str:
    """Name the resulting mix, not the prompt or an arbitrary track count."""
    from ..resolve import llm
    import os
    if os.environ.get("MRS_TESTING") != "1" and llm.available() and not llm.resting(_WORKER):
        sample = "; ".join(f"{t.title} / {t.artist}" for t in tracks[:14])[:900]
        got = llm.ask_json(
            'Name a music playlist like a human DJ. JSON only: {"name":""}. '
            'Give a vivid, short, non-generic name inspired by the actual songs; '
            'never include "playlist", a song count, or simply repeat the prompt.',
            f"Listener asked: {what[:300]}. Songs: {sample}", timeout=10, model=_WORKER) or {}
        title = str(got.get("name") or "").strip().strip('"')[:60]
        if title and title.casefold() != what.strip().casefold() and "playlist" not in title.casefold():
            return title
    artists = list(dict.fromkeys(t.artist.split(",")[0].strip() for t in tracks if t.artist))
    return (f"{artists[0]} After Dark" if artists else _title(what, len(tracks)))[:60]


def _free(store, name: str) -> str:
    taken = {n.lower() for n in store.names()}
    if name.lower() not in taken:
        return name
    for i in range(2, 50):
        if f"{name} {i}".lower() not in taken:
            return f"{name} {i}"
    return name
