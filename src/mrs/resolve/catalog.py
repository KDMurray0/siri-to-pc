"""YouTube Music lookups.

Two rules everywhere: prefer the original over remixes, and never return the
30-second preview clips.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Track, is_derivative

log = get("catalog")

CREATE_NO_WINDOW = 0x08000000
_client = None
_client_lock = threading.Lock()
_prefs: list[str] = []

_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 1800


def client():
    global _client
    with _client_lock:
        if _client is None:
            from ytmusicapi import YTMusic
            _client = YTMusic()
        return _client


def validate() -> bool:
    """Confirm we can talk to YouTube Music.

    Filtered on purpose: an unfiltered search can return an artist card that
    ytmusicapi 1.8.1 fails to parse, which says nothing about connectivity.
    """
    rows = client().search("daft punk", filter="songs", limit=1)
    return bool(rows)


def set_preferences(artists: list[str]) -> None:
    global _prefs
    _prefs = [a.lower() for a in artists if a]


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value):
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = (time.time(), value)
    return value


# -- conversion --------------------------------------------------------

def _duration(row: dict) -> int:
    secs = row.get("duration_seconds")
    if isinstance(secs, int) and secs:
        return secs
    text = row.get("duration") or ""
    if isinstance(text, str) and ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
            out = 0
            for p in parts:
                out = out * 60 + p
            return out
        except Exception:
            return 0
    return 0


_THUMB_SIZE = re.compile(r"=w\d+-h\d+")
ART_PX = 544          # the player draws it at 152, and phones at 2x or 3x


def _bigger(url: str) -> str:
    """Ask googleusercontent for a usable size.

    Radio rows hand back a 60px thumbnail and search rows 120px, both of which
    look like mush blown up to the player's artwork. The size lives in the URL,
    so just ask for a bigger one.
    """
    return _THUMB_SIZE.sub(f"=w{ART_PX}-h{ART_PX}", url or "", count=1)


def _thumb(row: dict) -> str:
    # search rows say "thumbnails", watch/radio rows say "thumbnail" — miss the
    # second and every song the radio picks turns up with a blank cover
    thumbs = row.get("thumbnails") or row.get("thumbnail") or []
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails") or []
    if not thumbs:
        return ""
    try:
        best = sorted(thumbs, key=lambda t: t.get("width", 0))[-1].get("url", "")
    except Exception:
        best = thumbs[-1].get("url", "")
    return _bigger(best)


def to_track(row: dict, origin: str = "radio") -> Track:
    artists = row.get("artists") or []
    names = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    album = row.get("album")
    album_name = album.get("name", "") if isinstance(album, dict) else (album or "")
    return Track(
        video_id=row.get("videoId") or "",
        title=(row.get("title") or "").strip(),
        artist=names.strip(),
        album=album_name,
        art=_thumb(row),
        duration=_duration(row),
        origin=origin,
    )


def _acceptable(t: Track, *, allow_variant: bool = False) -> bool:
    if not t.video_id or not t.title:
        return False
    floor = int(config.get("min_duration", 60) or 0)
    # duration 0 means "unknown" — let the downloader's filter catch it
    if floor and t.duration and t.duration < floor:
        return False
    if not allow_variant and is_derivative(t.title):
        return False
    return True


def _prefer(tracks: list[Track]) -> list[Track]:
    """Nudge artists the listener actually plays to the front."""
    if not _prefs:
        return tracks
    def key(t: Track) -> int:
        return 0 if t.primary_artist() in _prefs else 1
    return sorted(tracks, key=key)


# -- searches ----------------------------------------------------------

def search_songs(query: str, limit: int = 12, *, allow_variant: bool = False) -> list[Track]:
    key = f"songs:{query}:{limit}:{allow_variant}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        rows = client().search(query, filter="songs", limit=max(limit, 10))
    except Exception as exc:
        log.warning("search failed for %r: %s", query, exc)
        return []
    out = [to_track(r, "request") for r in rows]
    out = [t for t in out if _acceptable(t, allow_variant=allow_variant)]
    return _store(key, _prefer(out)[:limit])


def search_candidates(query: str, limit: int = 12) -> list[Track]:
    """For the UI's pick-a-result dropdown."""
    return search_songs(query, limit=limit, allow_variant=True)


