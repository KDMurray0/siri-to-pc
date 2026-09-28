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


class GroqBusy(Exception):
    """Groq can't be asked right now; try again in `wait` seconds."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"Groq is resting for {wait:.0f}s")
        self.wait = max(5.0, float(wait))


def anchors_of(what: str) -> list[dict]:
    """What the description names, as [{"kind": "artist"|"genre"|"song", "name"}]."""
    from ..resolve import parser
    from ..resolve.conjunction import looks_like_genre, split_seeds
    plan = parser.parse(what)
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
    for t in hits or []:
        if ranking.artist_matches(t, artist) and ranking.title_matches(t, title):
            return t
    return None


_CURATE = (
    "You curate playlists. Reply with JSON only. Pick music that FITS the "
    "description: the named bands, albums and genres, and other artists that "
    "clearly share that sound. Prefer well-known songs -- hits and fan "
    "favourites, never obscure deep cuts. Vary the artists; at most three songs "
    "by any one artist.")


def _groq_songs(what: str, n: int) -> list[tuple[str, str]]:
    from ..resolve import llm
    got = llm.ask_json(_CURATE + ' Format: {"songs":[{"artist":"","title":""}]}',
                       f"{min(90, n)} songs that fit: {what}", timeout=30)
    if got is None and llm.resting() > 0:
        raise GroqBusy(llm.resting())
    rows = (got or {}).get("songs") or []
    return [(str(r.get("artist") or "").strip(), str(r.get("title") or "").strip())
            for r in rows if isinstance(r, dict) and r.get("artist") and r.get("title")]


def _groq_artists(what: str, n: int) -> list[str]:
    from ..resolve import llm
    got = llm.ask_json(_CURATE + ' Format: {"artists":[""]}, best-known first.',
                       f"{min(120, n)} artists that fit: {what}", timeout=30)
    if got is None and llm.resting() > 0:
        raise GroqBusy(llm.resting())
    return [str(a).strip() for a in (got or {}).get("artists") or [] if str(a).strip()]


def _fits(what: str, anchors: list[dict], songs: int, taste, progress) -> list[Track]:
    """Songs that fit rather than songs by: Groq's picks, checked against the
    catalogue; the named bands' own likeliest songs mixed in."""
    from concurrent.futures import ThreadPoolExecutor
    from ..resolve import llm
    named = [a["name"] for a in anchors if a["kind"] == "artist"]
    picked: list[Track] = []
    artists: list[str] = []
    if llm.available():
        if songs <= 80:
            if progress:
                progress("Asking Groq for songs that fit")
            asks = _groq_songs(what, round(songs * 1.3))
            if progress:
                progress(f"Checking {len(asks)} songs")
            with ThreadPoolExecutor(max_workers=6) as pool:
                picked = [t for t in pool.map(lambda at: _found(*at), asks) if t]
        else:
            if progress:
                progress("Asking Groq for bands that fit")
            artists = _groq_artists(what, max(12, songs // 5))
    if len(picked) < songs:
        if not artists:
            from .kin import kin
            for name in named:
                artists += kin.prime(Track(title="", artist=name))[:8]
        pool_names = list(dict.fromkeys(named + artists))
        per = max(2, min(6, songs // max(1, len(pool_names)) + 1))
        lanes = []
        for i, name in enumerate(pool_names):
            got = _lane_for_artist(name, per, taste)
            if got:
                lanes.append(got)
            if progress:
                progress(f"{name}: {i + 1} of {len(pool_names)} bands")
            if len(picked) + sum(len(x) for x in lanes) >= songs * 1.1:
                break
        picked += _deal(lanes)
    return _deal([picked])


def build(what: str, *, songs: int = 0, minutes: int = 0, taste=None,
          progress=None, strict: bool = True) -> list[Track]:
    """The tracks, not yet saved anywhere."""
    anchors = anchors_of(what)
    if not anchors:
        return []
    if not songs:
        songs = max(10, round((minutes or 60) * 60 / 215))
    songs = max(1, min(MAX_SONGS, int(songs)))
    if not strict:
        tracks = _fits(what, anchors, songs, taste, progress)
        if taste is not None:
            tracks = [t for t in tracks if not taste.is_blocked(t)]
        return tracks[:songs]
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
        elif a["kind"] == "album":
            lanes.append(_lane_for_album(a["name"], a.get("artist", ""), taste))
        else:
            lanes.append(_lane_for_artist(a["name"], share, taste))
        if progress:
            progress(f"{a['name']}: {len(lanes[-1])} songs")
    tracks = _deal([lane for lane in lanes if lane])
    if taste is not None:
        tracks = [t for t in tracks if not taste.is_blocked(t)]
    return tracks[:songs]


def start(what: str, *, songs: int = 0, minutes: int = 0, name: str = "",
          store=None, taste=None, on_done=None, strict: bool = True,
          job_id: str = "") -> str:
    """Build in the background and save it as a playlist. Returns a job id."""
    import secrets
    job = job_id or secrets.token_hex(4)
    with _lock:
        _jobs[job] = {"what": what, "state": "building", "detail": "Reading what you asked for",
                      "at": time.time(), "name": name, "count": 0, "strict": strict}

    def note(detail: str) -> None:
        with _lock:
            _jobs[job]["detail"] = detail

    def work() -> None:
        try:
            tracks = build(what, songs=songs, minutes=minutes, taste=taste, progress=note,
                           strict=strict)
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
            _forget_waiting(job)
            if on_done:
                on_done(title, tracks)
        except GroqBusy as busy:
            # Cooking, not failed: it goes again the moment Groq will answer.
            at = time.time() + busy.wait
            with _lock:
                _jobs[job].update(state="waiting", ready_at=at,
                                  detail=f"Cooking that up -- Groq is busy, starting in about {busy.wait:.0f}s")
            _keep_waiting(job, what, songs, minutes, name, strict, store)
            timer = threading.Timer(busy.wait, lambda: start(
                what, songs=songs, minutes=minutes, name=name, store=store, taste=taste,
                on_done=on_done, strict=strict, job_id=job))
            timer.daemon = True
            timer.start()
        except Exception as exc:
            with _lock:
                _jobs[job].update(state="failed", detail=str(exc)[:160])
            log.warning("playlist build failed for %r: %s", what, exc)

    threading.Thread(target=work, daemon=True, name="playlist-build").start()
    return job


# Waiting jobs for the owner's own lists are written down, so a restart while
# Groq rests doesn't lose them; a listener's are held in memory.
def _waiting_file():
    from ..paths import data_dir
    return data_dir() / "playlist_jobs.json"


def _keep_waiting(job, what, songs, minutes, name, strict, store) -> None:
    from .playlists import playlists
    if store is not playlists:
        return
    import json
    from ..paths import write_atomic
    rows = _read_waiting()
    rows[job] = {"what": what, "songs": songs, "minutes": minutes, "name": name, "strict": strict}
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
    for job_id, r in rows.items():
        start(r.get("what", ""), songs=int(r.get("songs") or 0), minutes=int(r.get("minutes") or 0),
              name=r.get("name", ""), store=playlists, taste=taste,
              strict=bool(r.get("strict", True)), job_id=job_id)
    return len(rows)


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
