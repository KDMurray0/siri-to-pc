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
import hashlib
import hmac
import json
import secrets
import threading
import time

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("security")

TOKEN_TTL = 12 * 3600          # how long a minted token stays good
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

_PASS_LOCK = threading.Lock()


def _passes_file():
    return data_dir() / "passes.json"


def _load_passes() -> dict:
    try:
        return json.loads(_passes_file().read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_passes(rows: dict) -> None:
    try:
        write_atomic(_passes_file(), json.dumps(rows, indent=1))
    except Exception as exc:
        log.debug("couldn't write the pass list: %s", exc)


def _sign(key: str, body: str) -> str:
    return _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()[:18])


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
    with _PASS_LOCK:
        rows = _load_passes()
        for tid, r in rows.items():
            if r.get("owner") and not r.get("revoked"):
                body = f"{tid}.0.full"
                return f"{body}.{_sign(key, body)}"
    got = issue(key, name="Your own devices", hours=0, scope="full", owner=True)
    return got.get("token", "")


def issue(key: str, name: str = "", hours: float = 24,
          scope: str = "full", internal: bool = False,
          owner: bool = False) -> dict:
    """Mint a pass. hours <= 0 means it never expires.

    `internal` marks the player's own pass — the one it fetches so <audio>
    and EventSource have something to put in a URL. It's a real pass, but it
    isn't a link anybody was given, so it stays out of that list.
    """
    if not key:
        return {}
    scope = scope if scope in SCOPES else "full"
    tid = _fresh_id()
    expires = 0 if hours <= 0 else int(time.time() + hours * 3600)
    body = f"{tid}.{expires}.{scope}"
    token = f"{body}.{_sign(key, body)}"

    with _PASS_LOCK:
        rows = _load_passes()
        rows[tid] = {"name": (name or "").strip()[:40] or "unnamed",
                     "scope": scope, "expires": expires,
                     "created": int(time.time()), "revoked": False,
                     "internal": bool(internal), "owner": bool(owner),
                     "last_seen": 0}
        _save_passes(rows)
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
        if expires and expires < time.time():
            return None
    except Exception:
        return None

    with _PASS_LOCK:
        rows = _load_passes()
        row = rows.get(tid)
        # A pass with no registry entry is one whose record was deleted;
        # treat that as revoked rather than trusting the signature alone.
        if not row or row.get("revoked"):
            return None
        if row.get("last_seen", 0) < time.time() - 300:
            row["last_seen"] = int(time.time())
            _save_passes(rows)
    return {"id": tid, "scope": row.get("scope", scope) or "full",
            "name": row.get("name", ""), "expires": expires,
            "internal": bool(row.get("internal")),
            "owner": bool(row.get("owner"))}


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


def list_passes() -> list[dict]:
    now = time.time()
    with _PASS_LOCK:
        rows = _load_passes()
    out = []
    for tid, r in rows.items():
        if r.get("internal"):
            continue                 # the player's own, not a link you gave out
        if r.get("owner"):
            continue                 # yours, shown in its own place
        exp = r.get("expires", 0)
        out.append({
            "id": tid, "name": r.get("name", "unnamed"),
            "scope": r.get("scope", "full"),
            "revoked": bool(r.get("revoked")),
            "expires": exp,
            "expired": bool(exp and exp < now),
            "hours_left": None if not exp else max(0, round((exp - now) / 3600, 1)),
            "created": r.get("created", 0),
            "last_seen": r.get("last_seen", 0),
            "last_ip": r.get("last_ip", ""),
            "stats": _stats_of(r),
        })
    out.sort(key=lambda r: -(r["created"] or 0))
    return out


def revoke(tid: str) -> bool:
    """Ban one pass. The link keeps its shape and stops working."""
    with _PASS_LOCK:
        rows = _load_passes()
        row = rows.get(tid)
        if not row:
            return False
        row["revoked"] = True
        _save_passes(rows)
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
    with _PASS_LOCK:
        rows = _load_passes()
        for tid, r in rows.items():
            if r.get("owner") and not r.get("revoked"):
                r["revoked"] = True
                gone += 1
        if gone:
            _save_passes(rows)
    if gone:
        log.warning("revoked your own pass as part of a lockdown")
    return gone


def restore_pass(tid: str) -> bool:
    with _PASS_LOCK:
        rows = _load_passes()
        row = rows.get(tid)
        if not row:
            return False
        row["revoked"] = False
        _save_passes(rows)
    return True


def forget_pass(tid: str) -> bool:
    with _PASS_LOCK:
        rows = _load_passes()
        if tid not in rows:
            return False
        del rows[tid]
        _save_passes(rows)
    return True


def tidy_passes() -> int:
    """Drop expired passes that nobody has used in a fortnight.

    Internal ones go as soon as they expire — the player mints a fresh one
    every time it loads, so keeping the dead ones only grows the file.
    """
    now = time.time()
    with _PASS_LOCK:
        rows = _load_passes()
        spent = [t for t, r in rows.items()
                 if r.get("internal") and r.get("expires") and r["expires"] < now]
        for t in spent:
            del rows[t]
        if spent:
            _save_passes(rows)
    cutoff = time.time() - 14 * 86400
    with _PASS_LOCK:
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


# ── TLS ───────────────────────────────────────────────────────────────
def ensure_cert() -> tuple[str, str] | None:
    """A self-signed certificate, made once and reused.

    Without this the key, the token and everything you listen to cross the
    internet in clear text — a header keeps the key out of *logs*, it does
    nothing about anyone on the path. Self-signed means the browser objects
    the first time and you tap through; after that the connection is
    encrypted like any other.

    Not a substitute for a real certificate, and it can't prove the server is
    who it says it is — but it turns a plaintext link into an eavesdropping
    problem rather than a free one.
    """
    cert = data_dir() / "server.crt"
    key = data_dir() / "server.key"
    if cert.is_file() and key.is_file():
        return str(cert), str(key)
    try:
        import datetime
        import ipaddress
        import socket as _socket

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                             "Music Request Server")])
        # Every address it might be reached on, or the browser objects to the
        # name as well as the signature.
        alts = [x509.DNSName("localhost")]
        for ip in _local_addresses():
            try:
                alts.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                pass
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (x509.CertificateBuilder()
                   .subject_name(name).issuer_name(name)
                   .public_key(priv.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=3650))
                   .add_extension(x509.SubjectAlternativeName(alts),
                                  critical=False))
        crt = builder.sign(priv, hashes.SHA256())

        key.write_bytes(priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
        cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
        log.info("made a self-signed certificate at %s", cert)
        return str(cert), str(key)
    except Exception as exc:
        log.warning("couldn't make a certificate (%s) — staying on http", exc)
        return None


def _local_addresses() -> list[str]:
    import socket as _socket
    out = ["127.0.0.1"]
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        out.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None,
                                        _socket.AF_INET):
            addr = info[4][0]
            if addr not in out:
                out.append(addr)
    except Exception:
        pass
    return out


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