def artist_tracks(artist: str, limit: int = 20) -> list[Track]:
    key = f"artist:{artist}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    tracks: list[Track] = []
    try:
        found = client().search(artist, filter="artists", limit=1)
        if found:
            info = client().get_artist(found[0]["browseId"])
            songs = (info.get("songs") or {}).get("results") or []
            tracks = [to_track(r, "request") for r in songs]
    except Exception as exc:
        log.debug("artist lookup failed for %r: %s", artist, exc)
    if len(tracks) < limit:
        # Searching the band name also finds songs *called* that — asking for
        # Blur turned up "Blur" by Bella Kay and the queue called it same-artist.
        want = Track(title="", artist=artist).primary_artist()
        tracks += [t for t in search_songs(artist, limit=limit)
                   if not want or t.primary_artist() == want]
    seen, out = set(), []
    for t in tracks:
        if t.video_id and t.video_id not in seen and _acceptable(t):
            seen.add(t.video_id)
            out.append(t)
    return _store(key, out[:limit])


def search_artists(query: str, limit: int = 3) -> list[dict]:
    """Artist hits for the search dropdown."""
    try:
        rows = client().search(query, filter="artists", limit=limit)
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        name = r.get("artist") or r.get("title")
        if name:
            out.append({"kind": "artist", "name": name, "art": _thumb(r),
                        "subtitle": r.get("subscribers") or "Artist"})
    return out


def search_albums(query: str, limit: int = 3) -> list[dict]:
    """Album hits for the search dropdown."""
    try:
        rows = client().search(query, filter="albums", limit=limit)
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        title = r.get("title")
        if not title:
            continue
        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or [])
                            if a.get("name"))
        out.append({"kind": "album", "name": title, "artist": artists,
                    "art": _thumb(r),
                    "subtitle": f"Album{' · ' + artists if artists else ''}"})
    return out


def artist_all_tracks(artist: str, cap: int = 200) -> list[Track]:
    """Everything we can find by one artist — singles plus every album track.

    Asking for a band should play the band, not five songs and then drift, so
    this walks their albums instead of taking the handful `get_artist` returns.
    """
    key = f"artist_all:{artist}"
    hit = _cached(key)
    if hit is not None:
        return hit

    out: list[Track] = []
    seen: set[str] = set()
    seen_names: set[str] = set()

    def add(tracks: list[Track]) -> None:
        for t in tracks:
            if not t.video_id or t.video_id in seen or not _acceptable(t):
                continue
            # A discography walk hits the same song on the album, the deluxe
            # edition and the greatest hits — keep one.
            name = t.key()
            if name and name in seen_names:
                continue
            seen.add(t.video_id)
            if name:
                seen_names.add(name)
            out.append(t)

    browse_id = None
    try:
        found = client().search(artist, filter="artists", limit=1)
        if found:
            browse_id = found[0].get("browseId")
    except Exception as exc:
        log.debug("artist search failed for %r: %s", artist, exc)

    if browse_id:
        try:
            info = client().get_artist(browse_id)
            add([to_track(r, "request")
                 for r in (info.get("songs") or {}).get("results") or []])
            # Walk the discography for the rest.
            albums = (info.get("albums") or {}).get("results") or []
            singles = (info.get("singles") or {}).get("results") or []
            for entry in (albums + singles)[:25]:
                if len(out) >= cap:
                    break
                bid = entry.get("browseId")
                if not bid:
                    continue
                try:
                    al = client().get_album(bid)
                    art = _thumb(al)
                    rows = []
                    for r in al.get("tracks") or []:
                        t = to_track(r, "request")
                        t.artist = t.artist or artist
                        t.album = al.get("title", "")
                        t.art = t.art or art
                        rows.append(t)
                    add(rows)
                except Exception:
                    continue
        except Exception as exc:
            log.debug("artist walk failed for %r: %s", artist, exc)

    if len(out) < 20:
        add(search_songs(artist, limit=25))

    log.info("artist catalogue for %r: %d tracks", artist, len(out))
    return _store(key, out[:cap])


def album_tracks(album: str, artist: str = "", limit: int = 0) -> list[Track]:
    query = f"{album} {artist}".strip()
    try:
        rows = client().search(query, filter="albums", limit=1)
        if not rows:
            return []
        info = client().get_album(rows[0]["browseId"])
        art = _thumb(info)
        out = []
        for r in info.get("tracks") or []:
            t = to_track(r, "request")
            if not t.artist:
                t.artist = artist or ", ".join(
                    a.get("name", "") for a in (info.get("artists") or []))
            t.album = info.get("title", album)
            t.art = t.art or art
            if t.video_id:
                out.append(t)
        return out[:limit] if limit else out
    except Exception as exc:
        log.debug("album lookup failed for %r: %s", query, exc)
        return []


MB_API = "https://musicbrainz.org/ws/2"
MB_UA = {"User-Agent": "MusicRequestServer/2.0 (personal LAN music player)"}


