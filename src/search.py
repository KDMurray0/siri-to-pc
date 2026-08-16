"""YouTube Music search layer using ytmusicapi.

Resolves a search-plan dict (produced by ``matching.build_search_plan``)
against YouTube Music's search and browse endpoints, returning concrete
tracks with video IDs that mpv can play via yt-dlp.

No authentication is required; ``YTMusic()`` with no arguments supports
search and public browsing.
"""

import time
from collections import OrderedDict

from ytmusicapi import YTMusic


# ── In-memory LRU cache ──────────────────────────────────────────

class _SearchCache:
    """Simple bounded LRU cache with TTL."""

    def __init__(self, max_size=500, ttl=1800):
        self._max = max_size
        self._ttl = ttl
        self._cache = OrderedDict()
        self._timestamps = {}

    def get(self, key):
        now = time.time()
        if key in self._cache:
            ts = self._timestamps.get(key, 0)
            if now - ts < self._ttl:
                # Move to end (most recently used)
                self._cache.move_to_end(key)
                return self._cache[key]
            else:
                # Expired
                self._cache.pop(key, None)
                self._timestamps.pop(key, None)
        return None

    def put(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
            self._cache[key] = value
            self._timestamps[key] = time.time()
        else:
            if len(self._cache) >= self._max:
                oldest, _ = self._cache.popitem(last=False)
                self._timestamps.pop(oldest, None)
            self._cache[key] = value
            self._timestamps[key] = time.time()


# Lazy global - initialised from config in app.py startup
_cache = None


def init_cache(max_size=500, ttl=1800):
    """Initialise the search cache with settings from config."""
    global _cache
    _cache = _SearchCache(max_size=max_size, ttl=ttl)


def _cache_key(kind, query, artist=None):
    return f"{kind}|{query.lower().strip()}|{artist or ''}"


# ── YTMusic client singleton ─────────────────────────────────────

_ytmusic = None


def get_client():
    global _ytmusic
    if _ytmusic is None:
        _ytmusic = YTMusic()
    return _ytmusic


def validate_client():
    """Smoke-test the ytmusicapi client. Raises on failure."""
    c = get_client()
    # A minimal search to verify connectivity and API structure
    try:
        _ = c.search("test", filter="songs", limit=1)
    except Exception:
        raise RuntimeError(
            "ytmusicapi search failed. YouTube may have changed its API. "
            "Try updating the package: pip install --upgrade ytmusicapi"
        )


# ── Config overrides (set by app.py at startup) ───────────────────

_artist_track_count = 20
_album_track_count = 0


def set_config(artist_track_count=None, album_track_count=None):
    """Allow app.py to pass config values without importing it back."""
    global _artist_track_count, _album_track_count
    if artist_track_count is not None:
        _artist_track_count = artist_track_count
    if album_track_count is not None:
        _album_track_count = album_track_count


# ── Public resolve function ──────────────────────────────────────

def resolve(plan):
    """Resolve a search-plan dict into concrete tracks.

    Args:
        plan: dict with keys from ``matching.build_search_plan``:
            query, artist, kind, shuffle, mode, spoken

    Returns a dict suitable for ``player.play()``:
        {
            "tracks": [ {"video_id": ..., "title": ..., "artist": ..., "album": ..., "duration": ...}, ... ],
            "fallbacks": ["videoId", ...],
            "shuffle": bool,
            "mode": str,
            "spoken": str,
        }

    Raises RuntimeError (wrapped) on API failures.
    """
    query = plan.get("query", "")
    artist = plan.get("artist")
    kind = plan.get("kind")  # None means "auto"
    shuffle = plan.get("shuffle", False)
    mode = plan.get("mode", "play")
    spoken = plan.get("spoken", query)

    if not query:
        return {
            "tracks": [],
            "fallbacks": [],
            "shuffle": shuffle,
            "mode": mode,
            "spoken": spoken,
            "error": "Empty query",
        }

    # Cache lookup — cached entry has only tracks, fallbacks, kind
    cache_key = _cache_key(kind or "auto", query, artist)
    if _cache is not None:
        cached = _cache.get(cache_key)
        if cached is not None:
            # Apply per-call mode/shuffle on a copy of the cached result
            return {
                "tracks": cached["tracks"],
                "fallbacks": cached["fallbacks"],
                "shuffle": False,  # overridden below by kind logic
                "mode": mode,
                "kind": cached.get("kind", kind or "song"),
                "spoken": spoken,
            } | _apply_shuffle(cached.get("kind", kind or "song"), shuffle)

    try:
        c = get_client()
        results = []
        fallbacks = []

        if kind == "song":
            tracks, fallbacks = _resolve_song(c, query, artist, kind_is_explicit=True)
            resolved_kind = "song"
        elif kind == "album":
            tracks = _resolve_album(c, query, artist)
            resolved_kind = "album"
        elif kind == "artist":
            tracks = _resolve_artist(c, query, artist)
            resolved_kind = "artist"
        elif kind == "genre":
            tracks = _resolve_theme(c, query)
            resolved_kind = "genre"
        elif artist:
            # explicit "X by <artist>" with no forced type -> that song
            tracks, fallbacks = _resolve_song(c, query, artist, kind_is_explicit=False)
            resolved_kind = "song"
        else:
            # Ambiguous "play X" -> interpret from the catalog (artist/genre/song)
            resolved_kind, tracks, fallbacks = _smart_auto(c, query, artist)

        # Cache only the stable parts (tracks, fallbacks, kind) — not mode/shuffle
        if _cache is not None:
            _cache.put(cache_key, {
                "tracks": tracks,
                "fallbacks": fallbacks,
                "kind": resolved_kind,
            })

        # Build the result dict with per-call mode/shuffle on a copy
        result = {
            "tracks": tracks,
            "fallbacks": fallbacks,
            "shuffle": False,  # default
            "mode": mode,
            "kind": resolved_kind,
            "spoken": spoken,
        }

        # Apply kind-specific shuffle defaults
        if resolved_kind == "album":
            result["shuffle"] = False  # albums always in order
        elif resolved_kind in ("artist", "genre") and shuffle is None:
            result["shuffle"] = True
        else:
            result["shuffle"] = shuffle if shuffle is not None else False

        # Update spoken with real metadata when available
        if tracks:
            t0 = tracks[0]
            title = t0.get("title", "")
            artist_name = t0.get("artist", "")
            album_name = t0.get("album", "")
            if resolved_kind == "album" and len(tracks) > 1:
                result["spoken"] = f"Streaming the album {album_name or spoken} by {artist_name}, {len(tracks)} tracks"
            elif resolved_kind == "artist":
                result["spoken"] = f"Streaming {len(tracks)} tracks by {artist_name}"
            elif resolved_kind == "genre":
                result["spoken"] = f"Playing a {query} mix, {len(tracks)} tracks"
            else:
                if title and artist_name:
                    result["spoken"] = f"Streaming {title} by {artist_name}"
                elif title:
                    result["spoken"] = f"Streaming {title}"

        return result

    except Exception as exc:
        return {
            "tracks": [],
            "fallbacks": [],
            "shuffle": shuffle if shuffle is not None else False,
            "mode": mode,
            "kind": kind or "song",
            "spoken": f"I could not search for {query} on YouTube Music: {exc}",
            "error": str(exc),
        }


# ── Auto-queue (Spotify-style radio) ─────────────────────────────

def get_radio(video_id, limit=10, exclude_ids=None):
    """Return related tracks for a Spotify-style auto-queue.

    Uses YouTube Music's radio (``get_watch_playlist(..., radio=True)``),
    which returns a station of songs similar to *video_id*. The seed track
    and any ids in *exclude_ids* are filtered out.

    Returns a list of track dicts (same shape as ``resolve``'s tracks).
    """
    exclude = set(exclude_ids or ())
    exclude.add(video_id)
    try:
        c = get_client()
        wp = c.get_watch_playlist(videoId=video_id, radio=True, limit=max(limit * 3, 25))
    except Exception:
        return []

    out = []
    for t in wp.get("tracks", []):
        vid = t.get("videoId")
        if not vid or vid in exclude:
            continue
        exclude.add(vid)
        out.append(_radio_track_dict(t))
        if len(out) >= limit:
            break
    return out


def _radio_track_dict(t):
    """Convert a watch-playlist track to a standard track dict."""
    arts = t.get("artists") or []
    if isinstance(arts, list):
        artist_name = ", ".join(a.get("name", "") for a in arts if isinstance(a, dict) and a.get("name"))
    else:
        artist_name = str(arts) if arts else ""
    album = t.get("album")
    if isinstance(album, dict):
        album_name = album.get("name", "")
    else:
        album_name = str(album) if album else ""
    # length is a "m:ss" string
    dur = 0
    length = t.get("length")
    if length:
        parts = str(length).split(":")
        try:
            dur = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
        except (ValueError, IndexError):
            dur = 0
    return {
        "video_id": t.get("videoId", ""),
        "title": t.get("title", ""),
        "artist": artist_name,
        "album": album_name,
        "duration": dur,
        "thumbnail": _best_thumb(t.get("thumbnail") or t.get("thumbnails")),
    }


# ── Per-kind resolvers ───────────────────────────────────────────

def _resolve_song(client, query, artist=None, kind_is_explicit=True):
    """Search for a single song. Returns (tracks_list, fallbacks_list)."""
    search_query = f"{query} {artist}".strip() if artist else query
    results = client.search(search_query, filter="songs", limit=5)

    if not results:
        return [], []

    # Filter to the requested artist when known and results include artist info
    candidates = results
    if artist:
        filtered = [
            r for r in results
            if r.get("artists") and any(
                artist.lower() in a.get("name", "").lower()
                for a in r["artists"]
            )
        ]
        if filtered:
            candidates = filtered

    if not candidates:
        return [], []

    # Build track dicts
    tracks = [_track_dict(r) for r in candidates[:1]]
    fallbacks = [r.get("videoId") for r in candidates[1:] if r.get("videoId")]

    return tracks, fallbacks


def _resolve_album(client, query, artist=None):
    """Search for an album and return all tracks in correct order."""
    search_query = f"{query} {artist}".strip() if artist else query
    results = client.search(search_query, filter="albums", limit=3)

    # Filter to the requested artist when known
    if artist:
        filtered = [
            r for r in results
            if r.get("artists") and any(
                artist.lower() in a.get("name", "").lower()
                for a in r["artists"]
            )
        ]
        if filtered:
            results = filtered

    if not results:
        return []

    album = results[0]
    browse_id = album.get("browseId")
    if not browse_id or not browse_id.startswith("MPREb"):
        return []

    try:
        album_data = client.get_album(browse_id)
    except Exception:
        return []

    tracks_raw = album_data.get("tracks", [])

    # Apply album_track_count limit (0 means unlimited)
    if _album_track_count and _album_track_count > 0:
        tracks_raw = tracks_raw[:_album_track_count]

    tracks = []
    for t in tracks_raw:
        vid = t.get("videoId")
        if not vid:
            continue
        tracks.append({
            "video_id": vid,
            "title": t.get("title", ""),
            "artist": t.get("artist", album_data.get("artists", [{}])[0].get("name", "")),
            "album": album_data.get("title", ""),
            "duration": _parse_duration(t),
        })

    return tracks


import re as _re

_mood_categories_cache = None
_COMPILATION_RE = _re.compile(
    r'full\s+album|\bmix\b|\bmegamix\b|playlist|compilation|non[\s-]?stop|'
    r'\d+\s*(?:hour|hr|hrs|minutes?|min)\b|greatest\s+hits', _re.IGNORECASE)


def _norm(s):
    return _re.sub(r'\s+', ' ', _re.sub(r'[^\w\s]', ' ', (s or "").lower())).strip()


def _squash(s):
    return _re.sub(r'[^a-z0-9]', '', (s or "").lower())


def _is_compilation(title, duration):
    """Heuristic: skip hour-long mixes / 'full album' uploads."""
    if duration and duration > 900:
        return True
    return bool(title and _COMPILATION_RE.search(title))


# ── Taste preferences (for same-title conflict resolution) ───────────
_liked_artists = set()

# Derivative/low-quality versions to avoid unless the user explicitly asks.
_DERIV_RE = _re.compile(
    r'\b(remix|sped\s*-?up|spedup|slowed|reverb|nightcore|8d|mashup|bootleg|'
    r'karaoke|instrumental|cover|lo-?fi|nightcore|tiktok|edit|rework|flip|'
    r'live|acoustic|piano version|guitar version|remaster.*remix)\b', _re.IGNORECASE)


def set_preferences(liked_artists=None):
    """Update the set of artists the user listens to (normalised names)."""
    global _liked_artists
    if liked_artists is not None:
        _liked_artists = {_norm(a) for a in liked_artists if a}


def is_derivative(title):
    """True for remixes / sped-up / covers / karaoke etc."""
    return bool(title and _DERIV_RE.search(title))


def _taste_match(artist_norm):
    return bool(artist_norm and any(
        artist_norm == la or la in artist_norm or artist_norm in la
        for la in _liked_artists))


def _pick_song(song_hits, query):
    """Choose among same-title songs. Weighs three things:
      • popularity — YouTube Music already ranks the canonical version first;
      • taste — a version by an artist you listen to (which captures your
        genre: into punk -> your punk artists win the tie);
      • quality — remixes / sped-up / covers are avoided unless you asked.
    """
    want_variant = is_derivative(query)
    best, best_score = None, -1e9
    for i, s in enumerate(song_hits):
        score = -0.7 * i                       # popularity (earlier = better)
        if _taste_match(_norm(_artist_name(s))):
            score += 4.0                       # your artists / genre
        if not want_variant and is_derivative(s.get("title")):
            score -= 6.0                       # no stupid remixes
        if score > best_score:
            best_score, best = score, s
    return best or (song_hits[0] if song_hits else None)


def _mood_categories(client):
    global _mood_categories_cache
    if _mood_categories_cache is None:
        try:
            _mood_categories_cache = client.get_mood_categories()
        except Exception:
            _mood_categories_cache = {}
    return _mood_categories_cache


def _match_mood_category(client, query, strict=False):
    """Match *query* to a live YTM mood/genre category; return its params/None.

    strict: only a whole-query match ("jazz"=="Jazz"), never a loose token
    overlap ("swing" in "sultans of swing") — that must not beat a real song.
    """
    qsq = _squash(query)
    qtokens = set(_norm(query).split())
    loose = None
    for section in _mood_categories(client).values():
        for cat in section:
            title = cat.get("title")
            tsq = _squash(title)
            if not tsq:
                continue
            if tsq == qsq:                       # jazz == Jazz, hiphop == Hip-Hop
                return cat.get("params")
            if not strict:
                ttokens = set(_norm(title).split())
                if qtokens & ttokens:            # "country" ~ "Country & Americana"
                    loose = loose or cat.get("params")
    return loose


def _resolve_theme(client, query, limit=30):
    """Build a themed queue for any genre / mood / vibe.

    Uses a filtered song search (reliable) as the backbone and extends it with
    a radio station seeded from the top result, so the queue keeps the vibe and
    stays varied. Works for arbitrary themes ("rainy day", "coding", "80s",
    "gym") — nothing is hardcoded. A curated mood/genre playlist is used as a
    bonus when YouTube Music's (flaky) endpoint cooperates.
    """
    seen = set()
    out = []

    def add(track):
        vid = track.get("video_id")
        if vid and vid not in seen and not _is_compilation(track.get("title"), track.get("duration")):
            seen.add(vid)
            out.append(track)

    # 1) curated mood/genre category (best-effort; endpoint can error)
    params = _match_mood_category(client, query)
    if params:
        try:
            playlists = client.get_mood_playlists(params)
            qsq = _squash(query)
            chosen = next((p for p in playlists if qsq and qsq in _squash(p.get("title"))),
                          None) or (playlists[0] if playlists else None)
            if chosen and chosen.get("playlistId"):
                data = client.get_playlist(chosen["playlistId"], limit=limit * 2)
                for t in data.get("tracks", []):
                    if t.get("videoId"):
                        add(_track_dict(t))
        except Exception:
            pass

    # 2) filtered song search (reliable backbone)
    if len(out) < limit:
        try:
            for s in client.search(query, filter="songs", limit=limit * 2):
                if s.get("videoId"):
                    add(_track_dict(s))
                if len(out) >= limit:
                    break
        except Exception:
            pass

    # 3) extend with a radio station seeded from the top track (keeps the vibe)
    if out and len(out) < limit:
        for t in get_radio(out[0]["video_id"], limit=limit, exclude_ids=list(seen)):
            add(t)
            if len(out) >= limit:
                break

    return out[:limit]


def _artist_name(r):
    a = r.get("artists") or r.get("artist")
    if isinstance(a, list) and a:
        return a[0].get("name", "") if isinstance(a[0], dict) else str(a[0])
    return r.get("artist") or r.get("title") or ""


def _smart_auto(client, query, artist=None):
    """Interpret an ambiguous "play X" like an assistant would.

    Decides — from the YouTube Music catalog, not a hardcoded list — whether
    *query* is a genre/mood/vibe, an artist, or a single song. The rule:
      • an artist whose name *exactly* matches the query, and no equally-exact
        song, means the artist (their mix);
      • an exact or contained song-title match means that song;
      • anything else is treated as a theme/vibe and gets a themed queue.
    Returns (kind, tracks, fallbacks).
    """
    qsq = _squash(query)
    qtokens = set(_norm(query).split())
    # Decades ("80s", "2000s") are themes, even though songs of that name exist.
    if _re.match(r'^(19|20)?\d0s$', qsq):
        return "genre", _resolve_theme(client, query), []

    # YTM's unfiltered top result encodes prominence: for a bare artist name
    # ("queen", "korn") the top hit is the ARTIST, which should win over an
    # obscure song that merely shares the title. Only fires when the whole query
    # equals the artist's name, so "bohemian rhapsody queen" is unaffected.
    try:
        top = client.search(query, limit=3)
    except Exception:
        top = []
    if top and top[0].get("resultType") == "artist" \
            and _squash(_artist_name(top[0])) == qsq:
        return "artist", _resolve_artist(client, query), []

    # Songs first: a real title must beat a loose genre-word overlap.
    try:
        songs = [s for s in client.search(query, filter="songs", limit=8) if s.get("videoId")]
    except Exception:
        songs = []
    try:
        arts = client.search(query, filter="artists", limit=2)
    except Exception:
        arts = []

    artist_exact = bool(qsq) and any(_squash(_artist_name(a)) == qsq for a in arts[:2])

    # A song is meant when the query exactly matches a title, OR it names both a
    # title and an artist ("coming undone korn", "bohemian rhapsody queen").
    def song_score(s):
        tt = set(_norm(_core_title(s.get("title"))).split())
        at = set(_norm(_artist_name(s)).split())
        exact = _squash(_core_title(s.get("title"))) == qsq
        strong = bool(tt) and tt <= qtokens and bool(at & qtokens)
        return exact, strong, tt, at

    scored = [(song_score(s), s) for s in songs]
    exact_hit = any(sc[0] for sc, _ in scored)
    song_hits = [s for sc, s in scored if sc[0] or sc[1]]

    # A genre/mood term -> theme. A single word ("punk", "jazz") only needs a
    # loose category match and beats an obscure same-named song; multi-word
    # queries need a whole-query match and no exact song ("sultans of swing").
    single = len(qtokens) == 1
    if _match_mood_category(client, query, strict=not single) and (single or not exact_hit):
        return "genre", _resolve_theme(client, query), []

    if song_hits:
        # Conflict resolution: weigh popularity + your taste (your artists /
        # genre) and avoid remixes/covers unless you asked for one.
        best = _pick_song(song_hits, query)
        rest = [s["videoId"] for s in songs if s is not best][:5]
        return "song", [_track_dict(best)], rest

    if artist_exact:
        return "artist", _resolve_artist(client, query), []
    # Neither a clear artist nor a clear song -> a theme / vibe.
    return "genre", _resolve_theme(client, query), []


def _core_title(title):
    """Song title with parenthetical/bracketed/feat. suffixes removed."""
    t = _re.sub(r'[\(\[].*?[\)\]]', '', title or '')
    t = _re.sub(r'\b(feat|ft|featuring|with)\b.*$', '', t, flags=_re.IGNORECASE)
    return t.strip()


def _resolve_artist(client, query, artist=None):
    """Search for an artist and return top tracks (configurable count)."""
    max_tracks = _artist_track_count or 20

    search_query = query or artist
    results = client.search(search_query, filter="artists", limit=3)

    if not results:
        # Fall back to a song search grouped by this artist
        tracks_raw = client.search(search_query, filter="songs", limit=max_tracks)
        return [_track_dict(r) for r in tracks_raw[:max_tracks]]

    artist_obj = results[0]
    browse_id = artist_obj.get("browseId")
    if not browse_id or not browse_id.startswith("UC"):
        # Not a proper artist; fall back to song search
        tracks_raw = client.search(search_query, filter="songs", limit=max_tracks)
        return [_track_dict(r) for r in tracks_raw[:max_tracks]]

    try:
        artist_data = client.get_artist(browse_id)
    except Exception:
        tracks_raw = client.search(search_query, filter="songs", limit=max_tracks)
        return [_track_dict(r) for r in tracks_raw[:max_tracks]]

    # get_artist returns songs as {"browseId": ..., "results": [...]}
    songs_list = artist_data.get("songs", {}).get("results", []) if isinstance(artist_data.get("songs"), dict) else artist_data.get("songs", [])
    if len(songs_list) < max_tracks // 2:
        # Top up with a direct song search
        extra_limit = max_tracks - len(songs_list)
        extra = client.search(search_query, filter="songs", limit=extra_limit)
        seen_ids = {s.get("videoId") for s in songs_list}
        for e in extra:
            if e.get("videoId") not in seen_ids and len(songs_list) < max_tracks:
                songs_list.append(e)
                seen_ids.add(e.get("videoId"))

    return [_track_dict(s) for s in songs_list[:max_tracks]]


# ── Helpers ──────────────────────────────────────────────────────

def _apply_shuffle(kind, shuffle):
    """Return {"shuffle": bool} with kind-specific defaults applied."""
    if kind == "album":
        return {"shuffle": False}
    elif kind in ("artist", "genre") and shuffle is None:
        return {"shuffle": True}
    else:
        return {"shuffle": shuffle if shuffle is not None else False}


def _parse_duration(result):
    """Seconds from a ytmusicapi result: int ``duration_seconds`` or "m:ss"."""
    duration = result.get("duration_seconds")
    if isinstance(duration, int):
        return duration
    duration_str = result.get("duration") or "0:00"
    if isinstance(duration_str, (int, float)):
        return int(duration_str)
    try:
        secs = 0
        for part in str(duration_str).split(":"):
            secs = secs * 60 + int(part)
        return secs
    except (ValueError, IndexError):
        return 0


def _track_dict(result):
    """Convert a ytmusicapi search result to a track dict."""
    video_id = result.get("videoId", "")
    title = result.get("title", "")
    duration = _parse_duration(result)

    # ytmusicapi returns "artists" as a list of dicts with "name" and "id"
    artist_list = result.get("artists", [])
    if isinstance(artist_list, list):
        artist_name = ", ".join(a.get("name", "") for a in artist_list if isinstance(a, dict) and a.get("name"))
    else:
        artist_name = str(artist_list) if artist_list else ""

    album_obj = result.get("album", {})
    if isinstance(album_obj, dict):
        album_name = album_obj.get("name", "")
    else:
        album_name = str(album_obj) if album_obj else ""

    return {
        "video_id": video_id,
        "title": title,
        "artist": artist_name,
        "album": album_name,
        "duration": duration,
        "thumbnail": _best_thumb(result.get("thumbnails")),
    }


def search_candidates(query, limit=6):
    """Top song matches for a 'pick the right one' list (no playback)."""
    try:
        rows = get_client().search(query, filter="songs", limit=limit)
    except Exception:
        return []
    out = []
    for r in rows:
        if not r.get("videoId"):
            continue
        t = _track_dict(r)
        out.append({"video_id": t["video_id"], "title": t["title"],
                    "artist": t["artist"], "album": t["album"], "art": t["thumbnail"]})
        if len(out) >= limit:
            break
    return out


def _best_thumb(thumbs):
    """Return the largest thumbnail URL from a ytmusicapi thumbnails list."""
    if not thumbs or not isinstance(thumbs, list):
        return ""
    try:
        return sorted(thumbs, key=lambda t: t.get("width", 0))[-1].get("url", "")
    except Exception:
        return thumbs[-1].get("url", "") if thumbs else ""


# ── External sources (SoundCloud / Bandcamp via yt-dlp) ──────────────

_ytdlp_search_path = None


def resolve_external(query, source="soundcloud", limit=1):
    """Resolve a query against a non-YouTube source using yt-dlp search.

    Returns tracks carrying a direct ``url`` (played by the download layer).
    SoundCloud supports search (``scsearch``); Bandcamp resolves a URL only.
    """
    import shutil as _sh, subprocess as _sp, json as _json
    global _ytdlp_search_path
    if _ytdlp_search_path is None:
        _ytdlp_search_path = _sh.which("yt-dlp") or ""
    if not _ytdlp_search_path:
        return []

    if source == "soundcloud":
        spec = f"scsearch{max(1, limit)}:{query}"
    elif source == "bandcamp":
        spec = f"bcsearch{max(1, limit)}:{query}"
    else:
        spec = f"ytsearch{max(1, limit)}:{query}"
    try:
        out = _sp.run(
            [_ytdlp_search_path, spec, "--flat-playlist", "-J",
             "--no-warnings", "--extractor-args", "soundcloud:formats=none"],
            stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=30,
            creationflags=0x08000000,
        )
        data = _json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception:
        return []

    tracks = []
    for e in (data.get("entries") or []):
        url = e.get("url") or e.get("webpage_url")
        if not url:
            continue
        tracks.append({
            "video_id": (e.get("id") or url),
            "url": url,
            "title": e.get("title", ""),
            "artist": e.get("uploader") or e.get("channel") or "",
            "album": "",
            "duration": int(e.get("duration") or 0),
            "thumbnail": e.get("thumbnail") or "",
            "source": source,
        })
    return tracks
