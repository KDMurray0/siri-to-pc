"""Most likely to be wanted, first.

Asking for a band used to play their catalogue in album order: five hits, then
side one of the debut. What somebody asking for "Danzig" most likely wants is
Mother, and what *you* most likely want is whichever of their songs you've
played to the end before -- not the one you always skip.

So a list arrives in popularity order (YouTube Music's own top-songs ranking)
and is re-ranked by that and by this listener's own history, with a little
variety so the same request doesn't open the same way every time. The variety
scales with popularity: the hits trade places among themselves, the deep cuts
stay deep.
"""

from __future__ import annotations

import random
import re

from ..models import Track, _fold, _strip_article


def likely(tracks: list[Track], taste=None, *, variety: float = 0.3,
           seed=None) -> list[Track]:
    """Re-rank `tracks` (popularity order in) by how likely each is wanted."""
    rng = random.Random(seed)
    scored = []
    for i, t in enumerate(tracks):
        if taste is not None and taste.is_blocked(t):
            continue
        pop = 1.0 / (1.0 + i / 12.0)            # rank 1 = 1.0, rank 25 ~ 0.3
        mine = 0.0
        if taste is not None:
            mine = 0.45 * taste.score(t)        # -1..1: plays, skips, likes
            if taste.is_liked(t.video_id):
                mine += 0.5
        scored.append((pop + mine + rng.uniform(0.0, variety) * pop, i, t))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [t for _, _, t in scored]


def _name(text: str) -> str:
    return " ".join(_strip_article(_fold((text or "").lower())).replace("&", " and ")
                    .translate(str.maketrans({c: " " for c in ".,!?'\"()[]-/:"})).split())


def artist_matches(track: Track, wanted: str) -> bool:
    """Is this recording by the artist they named? Loose on spelling, strict on who.

    Exact names only -- ignoring case, spacing and punctuation -- and any one
    of several credited: "Mark Ronson, Amy Winehouse" is by Amy Winehouse. Never
    a substring: that is how "The Black Bon Jovi" passes for Bon Jovi.
    """
    squash = lambda x: _name(x).replace(" ", "")
    want = squash(wanted)
    if not want:
        return True
    names = [track.artist or "", track.primary_artist()]
    names += re.split(r",|&|\bfeat\.?\s|\bft\.?\s|\bwith\s| x ", track.artist or "", flags=re.I)
    return any(squash(n) == want for n in names if n.strip())


def title_matches(track: Track, wanted: str) -> bool:
    want, have = _name(wanted), _name(track.title)
    return bool(want) and (want == have or have.startswith(want) or want in have)
