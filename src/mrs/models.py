"""Shared data shapes."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

_BRACKETS = re.compile(r"\(.*?\)|\[.*?\]")
# No "with": it truncates real titles ("Bullet With Butterfly Wings" -> "bullet").
# A parenthesised "(with X)" is already removed by _BRACKETS.
_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*", re.I)
# Apostrophes are deleted rather than collapsed to a space: YouTube lists both
# "Don't Stop Me Now" and "Dont Stop Me Now", and turning them into spaces gave
# "don t stop me now" vs "dont stop me now" — two keys for one song.
_QUOTES = re.compile(r"['\u2018\u2019\u02bc`]")
_NONWORD = re.compile(r"[^a-z0-9]+")

# Words that are never part of a real title.
_JUNK = (r"karaoke|nightcore|8d|sped\s*up|slowed|bootleg|mashup|reverb|"
         r"lyric video|visuali[sz]er|snippet|preview|teaser")
# Words that often are: Live Forever, Live and Let Die, Cover Me, Remix Culture.
# These only mean "not the real release" when they sit where a descriptor sits.
_TAGS = r"remix|live|acoustic|cover|instrumental|edit|version|mix"

_DERIV_JUNK = re.compile(r"\b(" + _JUNK + r")\b", re.I)
# inside brackets: "Song (Live)", "Song [Acoustic Version]"
_DERIV_BRACKET = re.compile(r"[(\[][^)\]]*\b(" + _TAGS + r")\b", re.I)
# after a dash: "Song - Live", "Song — Acoustic"
_DERIV_DASH = re.compile(r"[-\u2013\u2014]\s*[^-\u2013\u2014]*\b(" + _TAGS + r")\b", re.I)
# "Live at Wembley", "Acoustic from the studio"
_DERIV_WHERE = re.compile(r"\b(live|acoustic|unplugged)\s+(at|in|from|on)\b", re.I)


def _strip_article(name: str) -> str:
    """"The Smashing Pumpkins" and "Smashing Pumpkins" are the same band —
    YouTube Music returns both spellings and they were dodging the dedupe."""
    return name[4:] if name.startswith("the ") else name


def norm_title(title: str, artist: str = "") -> str:
    """Collapse to "artist|title" for name-based dedupe."""
    t = (title or "").lower()
    t = _BRACKETS.sub(" ", t)
    t = _FEAT.sub(" ", t)
    t = _QUOTES.sub("", t)
    t = _NONWORD.sub(" ", t).strip()
    t = re.sub(r"\s+(remaster(ed)?|mix|version|edit)(\s+\d{4})?$", "", t).strip()
    a = _QUOTES.sub("", (artist or "").split(",")[0].lower())
    a = _NONWORD.sub(" ", a).strip()
    return f"{_strip_article(a)}|{t}" if t else ""


def is_derivative(title: str) -> bool:
    """A remix/live/cover rather than the release you asked for.

    "Live Forever" and "Live and Let Die" are real songs, so the softer words
    only count when they sit where a descriptor sits — in brackets, after a
    dash, or followed by "at"/"from".
    """
    t = title or ""
    return bool(_DERIV_JUNK.search(t) or _DERIV_BRACKET.search(t)
                or _DERIV_DASH.search(t) or _DERIV_WHERE.search(t))


@dataclass
class Track:
    """A song we might play. `path` is set once it's on disk."""
    video_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    art: str = ""
    duration: int = 0
    url: str = ""            # non-YouTube source (SoundCloud, local file)
    source: str = "youtube"  # youtube | soundcloud | local
    path: str = ""           # local file once downloaded
    origin: str = "request"  # request | radio | playlist | library
    reason: str = ""         # why the radio picked it, shown in the queue

    def key(self) -> str:
        return norm_title(self.title, self.artist)

    def primary_artist(self) -> str:
        # article-stripped so cohesion and run limits treat "The Smashing
        # Pumpkins" and "Smashing Pumpkins" as one band
        return _strip_article((self.artist or "").split(",")[0].strip().lower())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Track":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Candidate:
    """A queue idea that hasn't been downloaded yet."""
    track: Track
    score: float = 0.0
    reason: str = ""          # why it was picked, for debugging
    attempts: int = 0
    next_try: float = 0.0     # monotonic; backoff after a failure
    dead: bool = False

    def can_try(self, now: float) -> bool:
        return not self.dead and now >= self.next_try


@dataclass
class Plan:
    """What the parser decided a request means."""
    kind: str = "song"        # song | album | artist | genre | command | playlist
    query: str = ""
    artist: str = ""
    command: str = ""
    variant: bool = False     # user explicitly wants a remix/live version
    shuffle: bool | None = None
    mode: str = "play"        # play | next | queue
    spoken: str = ""
    source: str = "youtube"
    via: str = "grammar"      # grammar | llm — which parser decided


@dataclass
class Activity:
    """What the server is doing right now, for the UI."""
    stage: str = "idle"       # idle | finding | downloading | loading | playing
    detail: str = ""
    progress: float = 0.0     # 0..1 where known
    started: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
