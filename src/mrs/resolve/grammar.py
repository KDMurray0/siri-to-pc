"""Local phrase parsing. No network, instant."""

from __future__ import annotations

import re

from ..models import Plan
from .conjunction import looks_like_genre, split_seeds

_WAKE = re.compile(
    r"^\s*(?:hey\s+)?(?:siri|computer|assistant)[,\s]+", re.I)
_PLAY_SYNONYM = re.compile(
    r"^\s*(?:can you\s+|please\s+)?(?:put on|throw on|gimme|give me|"
    r"i wanna hear|i want to hear|let'?s hear|start playing|start)\s+", re.I)
_PLAY = re.compile(r"^\s*play\s+", re.I)

# Exact transport phrases — high confidence, so they short-circuit the LLM.
_TRANSPORT = {
    "pause": "pause", "pause music": "pause", "pause the music": "pause",
    "stop": "pause", "stop music": "pause", "stop the music": "pause",
    "resume": "resume", "unpause": "resume", "continue": "resume",
    "play": "resume", "keep playing": "resume",
    "next": "next", "skip": "next", "skip this": "next", "next song": "next",
    "next track": "next", "skip song": "next", "skip track": "next",
    "previous": "previous", "back": "previous", "go back": "previous",
    "previous song": "previous", "last song": "previous", "prev": "previous",
    "shuffle": "shuffle", "shuffle it": "shuffle",
    "repeat": "repeat", "loop": "repeat",
    "like": "like", "like this": "like", "love this": "like",
    "more like this": "more_like_this", "similar": "more_like_this",
    "mute": "mute", "unmute": "unmute",
    "save": "save", "save this": "save", "save this song": "save",
    "download": "save", "download this": "save", "download this song": "save",
    "keep this": "save",
}

# "add this to my gym playlist" / "add to gym"
_ADD_TO = re.compile(
    r"add (?:this|it|the song|this song)?\s*to (?:my )?(?P<name>.+?)"
    r"(?:\s+playlist|\s+list)?\s*$", re.I)

_VOL_SET = re.compile(r"\b(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\b", re.I)
_VOL_PCT = re.compile(r"\b(\d{1,3})\s*(?:%|percent)\b", re.I)
_VOL_UP = re.compile(r"\b(?:turn it up|volume up|louder|turn up)\b", re.I)
_VOL_DOWN = re.compile(r"\b(?:turn it down|volume down|quieter|softer|turn down)\b", re.I)

_BY = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", re.I)
_ALBUM = re.compile(
    r"\b(?:the\s+)?(?P<name>.+?)\s+(?:album|record|lp)\b|"
    r"\balbum\s+(?P<name2>.+)$", re.I)
_SONGS_BY = re.compile(r"^(?:songs?|music|tracks?)\s+by\s+(?P<artist>.+)$", re.I)
_SHUFFLE = re.compile(r"\bshuffle\b", re.I)
_VARIANT = re.compile(
    r"\b(remix|live|acoustic|cover|sped\s*up|slowed|instrumental|8d|nightcore)\b", re.I)

_GENRE_HINT = re.compile(
    r"^(?:some\s+|a bit of\s+)?(?P<g>[a-z0-9\s&'-]+?)\s*"
    r"(?:music|songs?|vibes?|tunes?|playlist|mix)$", re.I)
_DECADE = re.compile(r"^\s*(\d0s|\d{4}s)\s*$", re.I)
# "play some drum and bass", "play a bit of shoegaze". Without a trailing
# noun to go on this needs the genre itself to be recognisable, or "play
# some nights" becomes a request for the nights genre.
_SOME = re.compile(r"^(?:some|any|a bit of|a little)\s+(?P<g>.{2,40})$", re.I)


def clean(text: str) -> str:
    t = (text or "").strip()
    t = _WAKE.sub("", t)
    t = _PLAY_SYNONYM.sub("play ", t)
    return t.strip().strip("?.!,")


def transport(text: str) -> str | None:
    """An exact control phrase, or None."""
    key = re.sub(r"[^a-z ]", "", clean(text).lower()).strip()
    return _TRANSPORT.get(key)


def volume_intent(text: str) -> tuple[str, int] | None:
    t = clean(text)
    m = _VOL_SET.search(t) or _VOL_PCT.search(t)
    if m:
        return ("volume", max(0, min(150, int(m.group(1)))))
    if _VOL_UP.search(t):
        return ("volume_delta", 10)
    if _VOL_DOWN.search(t):
        return ("volume_delta", -10)
    return None


def playlist_add(text: str) -> str | None:
    """Playlist name from "add this to my X"."""
    m = _ADD_TO.search(clean(text))
    return m.group("name").strip() if m else None


# "make a 30 minute jazz and blues playlist", "create a grunge playlist
# that's 15 minutes long", "build me an hour of Bon Jovi".
#
# The length can come before the subject or after it, and the word
# "playlist" can sit on either side of the subject too, which is why this is
# a handful of small patterns rather than one clever one.
_MAKE = re.compile(
    r"^(?:make|create|build|put together|generate)\s+(?:me\s+)?(?:an|a|the)\b\s*", re.I)
_LEN = re.compile(
    r"\b(?:(?P<h>\d+)\s*(?:h|hr|hrs|hour|hours))"
    r"|\b(?P<m>\d+)\s*(?:m|min|mins|minute|minutes)\b", re.I)
