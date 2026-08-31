"""What a song is, past its name.

Three sources, and none of them invented. What this machine already knows —
the year, the tags, how often you've played it — costs nothing. Last.fm has
a write-up for most records. Wikipedia has the stories: How You Remind Me
came out of an argument, Money for Nothing out of a man complaining in a
television shop, and both of those are sitting in a section called
Background that nothing here was reading.

The LLM only ever shortens what those sources said. Asked to supply facts
of its own it will happily make some up, and a confident invention about a
real record is worse than a blank panel.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Track, _fold
from ..paths import data_dir, write_atomic

log = get("insights")

UA = {"User-Agent": "MusicRequestServer/2.0 (personal music player)"}
WIKI = "https://en.wikipedia.org/w/api.php"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
MAX_ENTRIES = 600          # a year of listening, roughly
STORY_CHARS = 1400         # what gets kept, and what the LLM is shown

# The sections worth reading. Wikipedia puts the interesting part under
# whichever of these the editor happened to pick.
_WANTED = re.compile(
    r"^==+\s*(background|writing|composition|inspiration|conception|"
    r"origin|recording|history|writing and recording|"
    r"background and writing|background and recording|"
    r"writing and composition|composition and lyrics|lyrics)"
    r"\s*==+\s*$", re.I | re.M)
_HEADING = re.compile(r"^==+\s*.+?\s*==+\s*$", re.M)
_TIDY = re.compile(r"\s*\n\s*")
# What an upload hangs off the end of a title. Searching Wikipedia for
# "Money For Nothing (Remastered 1996)" found the article for a Dire Straits
# live album and filed its history under the song.
_JUNK = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*|\s+-\s+.*$", re.I)


def _plain(title: str) -> str:
    """The record's name, without the upload's decorations."""
    out = _JUNK.sub(" ", title or "").strip(" -–—")
    return re.sub(r"\s{2,}", " ", out) or (title or "").strip()


def _get(url: str, timeout: float = 8.0):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        log.debug("fetch failed %s: %s", url.split("?")[0], exc)
        return None


# -- Wikipedia ---------------------------------------------------------

def _wiki_page(title: str, artist: str) -> str | None:
    """The article about this record, not about the band or the phrase."""
    title = _plain(title)
    q = f"{title} {artist} song".strip()
    data = _get(WIKI + "?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": q,
        "srlimit": "5", "format": "json"}))
    hits = ((data or {}).get("query") or {}).get("search") or []
    want = _fold(title.lower())
    for row in hits:
        name = row.get("title") or ""
        low = _fold(name.lower())
        # "(song)" is the giveaway; failing that, the article has to at
        # least be named after the record rather than merely mention it.
        if "song" in name.lower() or low == want or low.startswith(want + " "):
            return name
    # No article actually named after the record. Taking the best-scoring
    # hit anyway is how a song ended up showing the history of a live album
    # that merely mentions it.
    return None


def _wiki_text(page: str) -> str:
    data = _get(WIKI + "?" + urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": "1",
        "redirects": "1", "titles": page, "format": "json"}))
    pages = ((data or {}).get("query") or {}).get("pages") or {}
    for row in pages.values():
        text = row.get("extract") or ""
        if text:
            return text
    return ""


def _story_from(text: str) -> str:
    """The background section, or the opening if there isn't one.

    Subsections come with it. "Money for Nothing" files the story of the man
    in the appliance shop under Composition > Lyrics, and stopping at the
    next heading of any depth stopped at the section's own first subheading
    — which left an empty Composition and fell back to the lead paragraph.
    """
    if not text:
        return ""
    parts: list[str] = []
    seen = 0
    for m in _WANTED.finditer(text):
        if m.start() < seen:
            continue                  # already inside a section we took
        depth = len(m.group(0)) - len(m.group(0).lstrip("="))
        rest = text[m.end():]
        end = len(rest)
        for h in _HEADING.finditer(rest):
            if len(h.group(0)) - len(h.group(0).lstrip("=")) <= depth:
                end = h.start()
                break
        seen = m.end() + end
        body = _HEADING.sub(" ", rest[:end])      # drop the subheadings
        body = _TIDY.sub(" ", body).strip()
        if len(body) > 120:
            parts.append(body)
        if sum(len(p) for p in parts) >= STORY_CHARS:
            break
    if parts:
        return " ".join(parts)[:STORY_CHARS]
    lead = text.split("\n==", 1)[0]
    return _TIDY.sub(" ", lead).strip()[:STORY_CHARS]


# -- Last.fm -----------------------------------------------------------

def _lastfm(title: str, artist: str) -> dict:
    title = _plain(title)
    key = config.get("lastfm_api_key") or ""
    if not key or not artist:
        return {}
    data = _get(LASTFM + "?" + urllib.parse.urlencode({
        "method": "track.getInfo", "api_key": key, "format": "json",
        "track": title, "artist": artist, "autocorrect": "1"}))
    row = (data or {}).get("track") or {}
    if not row:
        return {}
    wiki = (row.get("wiki") or {}).get("summary") or ""
    wiki = re.sub(r"<a href.*?</a>", "", wiki).strip()
    return {
        "listeners": int(row.get("listeners") or 0),
        "playcount": int(row.get("playcount") or 0),
        "album": ((row.get("album") or {}).get("title") or ""),
        "url": row.get("url") or "",
        "summary": _TIDY.sub(" ", wiki),
    }


# -- shortening --------------------------------------------------------

_ASK = ("You are given text about one song. Reply with JSON: "
        '{"facts": ["...", "..."]}. Two or three short sentences, each a '
        "separate string, each a specific thing about how the song came "
        "about or what happened to it. Use ONLY what the text says — if the "
        "text has nothing specific, return an empty list. No opinions, no "
        "praise, no summarising the sound.")


def _facts(story: str, title: str, artist: str) -> list[str]:
    from . import llm
    if not story or not llm.available():
        return []
    try:
        payload = llm._post({
            "model": llm._model(), "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _ASK},
                {"role": "user",
                 "content": f"Song: {title} by {artist}\n\n{story[:STORY_CHARS]}"},
            ]}, float(config.get("groq_timeout", 8)))
        got = json.loads(payload["choices"][0]["message"]["content"])
    except Exception as exc:
        log.debug("couldn't shorten the story: %s", exc)
        return []
    out = [str(f).strip() for f in (got.get("facts") or []) if str(f).strip()]
    # Two facts run together with the join left in is the model losing the
    # shape of its own reply. Better dropped than shown as one sentence.
    return [f for f in out if len(f) > 25 and '","' not in f][:3]


# -- the store ---------------------------------------------------------

class _Store:
    """Facts don't change. Once looked up, kept."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        self._loaded = False
        self._busy: set[str] = set()

    def _file(self):
        return data_dir() / "insights.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self._rows = json.loads(self._file().read_text(encoding="utf-8"))
        except Exception:
            self._rows = {}

    def save(self) -> None:
        with self._lock:
            rows = self._rows
            if len(rows) > MAX_ENTRIES:
                keep = sorted(rows.items(), key=lambda kv: kv[1].get("at", 0))
                rows = dict(keep[-MAX_ENTRIES:])
                self._rows = rows
            try:
                write_atomic(self._file(), json.dumps(rows, indent=1))
            except Exception as exc:
                log.debug("couldn't save insights: %s", exc)

    def cached(self, key: str) -> dict | None:
        self.load()
        with self._lock:
            return self._rows.get(key)

    def put(self, key: str, row: dict) -> None:
        self.load()
        with self._lock:
            self._rows[key] = row
        self.save()


