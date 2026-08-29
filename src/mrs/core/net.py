"""Where this server can be reached from.

Three answers, and they're just addresses — no VPN, no tunnel, no third
party in the middle:

  this machine   127.0.0.1, for the app itself
  same wifi      the LAN address, for anything in the house
  the internet   your public address, once the port is forwarded

The last one is the only one that needs anything doing: one port-forward
rule on the router, pointing the chosen port at this machine. What keeps it
safe is in web/security.py — a key that lives in a header, links that carry
a signed pass instead of the key, and a door that shuts on anyone guessing.

The scheme follows the `https` setting, because a link that says http when
the server is only listening on https is a link that doesn't work.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from ..config import config
from ..logging_setup import get

log = get("net")

# Asked at most this often — it rarely changes and each answer costs a
# round trip to someone else's server.
_WAN_TTL = 15 * 60
_wan_cache: dict = {"ip": "", "at": 0.0}
_wan_lock = threading.Lock()

# Plain-text services that answer with nothing but the address. Tried in
# order; the first that replies wins.
_WAN_SOURCES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)


def scheme() -> str:
    return "https" if config.get("https") else "http"


def lan_ip() -> str:
    """The address of whichever interface would reach the internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packets sent, just routing
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def wan_ip(force: bool = False) -> str:
    """This network's public address, or "" if we can't find out."""
    with _wan_lock:
        fresh = time.time() - _wan_cache["at"] < _WAN_TTL
        if _wan_cache["ip"] and fresh and not force:
            return _wan_cache["ip"]

    found = ""
    for url in _WAN_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=6) as r:
                got = r.read().decode("ascii", "replace").strip()
            # Sanity-check it before believing it — these are other people's
            # servers and an error page is still a 200.
            parts = got.split(".")
            if len(parts) == 4 and all(p.isdigit() and int(p) < 256 for p in parts):
                found = got
                break
        except Exception as exc:
            log.debug("%s didn't answer: %s", url, exc)

    with _wan_lock:
        if found:
            _wan_cache.update({"ip": found, "at": time.time()})
        return found or _wan_cache["ip"]


def port_open(port: int, timeout: float = 4.0) -> bool | None:
    """Is the port actually reachable from outside?

    Answering this honestly needs something outside the network to try the
    connection, so it asks a port-checking service. None means we couldn't
    find out, which is different from a no.
    """
    ip = wan_ip()
    if not ip:
        return None

    # Ask ourselves first, over the public address. Most home routers loop
    # that back through the forward rule (hairpin NAT), so if we get our own
    # ping signature the rule is live — decided in a few milliseconds, by us,
    # with nobody else involved and nobody to rate-limit us. Only if that
    # fails do we need somebody outside to try the door.
    try:
        url = f"{scheme()}://{ip}:{int(port)}/api/ping"
        req = urllib.request.Request(url, headers={"User-Agent": "mrs"})
        ctx = None
        if scheme() == "https":
            import ssl
            ctx = ssl._create_unverified_context()   # our own self-signed cert
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            if b"music-request-server" in r.read(200):
                log.debug("port %s answered on %s — the forward rule is live",
                          port, ip)
                return True
    except Exception as exc:
        # Not a no: plenty of routers simply don't hairpin.
        log.debug("no loopback via %s: %s", ip, exc)

    try:
        req = urllib.request.Request(
            f"https://ports.yougetsignal.com/check-port.php?remoteAddress={ip}"
            f"&portNumber={int(port)}",
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace").lower()
        if "open" in body and "not open" not in body:
            return True
        if "closed" in body or "not open" in body:
            return False
    except Exception as exc:
        log.debug("port check failed: %s", exc)
    return None


def qr_svg(text: str, size: int = 176) -> str:
    """The address as an inline SVG QR code.

    SVG rather than a PNG so it stays sharp on a phone screen and needs no
    image library — the modules go out as one path.
    """
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)

    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if not row[x]:
                x += 1
                continue
            run = x
            while run < n and row[run]:
                run += 1
            parts.append(f"M{x} {y}h{run - x}v1h-{run - x}z")
            x = run
    path = "".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
            f'height="{size}" viewBox="0 0 {n} {n}" '
            f'shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code for {text.split("?")[0]}">'
            f'<rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')


def live_port() -> int:
    """The port actually being listened on, not the one that was asked for.

    They diverge more often than you'd think: the configured port can be held
    by something else — including a copy of this program that hasn't finished
    letting go — and the server steps to the next free one. Building links
    from config then produces addresses that point at nothing, which looks
    exactly like a firewall problem and isn't.
    """
    try:
        from ..server import runtime
        return int(runtime.get("port") or config.get("port", 5000))
    except Exception:
        return int(config.get("port", 5000))


def player_url(host: str, pass_token: str = "") -> str:
    port = live_port()
    q = f"?key={pass_token}" if pass_token else ""
    return f"{scheme()}://{host}:{port}/player{q}"


def addresses(pass_token: str = "") -> dict:
    """Every URL that reaches the player, furthest reach first.

    A pass can be handed in, and then every address carries it — that's what
    makes a copied link work for whoever you send it to.
    """
    port = live_port()
    rows = []

    # A hostname if there is one, because the raw address will move and take
    # every link you've handed out with it.
    host = (config.get("ddns_hostname") or "").strip()
    wan = wan_ip()
    if host:
        rows.append({"kind": "wan", "label": "Anywhere",
                     "url": player_url(host, pass_token), "host": host,
                     "named": True})
    elif wan:
        rows.append({"kind": "wan", "label": "Anywhere (needs the port forwarded)",
                     "url": player_url(wan, pass_token), "host": wan})
    rows.append({"kind": "lan", "label": "Same wifi",
                 "url": player_url(lan_ip(), pass_token), "host": lan_ip()})
    rows.append({"kind": "local", "label": "This machine",
                 "url": player_url("127.0.0.1", pass_token), "host": "127.0.0.1"})
    wanted = int(config.get("port", 5000))
    return {"addresses": rows, "port": port, "scheme": scheme(),
            "wan_ip": wan, "https": bool(config.get("https")),
            # If these disagree, a port-forward rule aimed at the configured
            # port points at nothing. Worth saying out loud rather than
            # letting it look like a firewall problem.
            "configured_port": wanted, "port_moved": wanted != port,
            "hostname": host}
