"""Keeping a hostname pointed at a home address that keeps moving.

A residential IP is public but not permanent — it changes on a reconnect, a
line fault, an engineer visit, or for no visible reason at all. Every link
handed out is then quietly dead, and it looks like the server broke rather
than the address moving underneath it.

So: a name, and something that keeps it honest. This talks the standard
"nic/update" protocol that Dynu and most others implement, which is a plain
GET with basic auth and a one-word answer.

Nothing here makes an unreachable network reachable. That's the port-forward
rule's job, and no amount of DNS substitutes for it — this only solves the
half where the address you forwarded to has changed.
"""

from __future__ import annotations

import base64
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import config
from ..logging_setup import get

log = get("ddns")

CHECK_EVERY = 5 * 60          # how often to look for a change
RETRY_AFTER = 60              # after a failure, before trying again

# The one-word answers the protocol defines. Anything starting "good" or
# "nochg" is success; the rest are worth showing to somebody.
_MEANING = {
    "good": "updated",
    "nochg": "already correct",
    "badauth": "username or password rejected",
    "notfqdn": "that hostname isn't a full domain name",
    "nohost": "no such hostname on this account",
    "numhost": "too many hostnames",
    "abuse": "the provider has blocked this hostname",
    "dnserr": "the provider had a DNS error",
    "911": "the provider is having problems — try later",
}

PROVIDERS = {
    "dynu": "https://api.dynu.com/nic/update",
    "duckdns": "https://www.duckdns.org/update",
    "noip": "https://dynupdate.no-ip.com/nic/update",
    "afraid": "https://freedns.afraid.org/nic/update",
}

_state: dict = {"last": "", "at": 0.0, "detail": "not set up", "ok": None}
_lock = threading.Lock()


def configured() -> bool:
    return bool((config.get("ddns_hostname") or "").strip()
                and (config.get("ddns_user") or "").strip())


def status() -> dict:
    with _lock:
        out = dict(_state)
    out["hostname"] = (config.get("ddns_hostname") or "").strip()
    out["provider"] = config.get("ddns_provider", "dynu")
    out["configured"] = configured()
    out["minutes_ago"] = round((time.time() - out["at"]) / 60, 1) if out["at"] else None
    return out


def _note(ok: bool | None, detail: str, ip: str = "") -> dict:
    with _lock:
        _state.update({"ok": ok, "detail": detail, "at": time.time()})
        if ip:
            _state["last"] = ip
    return status()


def update(ip: str = "", force: bool = False) -> dict:
    """Point the hostname at an address. Returns the new status."""
    if not configured():
        return _note(None, "not set up")

    from . import net
    ip = ip or net.wan_ip(force=force)
    if not ip:
        return _note(False, "couldn't work out this network's address")

    with _lock:
        unchanged = _state["last"] == ip and _state["ok"]
    if unchanged and not force:
        return status()

    host = (config.get("ddns_hostname") or "").strip()
    user = (config.get("ddns_user") or "").strip()
    secret = (config.get("ddns_password") or "").strip()
    base = PROVIDERS.get(config.get("ddns_provider", "dynu"), PROVIDERS["dynu"])

    url = f"{base}?{urllib.parse.urlencode({'hostname': host, 'myip': ip})}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "MusicRequestServer/3 ddns",
        # Basic auth is what this protocol specifies. Over https, which every
        # provider here requires.
        "Authorization": "Basic " + base64.b64encode(
            f"{user}:{secret}".encode()).decode(),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        return _note(False, f"provider said {exc.code}")
    except Exception as exc:
        return _note(False, f"couldn't reach the provider: {type(exc).__name__}")

    word = body.split()[0].lower() if body else ""
    meaning = _MEANING.get(word, body[:60] or "no answer")
    if word in ("good", "nochg"):
        log.info("%s now points at %s (%s)", host, ip, meaning)
        return _note(True, meaning, ip)
    log.warning("ddns update refused: %s", meaning)
    return _note(False, meaning)


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        wait = CHECK_EVERY
        try:
            if configured():
                got = update()
                if got.get("ok") is False:
                    wait = RETRY_AFTER
        except Exception as exc:
            log.debug("ddns loop: %s", exc)
            wait = RETRY_AFTER
        stop.wait(wait)


_stop = threading.Event()


def start() -> None:
    """Watch for the address changing, quietly, for as long as we run."""
    if not configured():
        log.debug("no dynamic dns configured")
        return
    threading.Thread(target=_loop, args=(_stop,), daemon=True,
                     name="ddns").start()
    log.info("keeping %s pointed here", config.get("ddns_hostname"))


def stop() -> None:
    _stop.set()
