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
from ..paths import data_dir, exclusive_file_lock, write_atomic

log = get("accounts")

# What an account may do. Deliberately the same words a link's scope uses, so
# everything downstream that already understands a scope needs no new case.
SCOPES = ("owner", "full", "phone", "blocked")
# New Google identities must never be able to self-assign ownership.  Owners
# are promoted solely by the configured owner email or by an existing owner.
NEW_ACCOUNT_SCOPES = ("full", "phone", "blocked")
FILE = "accounts.json"
# Bumped when the privacy notice changes in a way people should be asked about
# again. Stored with each acceptance, so "who agreed to what" is a fact.
TERMS_VERSION = "1"


def tag(sub: str) -> str:
    """How an account appears in a log: enough to follow one, not to name one.

    A log outlives the account it mentions, and an address in a log is
    personal data that deleting the account would leave behind.
    """
    return "acct-" + str(sub)[:6]


def clean_name(raw: str) -> str:
    """A display name somebody typed: printable, one line, not empty."""
    import unicodedata
    text = "".join(ch for ch in str(raw or "") if unicodedata.category(ch)[0] != "C")
    return " ".join(text.split())[:40]
_lock = threading.RLock()


class AccountPersistenceError(RuntimeError):
    """The identity change was not durably saved."""


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
                "picture": str(row.get("picture", ""))[:400],
                # A damaged account file must fail closed.  Treating an
                # unknown value as phone access would turn corruption into an
                # accidental admission decision.
                "scope": row.get("scope") if row.get("scope") in SCOPES else "blocked",
                "created": int(row.get("created") or 0),
                "last_seen": int(row.get("last_seen") or 0),
                "invited_by": str(row.get("invited_by", ""))[:60],
                # Consent is recorded, not assumed: what was agreed, to which
                # version of the notice, and when. Nothing here defaults to yes.
                "terms_at": int(row.get("terms_at") or 0),
                "terms_version": str(row.get("terms_version", ""))[:12],
                "tracking": row.get("tracking") is True,
                "tracking_at": int(row.get("tracking_at") or 0),
            }
    return out


def _write(people: dict) -> bool:
    try:
        write_atomic(_path(), json.dumps({"version": 1, "people": people}))
        return True
    except Exception as exc:
        log.warning("couldn't save the accounts: %s", exc)
        return False


def _ok_sub(sub: str) -> bool:
    """Google's subject id: digits, and never a path."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{4,64}", sub or ""))


def profile_id(sub: str) -> str:
    """The id their profile folder is named after. No colon — Windows."""
    return f"g-{sub}"


def by_profile(pid: str) -> dict | None:
    """The account whose profile id this is."""
    if not str(pid).startswith("g-"):
        return None
    return get(str(pid)[2:])


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


def default_scope() -> str:
    got = str(config.get("new_account_scope") or "blocked")
    return got if got in NEW_ACCOUNT_SCOPES else "blocked"


def admit(sub: str, email: str, name: str, *, picture: str = "",
          invited_by: str = "", scope: str = "", terms: bool = False,
          tracking: bool = False) -> dict:
    """Record somebody who has just proved who they are.

    A new sign-in lands at the owner's chosen default -- their own device, or
    held as blocked until the owner lets them in. The first person in is the
    owner only when the email matches the one written down beforehand; nobody
    becomes the owner by being early.
    """
    email = (email or "").strip().lower()
    with _lock, exclusive_file_lock(_path()):
        people = _read()
        row = people.get(sub)
        now = int(time.time())
        if row is None:
            # The admission route selects the scope from a validated pass or
            # a local-network policy.  Do not accept owner here: OAuth alone
            # must not create an owner account.
            admitted_scope = scope if scope in NEW_ACCOUNT_SCOPES else default_scope()
            row = {"sub": sub, "email": email,
                   "name": clean_name(name) or "Listener",
                   "picture": picture[:400], "scope": admitted_scope,
                   "created": now, "invited_by": invited_by[:60], "last_seen": now,
                   "terms_at": now if terms else 0,
                   "terms_version": TERMS_VERSION if terms else "",
                   "tracking": bool(tracking), "tracking_at": now if tracking else 0}
            if email and email == owner_email():
                row["scope"] = "owner"
            people[sub] = row
            log.info("new account: %s (%s)", tag(sub), row["scope"])
        else:
            row["email"] = email or row["email"]
            # The name they chose is theirs. Google's is a starting point for
            # an account that has none, never something to overwrite it with.
            if not row.get("name"):
                row["name"] = clean_name(name) or "Listener"
            if picture:
                row["picture"] = picture[:400]
            row["last_seen"] = now
            # The owner's address can be set after they first signed in.
            if email and email == owner_email() and row["scope"] != "owner":
                row["scope"] = "owner"
        if not _write(people):
            raise AccountPersistenceError("couldn't save the account")
        return dict(row)


def seen(sub: str) -> None:
    with _lock, exclusive_file_lock(_path()):
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
    with _lock, exclusive_file_lock(_path()):
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
        if not _write(people):
            raise AccountPersistenceError("couldn't save the account")
        log.info("%s is now %s", tag(sub), scope)
        return dict(row)


def set_consent(sub: str, *, tracking: bool | None = None,
                terms: bool | None = None) -> dict | None:
    """Record a change of mind, with when. Withdrawing is as easy as giving."""
    with _lock, exclusive_file_lock(_path()):
        people = _read()
        row = people.get(sub)
        if not row:
            return None
        now = int(time.time())
        if tracking is not None and bool(tracking) != bool(row.get("tracking")):
            row["tracking"] = bool(tracking)
            row["tracking_at"] = now
        if terms:
            row["terms_at"], row["terms_version"] = now, TERMS_VERSION
        if not _write(people):
            raise AccountPersistenceError("couldn't save the account")
        log.info("%s consent: tracking=%s", tag(sub), row["tracking"])
        return dict(row)


def rename(sub: str, name: str) -> dict | None:
    name = clean_name(name)
    if len(name) < 2:
        raise ValueError("A name needs at least two characters")
    with _lock, exclusive_file_lock(_path()):
        people = _read()
        row = people.get(sub)
        if not row:
            return None
        row["name"] = name
        if not _write(people):
            raise AccountPersistenceError("couldn't save the account")
        return dict(row)


def forget(sub: str) -> bool:
    with _lock, exclusive_file_lock(_path()):
        people = _read()
        row = people.pop(sub, None)
        if row:
            if not _write(people):
                raise AccountPersistenceError("couldn't save the account")
            log.info("forgot the account %s", tag(sub))
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
            "email": account.get("email", ""),
            "picture": account.get("picture", ""),
            # Read by whatever would otherwise learn from this person: an
            # account that hasn't opted in is heard, not studied.
            "tracking": bool(account.get("tracking"))}