# Channels that upload hours of wallpaper music. YouTube's Jazz and Ambient
# shelves are full of them, and "Relaxing Music" is not a jazz artist.
_FARM = re.compile(
    r"\b(bgm|lo-?fi|relax\w*|sleep\w*|study\w*|meditat\w*|calm\w*|soothing|"
    r"background|ambience|playlist|channel|topic|mix(es)?|vibes?|"
    r"instrumental[s]? \w+|\d+ hours?)\b", re.I)


def _is_channel(artist: str) -> bool:
    name = (artist or "").strip()
    if not name:
        return True
    return bool(_FARM.search(name))


ITUNES = "https://itunes.apple.com/search"
# Apple files things by format as well as by sound, and those buckets are what
# turns a search for classical into lullabies and show tunes.
_NOT_A_GENRE = {"children's music", "holiday", "musicals", "soundtrack",
                "karaoke", "spoken word", "books", "new age", "easy listening"}


def _from_itunes_genre(genre: str, limit: int) -> list[Track]:
    """Ask Apple's genre index, which is a real search rather than a shelf.

    attribute=genreTerm queries the whole catalogue by genre and comes back
    ordered by popularity, for any genre you can name — grime, bossa nova,
    shoegaze — in about half a second. It's the closest keyless thing to what
    Spotify does.

    Its own genre labels are too coarse to filter with: they call Stone Temple
    Pilots "Rock" and Alice In Chains "Alternative", so trusting them drops
    real results. Only the format buckets get dropped.
    """
    from concurrent.futures import ThreadPoolExecutor

    key = f"itunes:{genre}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit

    url = ITUNES + "?" + urllib.parse.urlencode(
        {"term": genre, "entity": "song", "attribute": "genreTerm",
         "limit": min(80, limit * 4)})
    try:
        req = urllib.request.Request(url, headers=MB_UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode("utf-8", "replace")).get("results", [])
    except Exception as exc:
        log.debug("itunes genre search failed for %r: %s", genre, exc)
        return []

    wanted: list[tuple[str, str]] = []
    per_artist: dict[str, int] = {}
    for row in rows:
        title = (row.get("trackName") or "").strip()
        artist = (row.get("artistName") or "").strip()
        bucket = (row.get("primaryGenreName") or "").strip().lower()
        if not title or not artist or bucket in _NOT_A_GENRE:
            continue
        if _is_channel(artist):
            continue
        who = Track(title="", artist=artist).primary_artist()
        if per_artist.get(who, 0) >= 2:
            continue
        per_artist[who] = per_artist.get(who, 0) + 1
        wanted.append((title, artist))
        if len(wanted) >= limit + 6:
            break

    if not wanted:
        return []

    deadline = time.monotonic() + 6.0

    def find(pair):
        if time.monotonic() > deadline:
            return None
        hits = search_songs(f"{pair[0]} {pair[1]}", limit=1)
        return hits[0] if hits else None

    with ThreadPoolExecutor(max_workers=5) as pool:
        found = list(pool.map(find, wanted))

    out, seen = [], set()
    for t in found:
        if t and t.video_id and t.video_id not in seen and _acceptable(t):
            seen.add(t.video_id)
            out.append(t)
        if len(out) >= limit:
            break
    if out:
        log.info("%s: %d tracks from apple's genre index, %d artists",
                 genre, len(out), len({t.primary_artist() for t in out}))
    return _store(key, out)


def _shelf_params(cats: dict, genre: str) -> str:
    """Find YouTube's own shelf for a genre.

    Their titles are compound — "Rap & hip-hop", "Reggae & caribbean",
    "Country & Americana", "R&B & soul" — so matching the whole title exactly
    found jazz, classical and metal and missed everything else. Match a word
    of the title instead, which keeps "pop" off the J-Pop shelf.
    """
    target = _squash(genre)
    if not target:
        return ""
    best = ""
    for group in cats.values():
        for cat in group or []:
            title = cat.get("title", "")
            if _squash(title) == target:
                return cat.get("params", "")
            words = {_squash(w) for w in re.split(r"[&/,]|\s+", title) if w}
            if target in words and not best:
                best = cat.get("params", "")
    return best


