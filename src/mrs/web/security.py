"""Keeping the key out of URLs, and shutting the door on guessers.

Two things live here.

**Signed tokens.** The master key belongs in a header, where it stays out of
browser history, server logs and Referer. But a header is not always
possible: `<audio>.src` and `EventSource` take a URL and nothing else, and a
link you send someone is a URL by definition. Those get a token instead —
HMAC-signed with the master key, valid for a few hours, and useless once it
expires. The real key is then never in a URL at all.

**Bans.** A wrong key three times from the same address and that address is
refused for a day. Brute-forcing a 32-character key was never going to
succeed, but there's no reason to let anyone sit there trying, and the
refusal costs one dict lookup rather than a config read and a comparison.

Neither of these is encryption. Over plain http the key and the token are
both readable by anything on the path between here and the listener — put
this behind TLS or a private network if it faces the internet.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import math
import os
import json
import secrets
import threading
import time

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("security")

TOKEN_TTL = 12 * 3600          # how long a minted token stays good
MAX_LINK_HOURS = 365 * 24       # links longer than a year should be permanent
STRIKES = 3                    # wrong keys before the door shuts
BAN_SECONDS = 24 * 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ── passes ────────────────────────────────────────────────────────────
#
# A pass is a named, signed credential you hand to a person rather than a
# device. The signature makes it unforgeable; the registry beside it makes it
# nameable and revocable, which a bare signed token can never be — once
# you've signed "valid until Tuesday" there is no taking it back.
#
# So: the token carries id.expiry.scope.signature, and passes.json remembers
# which id belongs to whom. Ban Michael and it's Michael's id that stops
# working, not everyone's.

SCOPES = ("full", "phone")     # what the holder may play out of

# Reentrant: _held() takes it too, and note_use nests inside.
_PASS_LOCK = threading.RLock()


def _passes_file():
    return data_dir() / "passes.json"


@contextlib.contextmanager
def _held():
    """Hold the pass registry across processes for a read-modify-write.

    _PASS_LOCK only covers threads in one process. Everything here is
    load-the-whole-file, change one row, write-the-whole-file back — so two
    processes doing that at once means the second one's write silently
    deletes whatever the first added. That is somebody's link disappearing,
    and it happens for real: the app runs all day while `--check` or a second
    copy is started alongside it.

    A lock file taken with O_EXCL, because it's the one thing every
    filesystem agrees on. Never blocks forever: a stale lock from a process
    that died mid-write is broken after a couple of seconds, which is far
    better than the registry becoming unwritable until a reboot.
    """
    path = _passes_file().with_suffix(".lock")
    fd, end = None, time.time() + 3.0
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > end:
                try:                       # stale: whoever held it is gone
                    age = time.time() - path.stat().st_mtime
                    if age > 3.0:
                        path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                log.warning("pass registry stayed locked — writing anyway")
                break
            time.sleep(0.02)
        except OSError:
            break                          # can't lock here; don't refuse to work
    with _PASS_LOCK:
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                path.unlink(missing_ok=True)


def _load_passes() -> dict:
    try:
        return json.loads(_passes_file().read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_passes(rows: dict) -> bool:
    try:
        write_atomic(_passes_file(), json.dumps(rows, indent=1))
        return True
    except Exception as exc:
        log.debug("couldn't write the pass list: %s", exc)
        return False


def _sign(key: str, body: str) -> str:
    return _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()[:18])


def session_cookie(key: str, sub: str, days: int = 30) -> str:
    """A signed note saying which account this browser is. Nothing else.

    Signed with the server's own key, like a pass, so it can be checked
    without keeping a table of live sessions — and revoking somebody is a
    change to their account rather than a hunt for their devices.
    """
    body = f"{sub}.{int(time.time() + days * 86400)}"
    return f"{body}.{_sign(key, body)}"


def read_session(key: str, cookie: str) -> str:
    """The account id in a cookie, or "" if it isn't ours or has expired."""
    parts = (cookie or "").split(".")
    if len(parts) != 3 or not key:
        return ""
    sub, until, sig = parts
    body = f"{sub}.{until}"
    if not hmac.compare_digest(sig, _sign(key, body)):
        return ""
    try:
        if float(until) < time.time():
            return ""
    except ValueError:
        return ""
    return sub


