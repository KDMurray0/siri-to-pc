"""What this server holds about a person, and how to take all of it away.

One place, used by everything that removes or reports on somebody: the person
deleting their own account, the owner removing somebody, and the person asking
what is held. Three routes each doing their own version of "everything" is how
one of them ends up missing a store.

The stores, in the order they are emptied:

  live session      the queue and player state while they are connected (memory)
  profile folder    their settings, playlists and, if they agreed, taste
  usage counters    a row in stats.json keyed by their account id
  list credit       "added by" on shared playlists, by id (and by name for old rows)
  owner's audit     entries made by their account id
  the account       email, chosen name, photo url, consents -- last, so a failure
                    part-way leaves an account that can try again rather than a
                    record of a person whose data is half gone

Not here, because it is not about them: the house totals (every event is counted
once for the place, without a name), and the temporary ban list, which is
addresses and is kept for a day for security.
"""

from __future__ import annotations

import json
import shutil
import time

from ..core import stats
from ..core import audit
from ..core.playlists import playlists
from ..core.profile import profiles
from ..core.session import sessions
from ..logging_setup import get
from ..paths import data_dir
from . import accounts
from . import security as sec

log = get("privacy")

# Files bigger than this are listed, not embedded, in an export.
_EMBED_LIMIT = 512 * 1024


def _home(sub: str):
    return data_dir() / "profiles" / accounts.profile_id(sub)


def erase(sub: str) -> dict:
    """Remove everything held about one account. Returns what went.

    Raises if a durable store can't be emptied, with the account still in place.
    """
    person = accounts.get(sub)
    if not person:
        return {"found": False}
    pid = accounts.profile_id(sub)
    report: dict = {"found": True}

    # In memory: they stop being a listener this instant.
    try:
        report["session"] = bool(sessions.close(pid, "account deleted"))
    except Exception as exc:                    # nothing durable depends on it
        log.debug("closing a session while erasing: %s", exc)
        report["session"] = False

    home = _home(sub)
    if home.exists():
        if not profiles.wipe(pid):
            raise RuntimeError("couldn't remove their profile folder")
        report["profile"] = True
    profiles.forget(pid)

    # Their Siri keys go before the account, so a key can never outlive the
    # person it acts for.
    report["siri_keys"] = sec.siri_forget(pid)
    report["usage"] = stats.erase(pid)
    report["credits"] = playlists.scrub_person(pid, person.get("name", ""))
    report["audit"] = audit.scrub(f"account:{pid}")

    if not accounts.forget(sub):
        raise RuntimeError("couldn't remove the account")
    report["account"] = True
    log.info("erased %s: %s", accounts.tag(sub),
             ", ".join(k for k, v in report.items() if v and k != "found"))
    return report


def forget_taste(sub: str) -> bool:
    """Clear what has been learned from what they played. Keeps likes and blocks.

    Those are things they did on purpose and this button is not for undoing
    them. Returns whether anything was on disk.
    """
    pid = accounts.profile_id(sub)
    path = _home(sub) / "taste" / "play_stats.json"
    had = path.exists()
    if had:
        path.unlink()
    # The engine holds it in memory too; a fresh one reads the empty folder.
    profiles.forget(pid)
    return had


def _read_json(path):
    try:
        return json.loads(path.read_text("utf-8-sig"))
    except Exception:
        return None


def export(sub: str) -> dict:
    """Everything held about this person, as one document they can keep."""
    person = accounts.get(sub)
    if not person:
        return {}
    pid = accounts.profile_id(sub)
    home = _home(sub)

    files: dict = {}
    other: list = []
    if home.exists():
        for path in sorted(home.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(home)).replace("\\", "/")
            size = path.stat().st_size
            if size <= _EMBED_LIMIT and path.suffix.lower() in (".json", ".txt"):
                got = _read_json(path) if path.suffix.lower() == ".json" else None
                if got is None:
                    try:
                        got = path.read_text("utf-8-sig")
                    except Exception:
                        got = None
                files[rel] = got
            else:
                other.append({"file": rel, "bytes": size})

    credits = []
    for list_name in playlists.names():
        held = playlists.credit_ids(list_name)
        for video_id, who in held.items():
            if who == pid:
                credits.append({"list": list_name, "video_id": video_id})

    usage = None if not person.get("tracking") else stats.link(pid)
    return {
        "about": ("Everything this server holds about you. The 'account' section is "
                  "what you gave us and what we recorded when you signed up; "
                  "'profile' is the folder where your settings, playlists and (if "
                  "you agreed) listening history live."),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "account": {
            "google_account_id": person.get("sub"),
            "email": person.get("email"),
            "display_name": person.get("name"),
            "photo_url": person.get("picture"),
            "access": person.get("scope"),
            "created": person.get("created"),
            "last_seen": person.get("last_seen"),
            "consent": {
                "accepted_privacy_notice_version": person.get("terms_version") or None,
                "accepted_privacy_notice_at": person.get("terms_at") or None,
                "listening_history_and_taste": bool(person.get("tracking")),
                "listening_history_and_taste_changed_at": person.get("tracking_at") or None,
            },
        },
        "profile": files,
        "other_files_in_profile": other,
        "shared_playlist_additions": credits,
        "siri_keys": [{"name": k["name"], "created": k["created"] or None,
                       "last_used": k["last_seen"] or None}
                      for k in sec.siri_keys(pid)],
        "usage_counters": usage if usage is not None else "not kept (you have not opted in)",
    }
