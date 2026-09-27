"""The owner's Google account and this computer are one person.

Signed in with Google as the owner, you're on the master profile -- the
house's likes, playlists and listening -- not a second, empty one. Anything
that built up under a separate account first (signed in before the owner's
address was written down, or under another spelling of it) is folded into the
house, and the old folder is kept aside rather than left orphaned.
"""

from __future__ import annotations

import time

from ..logging_setup import get
from ..paths import data_dir

log = get("ownersync")


def fold(pid: str) -> dict:
    """Move one account's own profile into the house's. Nothing there, nothing done."""
    from .profile import _safe, profiles
    home = data_dir() / "profiles" / _safe(pid)
    if not home.is_dir():
        return {"folded": False}
    from .playlists import Playlists, playlists
    from .taste import TasteEngine, taste
    report: dict = {"folded": True}
    try:
        report.update(taste.absorb(TasteEngine(root=home / "taste")))
    except Exception as exc:
        log.warning("couldn't fold the listening in: %s", exc)
        return {"folded": False, "error": str(exc)}
    moved = 0
    try:
        theirs = Playlists(home=home, session=pid)
        have = set(playlists.names())
        for name in theirs.names():
            tracks = theirs.tracks(name)
            if not tracks:
                continue
            for t in tracks:
                # A download inside the folder about to be put aside; it'll
                # fetch again by id.
                if str(getattr(t, "path", "") or "").startswith(str(home)):
                    t.path = ""
            dest = name if name not in have else f"{name} (Google)"
            playlists.add_many(dest, tracks)
            have.add(dest)
            moved += 1
    except Exception as exc:
        log.warning("couldn't fold the playlists in: %s", exc)
    report["playlists"] = moved
    try:
        from .session import sessions
        sessions.close(pid, "now the owner")
    except Exception:
        pass
    profiles.forget(pid)
    aside = home.with_name(home.name + f".folded-{int(time.time())}")
    try:
        home.rename(aside)
    except OSError as exc:
        log.warning("folded, but couldn't put the old folder aside: %s", exc)
    log.info("owner's Google account folded into the house (%s)",
             ", ".join(f"{k} {v}" for k, v in report.items() if k != "folded"))
    return report


def link_owner() -> list[dict]:
    """After the owner's address is set: whoever already signed in with it is
    the owner now, on the house profile, with what they had brought across."""
    from ..web import accounts
    done = []
    for row in accounts.promote_owner_matches():
        done.append({"sub": row["sub"], "email": row.get("email", ""),
                     **fold(accounts.profile_id(row["sub"]))})
    return done