# What each link has actually done, kept in memory between flushes so a
# progress ping every few seconds doesn't rewrite a json file every few
# seconds. Requests and finished tracks flush straight away — they're rare,
# and they're the numbers somebody is watching the list for.
_TALLY: dict[str, dict] = {}
_TALLY_DUE = 0.0


def note_use(tid: str, *, requests: int = 0, plays: int = 0,
             seconds: float = 0.0, ip: str = "") -> None:
    """Add to a link's running total.

    Per pass, not per address: the pass is the person, and somebody walking
    out of the house onto mobile data is still the same guest.
    """
    global _TALLY_DUE
    if not tid:
        return
    from ..core import stats
    stats.note(tid, requests=requests, plays=plays, seconds=seconds)
    with _PASS_LOCK:
        add = _TALLY.setdefault(tid, {"requests": 0, "plays": 0,
                                      "seconds": 0.0, "ip": ""})
        add["requests"] += requests
        add["plays"] += plays
        add["seconds"] += max(0.0, seconds)
        if ip:
            add["ip"] = ip
        if not (requests or plays) and time.time() < _TALLY_DUE:
            return
        _TALLY_DUE = time.time() + 30
    # The stats flush is a read-modify-write like any other, and it is the
    # one that runs constantly — every thirty seconds, in the copy that's
    # been playing music all day. Without the cross-process lock it was the
    # most likely thing to overwrite a pass somebody had just been given.
    with _held():
        rows, dirty = _load_passes(), False
        for pid, acc in list(_TALLY.items()):
            row = rows.get(pid)
            _TALLY.pop(pid, None)
            if not row:
                continue
            st = row.setdefault("stats", {})
            st["requests"] = int(st.get("requests", 0)) + acc["requests"]
            st["plays"] = int(st.get("plays", 0)) + acc["plays"]
            st["seconds"] = int(st.get("seconds", 0)) + int(acc["seconds"])
            if not st.get("first_used"):
                st["first_used"] = int(time.time())
            st["last_used"] = int(time.time())
            if acc["ip"]:
                row["last_ip"] = acc["ip"]
            row["last_seen"] = int(time.time())
            dirty = True
        if dirty:
            _save_passes(rows)


def _stats_of(row: dict) -> dict:
    st = dict(row.get("stats") or {})
    return {"requests": int(st.get("requests", 0)),
            "plays": int(st.get("plays", 0)),
            "minutes": round(int(st.get("seconds", 0)) / 60, 1),
            "first_used": int(st.get("first_used", 0)),
            "last_used": int(st.get("last_used", 0))}


def _fresh_id() -> str:
    """An id no other pass has.

    Collision odds are already negligible, but "negligible" is not the same
    as "checked", and two links sharing an id would mean revoking one
    silently revoking the other.
    """
    with _PASS_LOCK:
        taken = set(_load_passes())
    for _ in range(40):
        tid = secrets.token_urlsafe(6)
        if tid not in taken:
            return tid
    return secrets.token_urlsafe(12)          # absurd luck; go wider


def owner_pass(key: str) -> str:
    """The owner's own credential for devices that aren't this computer.

    The master key works on the machine it lives on, because the player is
    served without asking there. Anywhere else it has to travel in a link,
    and putting the key itself in one is the thing this whole scheme avoids.
    So the owner gets a pass like everybody else — permanent, full access,
    and marked so it carries owner rights rather than guest ones.

    It is as powerful as the key. Anyone who photographs that QR has the
    server, so it can be revoked and re-made like any other.
    """
    if not key:
        return ""
    with _held():
        rows = _load_passes()
        for tid, r in rows.items():
            if r.get("owner") and not r.get("revoked"):
                body = f"{tid}.0.full"
                return f"{body}.{_sign(key, body)}"
    got = issue(key, name="Your own devices", hours=0, scope="full", owner=True)
    return got.get("token", "")