store = _Store()


def _key(title: str, artist: str) -> str:
    """One entry per record, whichever upload of it happens to be playing."""
    return f"{_fold((artist or '').lower())}|{_fold(_plain(title).lower())}"


def lookup(title: str, artist: str) -> dict:
    """Everything the internet will say about this record. Cached forever."""
    key = _key(title, artist)
    got = store.cached(key)
    if got is not None:
        return got

    fm = _lastfm(title, artist)
    page = _wiki_page(title, artist)
    story = _story_from(_wiki_text(page)) if page else ""
    if not story:
        story = fm.get("summary") or ""
    sources = []
    if page:
        sources.append({"name": "Wikipedia",
                        "url": "https://en.wikipedia.org/wiki/"
                               + urllib.parse.quote(page.replace(" ", "_"))})
    if fm.get("url"):
        sources.append({"name": "Last.fm", "url": fm["url"]})

    row = {"story": story, "facts": _facts(story, title, artist),
           "listeners": fm.get("listeners", 0),
           "playcount": fm.get("playcount", 0),
           "album": fm.get("album", ""),
           "sources": sources, "at": time.time()}
    store.put(key, row)
    return row


def _heard(taste, track: Track) -> dict:
    """Your own history with this record, and only what's actually recorded.

    The history is deduped by video id, so there is no per-song play count
    to report and inventing one would be worse than leaving it out. What
    there is: whether you've liked it, when it last came round, and how
    many times you've played the band.
    """
    out = {"liked": False, "last": 0.0, "artist_plays": 0}
    if taste is None or track is None:
        return out
    try:
        out["liked"] = bool(taste.is_liked(track.video_id))
    except Exception:
        pass
    try:
        mine = _key(track.title, track.artist)
        for row in taste.recent(400):
            if _key(row.get("title", ""), row.get("artist", "")) == mine:
                out["last"] = float(row.get("at") or 0)
                break
    except Exception as exc:
        log.debug("couldn't read history: %s", exc)
    try:
        want = _fold((track.artist or "").lower())
        for row in taste.top_artists(200):
            if _fold((row.get("artist") or "").lower()) == want:
                out["artist_plays"] = int(row.get("plays") or 0)
                break
    except Exception:
        pass
    return out


