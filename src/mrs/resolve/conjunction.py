"""Reading "and" in a request, which is harder than it looks.

    play songs by nirvana and foo fighters   two artists
    play some thrash and black metal         two genres, one of them missing
                                             the word "metal" entirely
    play simon and garfunkel                 one act
    play some drum and bass                  one genre

Nobody says "thrash metal and black metal" out loud, so the head noun has to
be carried backwards. And plenty of acts have "and" in the middle of their
name, so a split is a *proposal* — the resolver checks whether the whole
phrase is somebody before believing it.
"""

from __future__ import annotations

import re

# The word a genre hangs off. "thrash and black metal" is two of these with
# the noun said once at the end; "jazz and funk" is two that each brought
# their own, so nothing is carried back.
_HEADS = {
    "metal", "rock", "punk", "jazz", "house", "techno", "pop", "hop",
    "soul", "funk", "blues", "country", "folk", "reggae", "ska", "wave",
    "core", "gaze", "grunge", "rap", "disco", "garage", "ambient", "trance",
    "dub", "classical", "electronica", "emo", "r&b", "rnb", "indie", "step",
}

# "and" inside a name rather than between two things. The list is short on
# purpose: it's the common ones, and the resolver's own check catches the
# rest by asking whether the whole phrase is an artist.
_GLUED = (
    # genres
    "drum and bass", "rock and roll", "rhythm and blues", "drum n bass",
    "rock n roll", "rhythm n blues",
    # acts that trip people up
    "simon and garfunkel", "hall and oates", "sam and dave", "sonny and cher",
    "ike and tina turner", "mumford and sons", "earth wind and fire",
    "peaches and herb", "captain and tennille", "ashford and simpson",
    "brooks and dunn", "hootie and the blowfish", "kool and the gang",
    "iron and wine", "belle and sebastian", "angus and julia stone",
    "she and him", "matt and kim", "chas and dave", "salt n pepa",
)

# "Florence and the Machine", "Nick Cave and the Bad Seeds", "Kool and the
# Gang" — one act. But "the Beatles and the Rolling Stones" is two, and the
# tell is that the first half brought an article of its own.
_AND_THE = re.compile(r"^(?!the\b).*?\band\s+the\b", re.I | re.S)

# Split points. Deliberately not "&": it lives inside names far more often
# than between them (Earth, Wind & Fire; Hall & Oates; Simon & Garfunkel).
_SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+|plus\s+)?|\s+and\s+|\s+plus\s+)\s*", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def glued(text: str) -> bool:
    """Is the "and" part of a name, rather than joining two things?"""
    low = _norm(text)
    if not low:
        return False
    # An ampersand is how an act is billed, not how people list things:
    # Earth, Wind & Fire; Crosby, Stills & Nash; Blood, Sweat & Tears. It
    # stops the commas being read as separators too. The cost is that
    # "thrash & black metal" won't split — worth it, because splitting a
    # band's name plays two wrong things while failing to split plays one
    # right one.
    if "&" in low:
        return True
    if _AND_THE.search(low):
        return True
    return any(g in low for g in _GLUED)


def _carry_head(parts: list[str]) -> list[str]:
    """Give the earlier parts the noun the last one is holding.

    "thrash and black metal" is thrash metal and black metal. "jazz and
    funk" is not jazz funk and funk — both of those brought their own noun,
    so nothing moves.
    """
    if len(parts) < 2:
        return parts
    tail = parts[-1].split()
    if len(tail) < 2:
        return parts
    head = tail[-1].lower()
    if head not in _HEADS:
        return parts
    out = []
    for p in parts[:-1]:
        words = p.split()
        # Already has a noun of its own, or already ends in this one.
        if not words or words[-1].lower() in _HEADS:
            out.append(p)
        else:
            out.append(f"{p} {head}")
    return out + [parts[-1]]


def split_seeds(text: str, *, carry: bool = True) -> list[str]:
    """Propose the things a request names. One item means "don't split".

    Only a proposal. The obvious shapes are caught here, but any act whose
    name simply has "and" in the middle and isn't on the short list above
    will be split wrongly — so the caller is expected to check whether the
    whole phrase resolves to somebody before taking the split.
    """
    raw = (text or "").strip()
    if not raw or glued(raw):
        return [raw] if raw else []
    parts = [p.strip(" ,") for p in _SPLIT.split(raw)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [raw]
    # A split that produces a bare article or a single letter is a bad read.
    if any(len(p) < 2 or p.lower() in ("the", "a", "an") for p in parts):
        return [raw]
    if carry:
        parts = _carry_head(parts)
    seen, out = set(), []
    for p in parts:
        k = _norm(p)
        if k and k not in seen:
            seen.add(k)
            out.append(p)
    return out or [raw]