def issue(key: str, name: str = "", hours: float = 24,
          scope: str = "full", internal: bool = False,
          owner: bool = False, siri: str = "") -> dict:
    """Mint a pass. Zero hours means it never expires.

    `internal` marks the player's own pass — the one it fetches so <audio>
    and EventSource have something to put in a URL. It's a real pass, but it
    isn't a link anybody was given, so it stays out of that list.

    `siri` is a profile id: the pass then belongs to that account and is only
    ever honoured as that person asking for a song. See siri_issue.
    """
    if not key:
        return {}
    try:
        hours = float(hours)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not math.isfinite(hours) or hours < 0 or hours > MAX_LINK_HOURS:
        return {}
    scope = scope if scope in SCOPES else "full"
    tid = _fresh_id()
    expires = 0 if hours <= 0 else int(time.time() + hours * 3600)
    body = f"{tid}.{expires}.{scope}"
    token = f"{body}.{_sign(key, body)}"

    with _held():
        rows = _load_passes()
        rows[tid] = {"name": (name or "").strip()[:40] or "unnamed",
                     "scope": scope, "expires": expires,
                     "created": int(time.time()), "revoked": False,
                     "internal": bool(internal), "owner": bool(owner),
                     "last_seen": 0}
        if siri:
            rows[tid]["siri"] = str(siri)[:80]
        if not _save_passes(rows):
            return {}
    log.info("issued a %s pass to %r (%s)", scope, name or "unnamed",
             "never expires" if not expires else f"{hours:g}h")
    return {"id": tid, "token": token, "name": name or "unnamed",
            "scope": scope, "expires": expires}


def read_token(key: str, token: str) -> dict | None:
    """The pass behind a token, or None if it isn't one we'd honour.

    Checks the signature first, so a made-up id never reaches the registry,
    then the expiry, then whether it's been revoked.
    """
    if not key or not token:
        return None
    try:
        tid, expires, scope, sig = token.split(".", 3)
        body = f"{tid}.{expires}.{scope}"
        if not hmac.compare_digest(sig, _sign(key, body)):
            return None
        expires = int(expires)
    except Exception:
        return None

    with _held():
        rows = _load_passes()
        row = rows.get(tid)
        # A pass with no registry entry is one whose record was deleted;
        # treat that as revoked rather than trusting the signature alone.
        if not row or row.get("revoked"):
            return None
        # The registry decides when a pass dies, not the token. The signature
        # proves we issued this id with this scope and cannot be forged into
        # a longer life — but the date baked into it is the date it was
        # minted with, and the owner is allowed to change their mind. Reading
        # it from the row is what lets Extend revive the link somebody
        # already has, instead of making them send a new one.
        if "expires" in row:
            expires = int(row.get("expires") or 0)
        if expires and expires < time.time():
            return None
        if row.get("last_seen", 0) < time.time() - 300:
            row["last_seen"] = int(time.time())
            _save_passes(rows)
    return {"id": tid, "scope": row.get("scope", scope) or "full",
            "name": row.get("name", ""), "expires": expires,
            "internal": bool(row.get("internal")),
            "owner": bool(row.get("owner")),
            "siri": str(row.get("siri") or "")}


def was_issued(key: str, token: str) -> bool:
    """Whether this token carries our signature.

    Somebody holding one had a real credential once, even if it has since been
    revoked, removed with its account or run out -- which is not somebody
    guessing, and shouldn't be treated as if they were.
    """
    if not key or not token:
        return False
    try:
        tid, expires, scope, sig = token.split(".", 3)
        return hmac.compare_digest(sig, _sign(key, f"{tid}.{expires}.{scope}"))
    except Exception:
        return False


def check_token(key: str, token: str) -> bool:
    return read_token(key, token) is not None


def token_scope(key: str, token: str) -> str:
    row = read_token(key, token)
    return row["scope"] if row else ""


def reissue_token(key: str, tid: str) -> str:
    """Rebuild the token string for a pass that already exists.

    The registry keeps who and what, not the token itself — there's no
    reason to store a credential we can regenerate from the key. Lets the
    settings page show a copyable link for a pass minted days ago.
    """
    if not key or not tid:
        return ""
    with _PASS_LOCK:
        row = _load_passes().get(tid)
    if not row or row.get("revoked"):
        return ""
    expires = int(row.get("expires", 0))
    if expires and expires < time.time():
        return ""
    body = f"{tid}.{expires}.{row.get('scope', 'full')}"
    return f"{body}.{_sign(key, body)}"


