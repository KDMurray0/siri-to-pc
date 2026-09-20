"""People, by who they are rather than by which link they happen to hold.

A link is a credential you can forward, and it can't tell two people apart:
a phone link handed to one guest and copied to three more is four listeners
sharing one history. An account is a person. They sign in with Google, their
taste and their playlists follow them to whatever device they pick up, and
taking their access away doesn't mean revoking a link somebody else is also
using.

Links still work and still matter — one is how a person gets in the door the
first time. What the link no longer has to be is the identity.
"""

from __future__ import annotations

import json
import re
import threading
import time

from ..config import config
from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("accounts")

# What an account may do. Deliberately the same words a link's scope uses, so
# everything downstream that already understands a scope needs no new case.
SCOPES = ("owner", "full", "phone", "blocked")
FILE = "accounts.json"
_lock = threading.RLock()


def _path():
    return data_dir() / FILE


def _read() -> dict:
    try:
        raw = json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    people = raw.get("people") if isinstance(raw, dict) else None
    if not isinstance(people, dict):
        return {}
    out = {}
    for sub, row in people.items():
        if isinstance(sub, str) and isinstance(row, dict) and _ok_sub(sub):
            out[sub] = {
                "sub": sub,
                "email": str(row.get("email", ""))[:120],
                "name": str(row.get("name", ""))[:80],
                "scope": row.get("scope") if row.get("scope") in SCOPES else "phone",
                "created": int(row.get("created") or 0),
                "last_seen": int(row.get("last_seen") or 0),
                "invited_by": str(row.get("invited_by", ""))[:60],
            }
    return out


def _write(people: dict) -> None:
    try:
        write_atomic(_path(), json.dumps({"version": 1, "people": people}))
    except OSError as exc:
        log.warning("couldn't save the accounts: %s", exc)


def _ok_sub(sub: str) -> bool:
    """Google's subject id: digits, and never a path."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{4,64}", sub or ""))


def profile_id(sub: str) -> str:
    """The id their profile folder is named after. No colon — Windows."""
    return f"g-{sub}"


def everyone() -> list[dict]:
    with _lock:
        rows = list(_read().values())
    rows.sort(key=lambda r: (r["scope"] != "owner", -(r["last_seen"] or 0)))
    return rows


def get(sub: str) -> dict | None:
    with _lock:
        return _read().get(sub)


def count() -> int:
    with _lock:
        return len(_read())


def owner_email() -> str:
    return str(config.get("owner_email") or "").strip().lower()


def admit(sub: str, email: str, name: str, *, invited_by: str = "",
          scope: str = "") -> dict:
    """Record somebody who has just proved who they are.

    The first person in is the owner only when the email matches the one
    written down beforehand. Nobody becomes the owner by being early.
    """
    email = (email or "").strip().lower()
    with _lock:
        people = _read()
        row = people.get(sub)
        now = int(time.time())
        if row is None:
            row = {"sub": sub, "email": email, "name": name[:80],
                   "scope": scope if scope in SCOPES else "phone",
                   "created": now, "invited_by": invited_by[:60], "last_seen": now}
            if email and email == owner_email():
                row["scope"] = "owner"
            people[sub] = row
            log.info("new account: %s (%s)", email or sub, row["scope"])
        else:
            row["email"] = email or row["email"]
            row["name"] = name[:80] or row["name"]
            row["last_seen"] = now
            # The owner's address can be set after they first signed in.
            if email and email == owner_email() and row["scope"] != "owner":
                row["scope"] = "owner"
        _write(people)
        return dict(row)


def seen(sub: str) -> None:
    with _lock:
        people = _read()
        row = people.get(sub)
        if not row:
            return
        # Once a minute is enough; this is on every request otherwise.
        if int(time.time()) - int(row.get("last_seen") or 0) < 60:
            return
        row["last_seen"] = int(time.time())
        _write(people)


def set_scope(sub: str, scope: str) -> dict | None:
    if scope not in SCOPES:
        raise ValueError("no such scope")
    with _lock:
        people = _read()
        row = people.get(sub)
        if not row:
            return None
        # The last owner can't demote themselves out of a server nobody owns.
        if row["scope"] == "owner" and scope != "owner":
            others = [r for s, r in people.items()
                      if s != sub and r["scope"] == "owner"]
            if not others:
                raise ValueError("that's the only owner")
        row["scope"] = scope
        _write(people)
        log.info("%s is now %s", row.get("email") or sub, scope)
        return dict(row)


def forget(sub: str) -> bool:
    with _lock:
        people = _read()
        row = people.pop(sub, None)
        if row:
            _write(people)
            log.info("forgot the account %s", row.get("email") or sub)
        return bool(row)


def as_row(account: dict) -> dict:
    """An account in the shape the rest of the app already understands.

    Everything downstream takes a pass row: an id to keep a profile under, a
    name, a scope, and whether it's the owner. An account answers all four,
    so nothing else has to learn what an account is.
    """
    scope = account.get("scope", "phone")
    return {"id": profile_id(account["sub"]),
            "name": account.get("name") or account.get("email") or "somebody",
            "scope": "full" if scope == "owner" else scope,
            "owner": scope == "owner",
            "account": account["sub"],
            "email": account.get("email", "")}