def _mb_genre_artists(genre: str, want: int = 8) -> list[str]:
    """Bands filed under this genre, from MusicBrainz. No key, no account.

    Asking MusicBrainz for *recordings* with the tag and then counting who made
    them beats asking it for artists directly — the artist search is a text
    match and returns Metallica and KISS for grunge, while the recordings are
    tagged by hand and give Mudhoney, Green River and Alice in Chains.
    """
    key = f"mbgenre:{genre}:{want}"
    hit = _cached(key)
    if hit is not None:
        return hit
    url = MB_API + "/recording?" + urllib.parse.urlencode(
        {"query": f'tag:"{genre}"', "fmt": "json", "limit": 100})
    try:
        req = urllib.request.Request(url, headers=MB_UA)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        log.debug("musicbrainz genre lookup failed for %r: %s", genre, exc)
        return []
    counts: dict[str, int] = {}
    for rec in data.get("recordings", []):
        credit = (rec.get("artist-credit") or [{}])[0]
        name = credit.get("name") or (credit.get("artist") or {}).get("name", "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    ranked = [a for a, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:want]
    if ranked:
        log.info("%s: %d artists from musicbrainz", genre, len(ranked))
    return _store(key, ranked)


def _from_genre_artists(genre: str, limit: int) -> list[Track]:
    """Popular songs by the bands MusicBrainz files under this genre.

    The tagged recordings themselves are mostly album tracks and B-sides —
    "Bushpusher Man" is real grunge but nobody asked for it. The artists are
    the useful part; YouTube already knows their well-known songs.
    """
    from concurrent.futures import ThreadPoolExecutor

    artists = _mb_genre_artists(genre)
    if not artists:
        return []
    with ThreadPoolExecutor(max_workers=5) as pool:
        batches = list(pool.map(lambda a: artist_tracks(a, 3)[:2], artists))
    out, seen = [], set()
    for batch in batches:
        for t in batch:
            if t.video_id and t.video_id not in seen and _acceptable(t):
                seen.add(t.video_id)
                out.append(t)
    return out[:limit]


def _from_genre_tag(genre: str, limit: int, budget: float = 6.0) -> list[Track]:
    """Tracks people have filed under this genre, resolved to playable ones.

    Last.fm knows what "grunge" means; YouTube's song search only knows the
    word. The names come back instantly but each still has to be found on
    YouTube, so they're looked up a few at a time against a time budget — one
    at a time took seven seconds before a note played.

    Capped per artist, because a genre's top tracks are its most famous band
    over and over: six of the first ten for grunge were Nirvana.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..core.tags import tagstore

    names = tagstore.top_tracks_for_tag(genre, limit * 3)
    if not names:
        return []

    per_artist: dict[str, int] = {}
    wanted: list[tuple[str, str]] = []
    for title, artist in names:
        who = Track(title="", artist=artist).primary_artist()
        if per_artist.get(who, 0) >= 2:
            continue
        per_artist[who] = per_artist.get(who, 0) + 1
        wanted.append((title, artist))
        if len(wanted) >= limit + 6:
            break

    deadline = time.monotonic() + budget

    def find(pair):
        if time.monotonic() > deadline:
            return None
        hits = search_songs(f"{pair[0]} {pair[1]}", limit=1)
        return hits[0] if hits else None

    with ThreadPoolExecutor(max_workers=5) as pool:
        found = list(pool.map(find, wanted))

    out, seen = [], set()
    for t in found:
        if t and t.video_id and t.video_id not in seen and _acceptable(t):
            seen.add(t.video_id)
            out.append(t)
        if len(out) >= limit:
            break
    if out:
        log.info("%s: %d tracks from the tag itself, %d artists",
                 genre, len(out), len({t.primary_artist() for t in out}))
    return out


def _shelf_tracks(genre: str, limit: int) -> list[Track]:
    """YouTube's own genre shelf, if it has one for this word."""
    out: list[Track] = []
    try:
        params = _shelf_params(client().get_mood_categories(), genre)
        if not params:
            return []
        for pl in (client().get_mood_playlists(params) or [])[:2]:
            try:
                items = client().get_playlist(pl["playlistId"], limit=limit)
                out += [to_track(r) for r in items.get("tracks", [])]
            except Exception:
                continue
    except Exception as exc:
        log.debug("mood lookup failed for %r: %s", genre, exc)
    return out[:limit]


def _word_search(genre: str, limit: int) -> list[Track]:
    """Searching the words. Knows nothing about genre — "grunge music" has
    returned Sk8er Boi and Anti-Hero — so filter it against the artists
    MusicBrainz says belong, when we know any."""
    loose = search_songs(f"{genre} music", limit=limit)
    known = {Track(title="", artist=a).primary_artist()
             for a in _mb_genre_artists(genre, 25)}
    if known:
        kept = [t for t in loose if t.primary_artist() in known]
        log.info("%s: word search gave %d, %d were on-genre",
                 genre, len(loose), len(kept))
        return kept
    return loose


def genre_tracks(genre: str, limit: int = 25) -> list[Track]:
    """Tracks for a genre, from whichever source can answer first.

    No single source is reliably best. Last.fm's tags are the most accurate
    when there's a key. Apple's genre index is a real search — any genre you
    can name, ranked by popularity, no key needed — and it's the main keyless
    route. MusicBrainz is hand-tagged and precise but obscure, so it covers
    the corners Apple ranks badly. YouTube's shelves are curated lists that
    only exist for a dozen broad names, so they're last.

    Taking a share from all four and interleaving them was tried: it queried
    four services every time, ran to fifteen seconds, and put each source's
    worst guess near the top. Asking in order and stopping when there's enough
    is faster and no worse.
    """
    key = f"genre:{genre}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit

    seen: set[str] = set()
    names: set[str] = set()
    per_artist: dict[str, int] = {}
    res: list[Track] = []

    def absorb(tracks: list[Track]) -> None:
        for t in tracks:
            if len(res) >= limit:
                return
            name, who = t.key(), t.primary_artist()
            if not t.video_id or t.video_id in seen or not _acceptable(t):
                continue
            if name and name in names:
                continue
            # two per band, and nothing from a wallpaper-music channel
            if _is_channel(t.artist) or per_artist.get(who, 0) >= 2:
                continue
            seen.add(t.video_id)
            if name:
                names.add(name)
            per_artist[who] = per_artist.get(who, 0) + 1
            res.append(t)

    for source in (_from_genre_tag, _from_itunes_genre,
                   _from_genre_artists, _shelf_tracks, _word_search):
        if len(res) >= limit:
            break
        try:
            absorb(source(genre, limit))
        except Exception as exc:
            log.debug("%s failed for %r: %s", source.__name__, genre, exc)

    log.info("%s: %d tracks, %d artists", genre, len(res), len(per_artist))
    return _store(key, res)


def _watch_rows(video_id: str, limit: int) -> list[dict]:
    """Radio rows for a track.

    ytmusicapi reads the Related tab's browse id before it parses the tracks,
    and YouTube stopped sending that key — so a KeyError('endpoint') threw away
    a response that had 50 perfectly good tracks in it. Try the library first
    (in case it gets fixed), then parse the same response ourselves.
    """
    try:
        wp = client().get_watch_playlist(videoId=video_id, radio=True, limit=limit + 10)
        return wp.get("tracks") or []
    except Exception as exc:
        log.debug("watch playlist fell back for %s: %s", video_id, exc)

    from ytmusicapi.navigation import TAB_CONTENT, nav
    from ytmusicapi.parsers.watch import parse_watch_playlist
    body = {"enablePersistentPlaylistPanel": True, "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
            "videoId": video_id, "playlistId": "RDAMVM" + video_id,
            "params": "wAEB"}
    resp = client()._send_request("next", body)
    watch = nav(resp, ["contents", "singleColumnMusicWatchNextResultsRenderer",
                       "tabbedRenderer", "watchNextTabbedResultsRenderer"])
    results = nav(watch, [*TAB_CONTENT, "musicQueueRenderer", "content",
                          "playlistPanelRenderer"], True)
    return parse_watch_playlist(results["contents"]) if results else []


def related(video_id: str, limit: int = 10) -> list[Track]:
    """The 'radio' continuation for a track."""
    if not video_id:
        return []
    key = f"radio:{video_id}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        rows = _watch_rows(video_id, limit)
    except Exception as exc:
        log.debug("radio failed for %s: %s", video_id, exc)
        return []
    out = []
    for r in rows:
        t = to_track(r)
        if t.video_id != video_id and _acceptable(t):
            out.append(t)
    return _store(key, out[:limit])


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# -- other sources -----------------------------------------------------

def search_soundcloud(query: str, limit: int = 3) -> list[Track]:
    return _ytdlp_search(f"scsearch{limit}:{query}", "soundcloud")


def search_bandcamp(query: str, limit: int = 3) -> list[Track]:
    return _ytdlp_search(f"bcsearch{limit}:{query}", "bandcamp")


def _ytdlp_search(spec: str, source: str) -> list[Track]:
    exe = shutil.which("yt-dlp")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "--dump-json", "--flat-playlist", "--no-warnings", spec],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        out.append(Track(
            video_id=d.get("id") or "",
            title=d.get("title") or "",
            artist=d.get("uploader") or d.get("channel") or "",
            url=d.get("url") or d.get("webpage_url") or "",
            duration=int(d.get("duration") or 0),
            art=d.get("thumbnail") or "",
            source=source,
        ))
    return out