# How long a link stays on the list after it dies. A link that vanishes the
# moment it expires takes its name and its history with it, and the first
# you know is that somebody says "it stopped working" about a thing you can
# no longer see. A day is long enough to notice and press Extend.
GRACE = 86400


def list_passes() -> list[dict]:
    now = time.time()
    with _held():
        rows = _load_passes()
    out = []
    for tid, r in rows.items():
        if r.get("internal"):
            continue                 # the player's own, not a link you gave out
        if r.get("owner"):
            continue                 # yours, shown in its own place
        if r.get("siri"):
            continue                 # somebody's own key, on their own page
        exp = r.get("expires", 0)
        out.append({
            "id": tid, "name": r.get("name", "unnamed"),
            "scope": r.get("scope", "full"),
            "revoked": bool(r.get("revoked")),
            "expires": exp,
            "expired": bool(exp and exp < now),
            "hours_left": None if not exp else max(0, round((exp - now) / 3600, 1)),
            # Only meaningful once it's dead: how long is left to change
            # your mind before the row goes for good.
            "removed_in_hours": (round((exp + GRACE - now) / 3600, 1)
                                 if exp and exp < now else None),
            "created": r.get("created", 0),
            "last_seen": r.get("last_seen", 0),
            "last_ip": r.get("last_ip", ""),
            "stats": _stats_of(r),
        })
    out.sort(key=lambda r: -(r["created"] or 0))
    return out


def extend(tid: str, hours: float = 24) -> dict:
    """Give a link more time. Zero hours makes it permanent.

    Measured from now rather than from when it died, because "another day"
    said about a link that expired yesterday means a day from now.
    """
    try:
        hours = float(hours)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "message": "hours must be a number"}
    if not math.isfinite(hours) or hours < 0 or hours > MAX_LINK_HOURS:
        return {"ok": False, "message": f"hours must be between 0 and {MAX_LINK_HOURS:g}"}
    with _held():
        rows = _load_passes()
        row = rows.get(tid)
        if not row or row.get("internal") or row.get("owner") or row.get("siri"):
            return {"ok": False, "message": "No such link"}
        was = int(row.get("expires") or 0)
        row["expires"] = 0 if hours <= 0 else int(time.time() + hours * 3600)
        # Extending something you had revoked is plainly meant to bring it
        # back; leaving it revoked would be a button that does nothing.
        row["revoked"] = False
        if not _save_passes(rows):
            return {"ok": False, "message": "Couldn't save that link"}
    log.info("extended %s: %s -> %s", tid,
             "never" if not was else time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(was)),
             "never" if not row["expires"] else
             time.strftime("%Y-%m-%d %H:%M", time.localtime(row["expires"])))
    return {"ok": True, "expires": row["expires"],
            "message": ("That link no longer expires" if not row["expires"]
                        else f"Another {hours:g} hours on that link")}


def revoke(tid: str) -> bool:
    """Ban one pass. The link keeps its shape and stops working."""
    with _held():
        rows = _load_passes()
        row = rows.get(tid)
        if not row:
            return False
        row["revoked"] = True
        if not _save_passes(rows):
            return False
    log.warning("revoked the pass for %r", row.get("name", tid))
    return True


def revoke_owner_pass() -> int:
    """Bin your own link too.

    It's kept out of list_passes because it isn't something you handed to
    anybody — but it is a credential in a QR code, and "revoke everything"
    that leaves the most powerful one alive isn't revoking everything. A
    fresh one is minted the next time an address is asked for.
    """
    gone = 0
    with _held():
        rows = _load_passes()
        for tid, r in rows.items():
            if r.get("owner") and not r.get("revoked"):
                r["revoked"] = True
                gone += 1
        if gone and not _save_passes(rows):
            return 0
    if gone:
        log.warning("revoked your own pass as part of a lockdown")
    return gone


def restore_pass(tid: str) -> bool:
    with _held():
        rows = _load_passes()
        row = rows.get(tid)
        if not row:
            return False
        row["revoked"] = False
        if not _save_passes(rows):
            return False
    return True


