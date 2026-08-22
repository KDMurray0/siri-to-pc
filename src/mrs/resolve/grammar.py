"""Local phrase parsing. No network, instant."""

from __future__ import annotations

import re

from ..models import Plan

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
}

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
        return plan

    # No structure to go on: let the resolver decide song vs artist.
    plan.kind = "auto"
    plan.query = body
    return plan