# Said rather than counted. Longest first so "half an hour" wins over "an
# hour". Matched against the whole sentence, because "build me an hour of
# shoegaze" has already had its "an" taken off by _MAKE by then.
_SPOKEN_LENGTHS = [("quarter of an hour", 15), ("half an hour", 30),
                   ("three hour", 180), ("two hour", 120),
                   ("an hour", 60), ("one hour", 60), ("a hour", 60)]
_HOUR_LEFT = re.compile(r"\b(?:half\s+(?:an\s+)?|quarter\s+of\s+(?:an\s+)?)?"
                        r"(?:an\s+|a\s+|one\s+|two\s+|three\s+)?hours?\b", re.I)
_PLAYLIST_WORD = re.compile(
    r"\b(?:playlist|mix|set|selection)\b", re.I)
_LEN_TAIL = re.compile(
    r"\b(?:that(?:'s| is)?|which(?:'s| is)?|lasting|of|for|about|around)?\s*"
    r"\d+\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\s*(?:long)?\b", re.I)


def playlist_make(text: str):
    """A request to build a playlist, as (subject, minutes) — or None.

    Minutes is None when no length was asked for, which the caller reads as
    "a sensible one". The subject is handed on untouched so the ordinary
    resolver can decide whether it is an artist, a genre, several of either,
    or a song to build around — this only works out that a playlist is what
    was wanted, and how long.
    """
    said = clean(text)
    if not _MAKE.match(said) or not _PLAYLIST_WORD.search(said):
        return None
    body = _MAKE.sub("", said, count=1)

    minutes = None
    m = _LEN.search(body)
    if m:
        minutes = int(m.group("h")) * 60 if m.group("h") else int(m.group("m"))
    else:
        low = said.lower()
        for phrase, mins in _SPOKEN_LENGTHS:
            if phrase in low:
                minutes = mins
                body = _HOUR_LEFT.sub(" ", body)
                break

    # Take out the length and the word "playlist", and whatever is left is
    # what it should be a playlist of.
    body = _LEN_TAIL.sub(" ", body)
    body = _PLAYLIST_WORD.sub(" ", body)
    body = re.sub(r"\b(?:with|of|about|featuring|full of|made (?:up )?of)\b",
                  " ", body, flags=re.I)
    body = re.sub(r"\b(?:long|please|thanks)\b", " ", body, flags=re.I)
    body = re.sub(r"\s{2,}", " ", body).strip(" ,.-")
    # "a playlist like Bohemian Rhapsody" — the subject is that record, and
    # the resolver builds around it exactly as it would if you'd asked to
    # play it. The word itself would only confuse the search.
    body = re.sub(r"^(?:like|similar to|based on|around|inspired by)\s+", "",
                  body, flags=re.I).strip()
    return (body, minutes) if body else None


_FILLER = re.compile(r"^(?:some|any|a bit of|a little|a few)\s+", re.I)


def _maybe_split(text: str) -> list[str]:
    """Two things, or one thing with "and" in the middle? A proposal only —
    the resolver checks whether the whole phrase is somebody first.

    The filler comes off here and not off plan.query, because "Some Nights"
    is a song and "Some Might Say" is a record — stripping it everywhere
    would break both. This path already needs a conjunction to reach.
    """
    parts = split_seeds(text)
    if len(parts) < 2:
        return []
    parts[0] = _FILLER.sub("", parts[0]).strip() or parts[0]
    return parts


def parse(text: str) -> Plan:
    """Best-effort structural read of a request."""
    raw = clean(text)
    plan = Plan(via="grammar", spoken=raw)

    cmd = transport(raw)
    if cmd:
        return Plan(kind="command", command=cmd, via="grammar", spoken=raw)
    vol = volume_intent(raw)
    if vol:
        return Plan(kind="command", command=vol[0], query=str(vol[1]),
                    via="grammar", spoken=raw)

    body = _PLAY.sub("", raw).strip()
    if _SHUFFLE.search(body):
        plan.shuffle = True
        body = _SHUFFLE.sub("", body).strip()
    plan.variant = bool(_VARIANT.search(body))

    m = _SONGS_BY.match(body)
    if m:
        plan.kind = "artist"
        plan.query = plan.artist = m.group("artist").strip()
        plan.seeds = _maybe_split(plan.query)
        return plan

    m = _ALBUM.search(body)
    if m and (m.group("name") or m.group("name2")):
        plan.kind = "album"
        plan.query = (m.group("name") or m.group("name2")).strip()
        m2 = _BY.match(plan.query)
        if m2:
            plan.query = m2.group("title").strip()
            plan.artist = m2.group("artist").strip()
        return plan

    m = _BY.match(body)
    if m:
        plan.kind = "song"
        plan.query = m.group("title").strip()
        plan.artist = m.group("artist").strip()
        return plan

    if _DECADE.match(body):
        plan.kind = "genre"
        plan.query = body.strip()
        return plan

    m = _GENRE_HINT.match(body)
    if m:
        plan.kind = "genre"
        plan.query = m.group("g").strip()
        plan.seeds = _maybe_split(plan.query)
        return plan

    m = _SOME.match(body)
    if m:
        asked = m.group("g").strip()
        parts = split_seeds(asked)
        if all(looks_like_genre(p) for p in parts):
            plan.kind = "genre"
            plan.query = asked
            plan.seeds = parts if len(parts) > 1 else []
            return plan

    # No structure to go on: let the resolver decide song vs artist.
    plan.kind = "auto"
    plan.query = body
    plan.seeds = _maybe_split(body)
    return plan