# Siri keys. A person sets a Shortcut up once, on a phone, and it has to go on
# working -- so these never expire -- but it must not be the person's whole
# login pasted into an automation. It can ask for a song and do nothing else
# (api._siri_row enforces that), it is theirs to make and to take back, and it
# dies with the account.
MAX_SIRI_KEYS = 3


def siri_keys(pid: str) -> list[dict]:
    """What an account has, without the keys themselves."""
    if not pid:
        return []
    with _held():
        rows = _load_passes()
    out = [{"id": tid, "name": r.get("name", "Siri"),
            "created": int(r.get("created", 0)),
            "last_seen": int(r.get("last_seen", 0))}
           for tid, r in rows.items() if r.get("siri") == pid and not r.get("revoked")]
    out.sort(key=lambda r: r["created"])
    return out


def siri_issue(key: str, pid: str, name: str = "") -> dict:
    """A new key for one account, or {"error": ...} saying why not."""
    if not key or not pid:
        return {"error": "no key"}
    if len(siri_keys(pid)) >= MAX_SIRI_KEYS:
        return {"error": "limit"}
    got = issue(key, name=(name or "Siri").strip()[:30], hours=0, scope="phone", siri=pid)
    return got or {"error": "save"}


def siri_token(key: str, pid: str, tid: str) -> str:
    """The key again, for the account it belongs to and nobody else."""
    if not key or not pid or not tid:
        return ""
    with _held():
        row = _load_passes().get(tid)
    if not row or row.get("siri") != pid or row.get("revoked"):
        return ""
    body = f"{tid}.0.{row.get('scope', 'phone')}"
    return f"{body}.{_sign(key, body)}"


def siri_revoke(pid: str, tid: str) -> bool:
    with _held():
        row = _load_passes().get(tid)
    if not row or row.get("siri") != pid:
        return False
    return forget_pass(tid)


def siri_forget(pid: str) -> int:
    """Every key an account had. For when the account goes."""
    if not pid:
        return 0
    with _held():
        rows = _load_passes()
        mine = [tid for tid, r in rows.items() if r.get("siri") == pid]
        for tid in mine:
            del rows[tid]
        if mine and not _save_passes(rows):
            raise RuntimeError("couldn't remove their Siri keys")
    return len(mine)


def forget_pass(tid: str) -> bool:
    with _held():
        rows = _load_passes()
        if tid not in rows:
            return False
        del rows[tid]
        if not _save_passes(rows):
            return False
    return True


def tidy_passes() -> int:
    """Drop expired passes once their grace day is up.

    Internal ones go as soon as they expire — the player mints a fresh one
    every time it loads, so keeping the dead ones only grows the file.

    Everything else gets GRACE first. The point of the delay is that an
    expired link is still worth looking at: whose it was, what they played,
    and whether you meant to let it lapse. Extend is only reachable while
    the row is still there.
    """
    now = time.time()
    with _held():
        rows = _load_passes()
        spent = [t for t, r in rows.items()
                 if r.get("internal") and r.get("expires") and r["expires"] < now]
        for t in spent:
            del rows[t]
        if spent:
            _save_passes(rows)
    cutoff = time.time() - GRACE
    with _held():
        rows = _load_passes()
        dead = [t for t, r in rows.items()
                if r.get("expires") and r["expires"] < cutoff]
        for t in dead:
            del rows[t]
        if dead:
            _save_passes(rows)
    return len(dead)