def about(track: Track | None, *, taste=None, fetch: bool = True) -> dict:
    """The panel's contents for one record.

    `fetch=False` answers only from what's already on disk, for the caller
    that wants an instant answer and will ask again in a moment.
    """
    if not track or not track.title:
        return {"ready": False, "title": "", "artist": ""}
    from ..core.era import era
    from ..core.tags import tagstore

    key = _key(track.title, track.artist)
    row = store.cached(key)
    if row is None and fetch:
        row = lookup(track.title, track.artist)
    ready = row is not None
    row = row or {}

    tags = []
    try:
        got = tagstore.get(track)
        tags = [t for t, _ in sorted((got or {}).items(),
                                     key=lambda kv: -kv[1])][:6]
    except Exception:
        pass

    return {
        "ready": ready,
        "title": _plain(track.title),
        "artist": track.artist,
        "album": row.get("album") or track.album or "",
        # The artist's start year, not the record's — era looks up bands.
        # Shown as "active since", because putting 1995 next to a song
        # from 2001 is just wrong.
        "since": era.get(track) or 0,
        "tags": tags,
        "story": row.get("story", ""),
        "facts": row.get("facts", []),
        "listeners": row.get("listeners", 0),
        "playcount": row.get("playcount", 0),
        "sources": row.get("sources", []),
        "heard": _heard(taste, track),
    }


def warm(track: Track | None) -> None:
    """Look one up in the background, so the panel is filled before it's opened."""
    if not track or not track.title:
        return
    key = _key(track.title, track.artist)
    if store.cached(key) is not None:
        return
    with store._lock:
        if key in store._busy:
            return
        store._busy.add(key)

    def run():
        try:
            lookup(track.title, track.artist)
        except Exception as exc:
            log.debug("warm failed: %s", exc)
        finally:
            with store._lock:
                store._busy.discard(key)

    threading.Thread(target=run, daemon=True, name="insights").start()
