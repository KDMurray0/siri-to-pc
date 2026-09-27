"""Make a playlist of a given size from a description: bands, genres, or both.

"500 songs of nu metal and glam metal" is read into anchors -- here, two genres
-- and filled from each anchor's most-played songs, weighed against the
listener's own history. A band gives its likeliest songs; a genre gives its top
artists' likeliest few each, so a big list is many bands and not one band a
hundred times. Everything is dealt out so no act runs back to back.

It takes a while for a big list -- two lookups per artist -- so it runs aside
and the list fills in when it's done.
"""

from __future__ import annotations

import math
import threading
import time
from itertools import zip_longest

from ..config import config
from ..logging_setup import get
from ..models import Plan, Track

log = get("builder")

MAX_SONGS = 1000
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def anchors_of(what: str) -> list[dict]:
    """What the description names, as [{"kind": "artist"|"genre"|"song", "name"}]."""
    from ..resolve import parser
    from ..resolve.conjunction import looks_like_genre, split_seeds
    plan = parser.parse(what)
    if plan.kind == "mix" and plan.items:
        return [dict(i) for i in plan.items]
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


def _lane_for_artist(name: str, n: int, taste) -> list[Track]:
    from ..resolve import catalog, ranking
    return ranking.likely(catalog.artist_top_tracks(name), taste)[:n]


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


def _deal(lanes: list[list[Track]]) -> list[Track]:
    out, seen, keys = [], set(), set()
    for row in zip_longest(*lanes):
        for t in row:
            if t is None or t.video_id in seen or (t.key() and t.key() in keys):
                continue
            seen.add(t.video_id)
            if t.key():
                keys.add(t.key())
            out.append(t)
    return out


def build(what: str, *, songs: int = 0, minutes: int = 0, taste=None,
          progress=None) -> list[Track]:
    """The tracks, not yet saved anywhere."""
    anchors = anchors_of(what)
    if not anchors:
        return []
    if not songs:
        songs = max(10, round((minutes or 60) * 60 / 215))
    songs = max(1, min(MAX_SONGS, int(songs)))
    share = math.ceil(songs * 1.15 / len(anchors))          # a little over, for losses
    lanes = []
    for a in anchors:
        if a["kind"] == "genre":
            lanes.append(_lane_for_genre(a["name"], share, taste, progress))
        elif a["kind"] == "song":
            from ..resolve import resolver
            res = resolver.resolve(Plan(kind="song", query=a["name"], artist=a.get("artist", "")), taste)
            lane = list(res.tracks[:1]) if res else []
            if lane and lane[0].artist:
                lane += _lane_for_artist(lane[0].artist.split(",")[0], share - 1, taste)
            lanes.append(lane)
        else:
            lanes.append(_lane_for_artist(a["name"], share, taste))
        if progress:
            progress(f"{a['name']}: {len(lanes[-1])} songs")
    tracks = _deal([lane for lane in lanes if lane])
    if taste is not None:
        tracks = [t for t in tracks if not taste.is_blocked(t)]
    return tracks[:songs]


def start(what: str, *, songs: int = 0, minutes: int = 0, name: str = "",
          store=None, taste=None, on_done=None) -> str:
    """Build in the background and save it as a playlist. Returns a job id."""
    import secrets
    job = secrets.token_hex(4)
    with _lock:
        _jobs[job] = {"what": what, "state": "building", "detail": "Reading what you asked for",
                      "at": time.time(), "name": name, "count": 0}

    def note(detail: str) -> None:
        with _lock:
            _jobs[job]["detail"] = detail

    def work() -> None:
        try:
            tracks = build(what, songs=songs, minutes=minutes, taste=taste, progress=note)
            if not tracks:
                raise RuntimeError(f"couldn't find anything for {what}")
            title = name or _title(what, songs or len(tracks))
            if store is not None:
                title = _free(store, title)
                store.create(title)
                store.add_many(title, tracks)
            mins = round(sum(t.duration or 215 for t in tracks) / 60)
            with _lock:
                _jobs[job].update(state="done", name=title, count=len(tracks), minutes=mins,
                                  detail=f"{len(tracks)} songs, about {mins} minutes")
            log.info("built %r: %d songs for %r", title, len(tracks), what)
            if on_done:
                on_done(title, tracks)
        except Exception as exc:
            with _lock:
                _jobs[job].update(state="failed", detail=str(exc)[:160])
            log.warning("playlist build failed for %r: %s", what, exc)

    threading.Thread(target=work, daemon=True, name="playlist-build").start()
    return job


def job(job_id: str) -> dict | None:
    with _lock:
        got = _jobs.get(job_id)
        return dict(got) if got else None


def _title(what: str, n: int) -> str:
    return f"{what.strip().title()} ({n})"[:60]


def _free(store, name: str) -> str:
    taken = {n.lower() for n in store.names()}
    if name.lower() not in taken:
        return name
    for i in range(2, 50):
        if f"{name} {i}".lower() not in taken:
            return f"{name} {i}"
    return name