def same_key(supplied: str, expected: str) -> bool:
    """Constant-time, so the key can't be guessed a character at a time."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


# ── bans ──────────────────────────────────────────────────────────────
class Bans:
    """Wrong key three times and you're not welcome for a day.

    Kept on disk, because a ban that a restart clears is a ban an attacker
    can wait out — and this program gets restarted a lot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._strikes: dict[str, list] = {}     # ip -> [count, first_seen]
        self._until: dict[str, float] = {}      # ip -> banned until
        self._load()

    # -- persistence --
    def _file(self):
        return data_dir() / "blocked.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8-sig"))
            now = time.time()
            self._until = {ip: t for ip, t in (raw.get("until") or {}).items()
                           if float(t) > now}
        except Exception:
            self._until = {}

    def _save(self) -> None:
        try:
            write_atomic(self._file(), json.dumps({"until": self._until}, indent=1))
        except Exception as exc:
            log.debug("couldn't write the ban list: %s", exc)

    # -- the two questions worth asking --
    def blocked(self, ip: str) -> bool:
        if not ip or _is_local(ip):
            return False
        with self._lock:
            until = self._until.get(ip, 0)
            if not until:
                return False
            if until < time.time():
                del self._until[ip]
                self._save()
                return False
            return True

    def wrong_key(self, ip: str) -> bool:
        """Record a failure. True once it's tipped into a ban."""
        if not ip or _is_local(ip):
            return False
        now = time.time()
        with self._lock:
            count, first = self._strikes.get(ip, [0, now])
            # Strikes age out, so an honest client with a stale key months
            # apart isn't treated as an attack.
            if now - first > BAN_SECONDS:
                count, first = 0, now
            count += 1
            self._strikes[ip] = [count, first]
            if count < STRIKES:
                log.info("bad key from %s (%d/%d)", ip, count, STRIKES)
                return False
            self._until[ip] = now + BAN_SECONDS
            self._strikes.pop(ip, None)
            self._save()
        log.warning("blocked %s for %d hours — %d bad keys",
                    ip, BAN_SECONDS // 3600, STRIKES)
        return True

    def good_key(self, ip: str) -> None:
        """A success wipes the slate; a typo shouldn't accumulate."""
        if not ip:
            return
        with self._lock:
            self._strikes.pop(ip, None)

    # -- for the settings panel --
    def listing(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [{"ip": ip, "minutes_left": int((t - now) / 60)}
                    for ip, t in sorted(self._until.items(), key=lambda kv: -kv[1])
                    if t > now]

    def forgive(self, ip: str = "") -> int:
        with self._lock:
            if ip:
                gone = 1 if self._until.pop(ip, None) else 0
                self._strikes.pop(ip, None)
            else:
                gone = len(self._until)
                self._until.clear()
                self._strikes.clear()
            self._save()
        return gone


_PRIVATE = ("10.", "192.168.", "127.", "169.254.", "::1", "fc", "fd")


def _own_wan() -> str:
    """This network's own public address, cached by the net module."""
    try:
        from ..core.net import _wan_cache
        return _wan_cache.get("ip") or ""
    except Exception:
        return ""


def is_home(ip: str) -> bool:
    """Is this address actually inside the house?

    Deliberately not _is_local. That one answers "should this be exempt from
    banning" and says yes to our own public address, which is right for bans
    and wrong for anything else: a connection arriving *from* the WAN address
    — a router looping a forwarded port back on itself, say — is not in the
    house, and the open-LAN rule hands out the master key.

    Two different questions had one answer, and the answer was the generous
    one. This is the strict one: loopback and genuinely private ranges only.
    """
    if not ip:
        return False        # unknown is not "trusted", unlike for bans
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return ip.lower().startswith(_PRIVATE)


def _is_local(ip: str) -> bool:
    """Home network addresses are never banned.

    Not because they're trusted — they still need a valid key — but because
    the thing this defends against arrives from the internet, and the
    realistic way a ban fires on the LAN is your own phone holding a link
    whose token expired. Locking the household out for a day to slow down an
    attacker who is already inside the house is the wrong trade.
    """
    if not ip:
        return True
    # Your own public address, which is what you arrive from when you test
    # the link on mobile data. Three tries with a stale link would otherwise
    # shut out your whole household for a day — and it did.
    if ip and ip == _own_wan():
        return True
    if ip.startswith("172."):                      # 172.16-31 are private
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return ip.lower().startswith(_PRIVATE)


bans = Bans()


def random_port() -> int:
    """A port nothing scans by habit.

    Everything below 1024 and the usual suspects above it are swept
    constantly by anyone with a spare afternoon. A random five-digit port
    isn't security — anyone who scans all 65535 still finds it — but it takes
    you out of the drive-by traffic entirely, which is most of it.
    """
    import socket as _socket
    for _ in range(40):
        candidate = secrets.randbelow(45000) + 20000
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
            probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", candidate))
                return candidate
            except OSError:
                continue
    return 7420
