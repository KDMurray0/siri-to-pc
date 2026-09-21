"""Signing in with Google, with no library and no key handling.

The authorization code flow: the browser goes to Google, comes back with a
code, and this machine trades that code for an id_token by calling Google
directly over TLS with the client secret. Because that answer comes straight
from Google's token endpoint rather than through the browser, its contents
are already known to be Google's — the fields are still checked (who it was
issued to, who issued it, when, and the nonce that ties it to the request
that started), but no JWT signature verification and no crypto dependency.

Nothing here trusts anything the browser carried except a state string this
machine signed and remembers issuing.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get

log = get("google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
UA = {"User-Agent": "MusicRequestServer/2.0 (personal music player)"}

# Sign-ins that have started and not finished. Small, short-lived, and in
# memory: a restart mid-sign-in is a sign-in you do again.
_PENDING: dict[str, dict] = {}
_PENDING_FOR = 600.0
# OAuth state is attacker-controlled at the start endpoint.  Keep a hard
# process-wide ceiling and a smaller ceiling per requester so abandoned starts
# cannot grow memory or make every later request scan an unbounded mapping.
_PENDING_MAX = 128
_PENDING_PER_IP = 8
_lock = threading.RLock()

# Somebody Google has just vouched for, whom we have never seen, and who came in
# by "Log in" rather than "Sign up". They have proved who they are but not agreed
# to anything, and there is no name yet -- so they wait here, server-side, for the
# ten minutes it takes to pick one. The browser holds only a random handle; the
# verified identity itself never leaves this process.
_CLAIMS: dict[str, dict] = {}
_CLAIMS_MAX = 128


class SignInBusy(RuntimeError):
    """Too many unfinished Google sign-ins are already outstanding."""


def configured() -> bool:
    return bool(str(config.get("google_client_id") or "").strip()
                and str(config.get("google_client_secret") or "").strip())


def redirect_uri() -> str:
    """Where Google sends them back. Must match the Console exactly."""
    from ..core import net
    host = str(config.get("ddns_hostname") or "").strip()
    if not host:
        return ""
    # The address the outside world uses: Google sends the browser there, so
    # it has to be one that browser can reach, prefix and all, and it has to
    # match what is registered in the Console character for character.
    return net.public_base(host) + "/auth/google/callback"


def start(next_path: str = "/player", invited_by: str = "",
          scope: str = "", client_ip: str = "", mode: str = "login",
          signup: dict | None = None) -> str:
    """The URL to send somebody to, and the state that remembers why.

    `mode` is what they pressed: "signup" carries the name and agreements they
    already gave on our page (kept here, not in the url Google sees), "login"
    carries nothing and finds an account or asks for the rest afterwards.
    """
    if not configured():
        return ""
    uri = redirect_uri()
    if not uri:
        return ""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    with _lock:
        now = time.time()
        for old, row in list(_PENDING.items()):
            if now - row["at"] > _PENDING_FOR:
                _PENDING.pop(old, None)
        if len(_PENDING) >= _PENDING_MAX:
            raise SignInBusy("too many sign-ins are already in progress")
        if client_ip and sum(1 for row in _PENDING.values()
                             if row.get("ip") == client_ip) >= _PENDING_PER_IP:
            raise SignInBusy("too many sign-ins from this address")
        _PENDING[state] = {"at": now, "nonce": nonce, "next": next_path,
                           "invited_by": invited_by, "scope": scope, "uri": uri,
                           "ip": client_ip, "mode": mode if mode == "signup" else "login",
                           "signup": dict(signup or {})}
    query = urllib.parse.urlencode({
        "client_id": str(config.get("google_client_id")).strip(),
        "redirect_uri": uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        # Always ask which account: a household machine is signed into
        # somebody's Google already, and silently reusing it hands them
        # whatever the first person's account can do.
        "prompt": "select_account",
    })
    return f"{AUTH_URL}?{query}"


def pending(state: str) -> dict | None:
    """The sign-in this state belongs to. One use only."""
    with _lock:
        row = _PENDING.pop(state or "", None)
    if not row or time.time() - row["at"] > _PENDING_FOR:
        return None
    return row


def hold(who: dict, row: dict) -> str:
    """Keep a verified stranger while they choose a name. Returns the handle."""
    handle = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for old, held in list(_CLAIMS.items()):
            if now - held["at"] > _PENDING_FOR:
                _CLAIMS.pop(old, None)
        if len(_CLAIMS) >= _CLAIMS_MAX:
            raise SignInBusy("too many people are finishing signing up")
        _CLAIMS[handle] = {"at": now, "who": dict(who), "next": row.get("next", "/player")}
    return handle


def peek_claim(handle: str) -> dict | None:
    """What to show them while they decide: a suggested name and their photo."""
    with _lock:
        held = _CLAIMS.get(handle or "")
    if not held or time.time() - held["at"] > _PENDING_FOR:
        return None
    who = held["who"]
    return {"name": who.get("name", ""), "picture": who.get("picture", ""),
            "next": held["next"]}


def take_claim(handle: str) -> dict | None:
    """The verified identity behind a handle. One use only."""
    with _lock:
        held = _CLAIMS.pop(handle or "", None)
    if not held or time.time() - held["at"] > _PENDING_FOR:
        return None
    return {**held["who"], "next": held["next"]}


def _post(url: str, form: dict, timeout: float = 12.0) -> dict | None:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, headers=dict(
        UA, **{"Content-Type": "application/x-www-form-urlencoded"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        log.warning("google token exchange failed: %s", exc)
        return None


def _claims(id_token: str) -> dict:
    """The middle of a JWT, without verifying it — see the note up top."""
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        got = json.loads(base64.urlsafe_b64decode(part).decode())
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def finish(code: str, row: dict) -> dict | None:
    """Trade the code for who they are. None if anything doesn't add up."""
    if not configured() or not code:
        return None
    got = _post(TOKEN_URL, {
        "code": code,
        "client_id": str(config.get("google_client_id")).strip(),
        "client_secret": str(config.get("google_client_secret")).strip(),
        "redirect_uri": row.get("uri") or redirect_uri(),
        "grant_type": "authorization_code",
    })
    if not got or not got.get("id_token"):
        return None
    claims = _claims(got["id_token"])
    want_id = str(config.get("google_client_id")).strip()
    if claims.get("aud") != want_id:
        log.warning("id_token was issued to somebody else")
        return None
    if claims.get("iss") not in ISSUERS:
        log.warning("id_token came from %r", claims.get("iss"))
        return None
    if float(claims.get("exp") or 0) < time.time() - 60:
        log.warning("id_token had already expired")
        return None
    if row.get("nonce") and claims.get("nonce") != row["nonce"]:
        log.warning("id_token answers a different sign-in")
        return None
    if not claims.get("sub"):
        return None
    # An unverified address is one anybody could have typed into a Google
    # Workspace they control; the address is what an invitation is written
    # against, so it has to be one Google says it checked.
    if claims.get("email") and not claims.get("email_verified"):
        log.warning("google has not verified an address")
        return None
    return {"sub": str(claims["sub"]), "email": str(claims.get("email", "")),
            "name": str(claims.get("name") or claims.get("given_name") or ""),
            "picture": str(claims.get("picture") or "")}
