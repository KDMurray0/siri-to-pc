"""Two passes at an AI playlist: one model reads, the other does the work.

The big model (gpt-oss-120b) reads what the listener said and writes a brief:
the songs they named, the bands, the sound those share, the mood, what to
leave out. It never reads a whole track list -- a 450-song list is most of a
free-tier minute on its own. The small model (gpt-oss-20b) does the volume:
the songs themselves, and for an edit, which rows of a long list the brief's
rule describes. Groq rations each model separately, so splitting the work
also doubles what a minute can do.

Nothing here touches the catalogue; the builder checks every pick.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field

from ..logging_setup import get
from ..models import Track, _fold
from ..resolve import llm

log = get("curator")

PLANNER = "openai/gpt-oss-120b"
WORKER = "openai/gpt-oss-20b"
CHUNK = 120                 # rows of a list the worker judges in one ask
_ENERGY = ("low", "medium", "high", "any")


class Busy(Exception):
    """Both models are resting; try again in `wait` seconds."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"Groq is resting for {wait:.0f}s")
        self.wait = max(5.0, float(wait))


@dataclass
class Brief:
    """What a playlist should be, as the planner read it."""
    title: str = ""
    literal: list[tuple[str, str]] = field(default_factory=list)   # (artist, title), as named
    artists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    mood: str = ""
    energy: str = "any"
    era: str = ""
    avoid: list[str] = field(default_factory=list)
    strict: bool = False
    note: str = ""
    songs: int = 0          # the length it asks for, or suits; 0 when nothing says
    minutes: int = 0

    def describe(self) -> str:
        """The brief, as the worker reads it."""
        bits = [self.note.strip()]
        if self.genres:
            bits.append("Sound: " + ", ".join(self.genres) + ".")
        if self.mood:
            bits.append("Mood: " + self.mood + ".")
        if self.energy and self.energy != "any":
            bits.append(f"Energy: {self.energy}.")
        if self.era:
            bits.append("Era: " + self.era + ".")
        if self.artists:
            bits.append(("Only these artists: " if self.strict else "Feature: ")
                        + ", ".join(self.artists[:40]) + ".")
        if self.avoid:
            bits.append("Avoid: " + ", ".join(self.avoid) + ".")
        return " ".join(b for b in bits if b)


@dataclass
class Change:
    """An edit to a list that already exists."""
    drop_artists: list[str] = field(default_factory=list)   # every song by these goes
    drop_rule: str = ""                                     # judged song by song
    add: int = 0
    brief: Brief = field(default_factory=Brief)
    summary: str = ""


# -- asking ------------------------------------------------------------------

def _ask(system: str, user: str, *, prefer: str, timeout: float = 40.0,
         wait: float = 0.0) -> dict | None:
    """A JSON answer from the preferred model, or the other one while that
    rests. Busy when both are resting longer than `wait` allows."""
    other = WORKER if prefer == PLANNER else PLANNER
    deadline = time.monotonic() + wait
    while True:
        for model in (prefer, other):
            if llm.resting(model) <= 0:
                got = llm.ask_json(system, user, timeout=timeout, model=model)
                if got is not None or llm.resting(model) <= 0:
                    return got
        rest = min(llm.resting(prefer), llm.resting(other))
        if time.monotonic() + rest > deadline:
            raise Busy(rest)
        time.sleep(min(rest, 30.0) + 0.5)


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _fold((text or "").lower())))


def named_in(title: str, said: str) -> bool:
    """Is this title actually in what they said? A song the planner made up
    must never be forced into somebody's list."""
    t = _words(title)
    return bool(t) and f" {t} " in f" {_words(said)} "


def _strings(value, limit: int, size: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(v).strip()[:size] for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return list(dict.fromkeys(out))[:limit]


def _energy(value) -> str:
    v = str(value or "any").strip().lower()
    return v if v in _ENERGY else "any"


def _truthy(value) -> bool:
    # gpt-oss sometimes answers "false" as a string.
    return value is True or str(value).strip().lower() == "true"


# -- planning a new list -------------------------------------------------------

_PLAN = (
    "You plan playlists for a music player. Reply with JSON only. Read the "
    "listener's request and write a brief for the curator who picks the songs. "
    "literal: each specific song the request names, as {artist,title}; never add "
    "a song it doesn't name. artists: the artists it names. genres: up to five "
    "subgenres the request and its artists share, most specific first. mood: a "
    "few words. energy: low|medium|high|any. era: a decade or empty. avoid: what "
    "wouldn't fit (ballads, covers, live versions, an artist's off-style songs). "
    "strict: true only if they want nothing but the named artists. title: a short "
    "evocative name for the list, never the word playlist. note: two or three "
    "sentences telling the curator exactly what to pick. songs and minutes: the "
    "length the request asks for, or that suits what it's for (a run about 45 "
    "minutes, a party about 3 hours, a quick mix about 15 songs); both 0 when "
    "nothing suggests a length.")
_PLAN_FORMAT = ('{"title":"","literal":[{"artist":"","title":""}],"artists":[],'
                '"genres":[],"mood":"","energy":"any","era":"","avoid":[],'
                '"strict":false,"note":"","songs":0,"minutes":0}')


def plan(what: str, *, hint: str = "") -> Brief:
    """Read a request into a brief. An empty brief when Groq says nothing."""
    user = what.strip()[:4000] + (("\n" + hint.strip()) if hint.strip() else "")
    got = _ask(_PLAN + " Format: " + _PLAN_FORMAT, user, prefer=PLANNER) or {}
    return brief_from(got, what)


def brief_from(got: dict, what: str) -> Brief:
    """A brief from the planner's JSON, keeping only what can be trusted."""
    literal = []
    for row in got.get("literal") or []:
        if not isinstance(row, dict):
            continue
        artist = str(row.get("artist") or "").strip()[:120]
        title = str(row.get("title") or "").strip()[:160]
        if title and named_in(title, what):
            literal.append((artist, title))
    return Brief(
        title=str(got.get("title") or "").strip().strip('"')[:60],
        literal=literal[:60],
        artists=_strings(got.get("artists"), 60, 120),
        genres=_strings(got.get("genres"), 5),
        mood=str(got.get("mood") or "").strip()[:80],
        energy=_energy(got.get("energy")),
        era=str(got.get("era") or "").strip()[:20],
        avoid=_strings(got.get("avoid"), 8),
        strict=_truthy(got.get("strict")),
        note=str(got.get("note") or "").strip()[:600],
        songs=_whole(got.get("songs"), 1000),
        minutes=_whole(got.get("minutes"), 24 * 60),
    )


def _whole(value, top: int) -> int:
    try:
        return max(0, min(top, int(float(value or 0))))
    except (TypeError, ValueError, OverflowError):
        return 0


# -- the grunt work ------------------------------------------------------------

_PICK = (
    "You curate playlists. Reply with JSON only. Follow the brief. Pick songs for "
    "this list: well-known songs that suit its sound, mood and energy, each chosen "
    "because it fits rather than because it is the artist's biggest hit. Real, "
    "released studio recordings by the original artist. Weight the artists by how well they fit: "
    "several songs from the ones at the heart of it, one from others, none from "
    "ones that only half fit. Never ration artists evenly or cycle through them in "
    "turn. Order it as a DJ would: let the energy build and breathe, never the same "
    "artist twice in a row. No song twice.")


def songs(brief: Brief, n: int, *, have: list[tuple[str, str]] | None = None,
          carry: bool = False) -> list[tuple[str, str]]:
    """(artist, title) pairs that fit the brief, in playing order. `carry`: the
    list so far is `have`, and these are the next stretch of it."""
    have = have or []
    taken = (" Already in the list, don't repeat: "
             + "; ".join(f"{t} by {a}" for a, t in have[-40:]) + ".") if have else ""
    if carry and have:
        taken += " Carry on from the last of those, keeping the flow."
    user = f"{brief.describe()}{taken} {min(90, max(1, n))} songs."
    got = _ask(_PICK + ' Format: {"songs":[{"artist":"","title":""}]}', user,
               prefer=WORKER) or {}
    out = []
    for row in got.get("songs") or []:
        if isinstance(row, dict) and row.get("artist") and row.get("title"):
            out.append((str(row["artist"]).strip()[:120], str(row["title"]).strip()[:160]))
    return out


def artists(brief: Brief, n: int) -> list[str]:
    """Artists that fit the brief, best known first: for lists too long to name
    every song."""
    got = _ask(_PICK + ' Name artists, not songs. Format: {"artists":[""]}',
               f"{brief.describe()} {min(120, max(4, n))} artists.", prefer=WORKER) or {}
    return _strings(got.get("artists"), 120, 120)


# -- changing a list that exists -----------------------------------------------

_CHANGE = (
    "You edit playlists. Reply with JSON only. You get what the listener wants "
    "changed and a summary of the list (artist: songs). drop_artists: artists "
    "whose songs should all go -- the ones named, plus artists like them if they "
    "asked for that. drop_rule: one sentence describing any other songs to take "
    "out, to be judged song by song (e.g. 'slow, mellow or acoustic songs'), or "
    "empty. add: how many new songs to add -- enough to keep the list about its "
    "length unless they said otherwise, 0 if they only asked to remove. genres, "
    "mood, energy, avoid, note: the brief for the new songs. summary: one short "
    "sentence telling the listener what will change.")
_CHANGE_FORMAT = ('{"drop_artists":[],"drop_rule":"","add":0,"genres":[],"mood":"",'
                  '"energy":"any","avoid":[],"note":"","summary":""}')


def outline(tracks: list[Track], limit: int = 120) -> str:
    """A list as the planner reads it: who's in it and how much."""
    names = Counter((t.artist or "").split(",")[0].strip() for t in tracks if t.artist)
    rows = ", ".join(f"{a}: {n}" for a, n in names.most_common(limit))
    more = len(names) - limit
    return f"{len(tracks)} songs. {rows}" + (f", and {more} more artists" if more > 0 else "")


def change(what: str, tracks: list[Track]) -> Change:
    """Read an edit ("more energetic", "no more Vampire Weekend or anything
    like them") into what goes and what comes in."""
    user = f"Change: {what.strip()[:1000]}\nThe list: {outline(tracks)}"
    got = _ask(_CHANGE + " Format: " + _CHANGE_FORMAT, user, prefer=PLANNER) or {}
    try:
        add = max(0, min(500, int(got.get("add") or 0)))
    except (TypeError, ValueError):
        add = 0
    return Change(
        drop_artists=_strings(got.get("drop_artists"), 40, 120),
        drop_rule=str(got.get("drop_rule") or "").strip()[:300],
        add=add,
        brief=Brief(genres=_strings(got.get("genres"), 5), mood=str(got.get("mood") or "")[:80],
                    energy=_energy(got.get("energy")), avoid=_strings(got.get("avoid"), 8),
                    note=str(got.get("note") or "").strip()[:600]),
        summary=str(got.get("summary") or "").strip()[:200],
    )


_JUDGE = ("You check songs against a rule. Reply with JSON only: "
          '{"match":[numbers]} -- the numbers of the songs the rule describes. '
          "Only clear cases.")


def matching(rule: str, tracks: list[Track], *, wait: float = 120.0) -> set[int]:
    """Indexes of the songs a rule describes, judged by the worker a chunk at a
    time, waiting out the rate limit between chunks if it has to."""
    found: set[int] = set()
    if not rule.strip():
        return found
    for start in range(0, len(tracks), CHUNK):
        rows = tracks[start:start + CHUNK]
        lines = "\n".join(f"{i}. {t.title} - {(t.artist or '').split(',')[0]}"
                          for i, t in enumerate(rows))
        got = _ask(_JUDGE, f"Rule: {rule}\n{lines}", prefer=WORKER, wait=wait) or {}
        for i in got.get("match") or []:
            if isinstance(i, int) and 0 <= i < len(rows):
                found.add(start + i)
    return found
