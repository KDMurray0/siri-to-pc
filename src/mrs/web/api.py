"""HTTP API.

FastAPI plus an SSE stream, so the UI doesn't poll. Blocking work (yt-dlp, mpv)
stays on threads via plain def routes.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
import json
import math
import mimetypes
import urllib.parse
import shutil
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse,
                               StreamingResponse)
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import config
from ..core import autoeq
from . import accounts
from . import google
from ..core import stats
from ..core import cast as cast_mod
from ..core import cookies as cookie_mod
from ..core import radio as radio_mod
from ..core.downloader import downloader
from ..core.extras import caster, scrobbler
from ..core.library import library
from ..core.playlists import playlists
from ..core.taste import taste
from ..events import Ev, bus
from . import security as sec
from .security import bans, same_key
from .policy import (OWNER as OWNER_ROUTES, READ_ONLY as READ_ONLY_ROUTES,
                     changing as api_changes, install as install_api_policy,
                     matching_route)
from ..logging_setup import get, log_path, spawn
from ..paths import resource_dir
from ..player import CAST_DEVICE, player
from ..requests import (add_spotify, handle_request, play_for_you,
                        play_station, play_video)
from ..resolve import catalog, llm, lyrics as lyrics_mod, spotify

log = get("api")

NEWLINE = chr(10)

app = FastAPI(title="Music Request Server", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(resource_dir()) / "web" / "templates"))
_start = time.time()


# ── the front door ────────────────────────────────────────────────────

@app.middleware("http")
async def _door(request: Request, call_next):
    """Enforce safe API verbs before handlers run and harden every response."""
    ip = (request.client.host if request.client else "") or ""
    if bans.blocked(ip):
        return JSONResponse({"detail": "Blocked"}, status_code=403)

    route = child = None
    if request.url.path.startswith("/api/"):
        route, child = matching_route(app, request.scope)
        if route:
            if request.method == "GET" and api_changes(
                    route.path, request.query_params,
                    (child or {}).get("path_params", {})) and not config.get(
                        "allow_legacy_get_mutations", False):
                return JSONResponse(
                    {"detail": "Use POST with a JSON body; GET does not change state"},
                    status_code=405, headers={"Allow": "POST"})

    if request.method == "POST" and (route or request.url.path == "/"):
        origin = request.headers.get("origin")
        if origin and not _same_origin(origin, request):
            return JSONResponse({"detail": "Cross-origin request refused"},
                                status_code=403)
        # Parameters go in the body. A credential may still ride in the URL:
        # a Shortcut or a shared link has nowhere else to put one, and
        # require_key decides whether that spelling is accepted.
        if set(request.query_params) - {"key", "token"}:
            return JSONResponse(
                {"detail": "POST parameters belong in the JSON body"},
                status_code=400)

    resp = await call_next(request)
    # The owner's trail: changes to the machine, not every skip and progress
    # tick — those arrive every few seconds per listener and would rewrite
    # the file each time.
    if (route and route.path[5:] in OWNER_ROUTES
            and (route.path[5:] not in READ_ONLY_ROUTES
                 if request.method == "POST" else
                 api_changes(route.path, request.query_params,
                             (child or {}).get("path_params", {})))):
        try:
            from ..core.audit import record
            row = getattr(request.state, "pass_row", None) or {}
            actor = ("owner" if not row or row.get("owner") else
                     "guest:" + str(row.get("name") or "shared link")[:30])
            record(f"{request.method} {route.path}", actor, resp.status_code)
        except Exception as exc:
            log.warning("couldn't write the owner audit trail: %s", exc)
    # A page fetched with ?key= or ?token= in its URL would otherwise hand
    # that URL to every third-party it links to.
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


def _same_origin(origin: str, request: Request) -> bool:
    """Compare normalized origins, including effective default ports."""
    from urllib.parse import urlsplit

    def parts(value):
        got = urlsplit(value)
        if got.scheme not in ("http", "https") or not got.hostname:
            return None
        try:
            port = got.port or (443 if got.scheme == "https" else 80)
        except ValueError:
            return None
        return got.scheme.lower(), got.hostname.lower(), port

    return parts(origin) is not None and parts(origin) == parts(str(request.base_url))


# ── auth ──────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _key_in_url_ok(request: Request) -> bool:
    """Raw key in a query string: only if allowed, or from this machine.

    The tray and the window open their pages with ?key=, and a loopback URL
    goes nowhere a network log or a Referer can see. Without this a strict
    install couldn't open its own window.
    """
    return (bool(config.get("allow_key_in_url", False))
            or _client_ip(request) in ("127.0.0.1", "::1"))


def _refuse(ip: str) -> None:
    """Say no, slowly, and count it against the address.

    The pause is deliberate. Three guesses is already the limit, but a
    request that costs half a second is a thousand times more expensive to
    grind through than one that costs nothing, and it makes a scanner look
    elsewhere long before it reaches the ban.
    """
    banned = bans.wrong_key(ip)
    time.sleep(0.5)
    raise HTTPException(status_code=403,
                        detail="Blocked" if banned else "Not authorised")


SESSION_COOKIE = "mrs_account"


def _account_row(request: Request) -> dict | None:
    """Who the signed-in cookie says this is, in pass-row shape.

    None means "nobody signed in here" and the older ways in are tried next.
    A cookie for an account that has since been forgotten is nobody.
    """
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie:
        return None
    sub = sec.read_session(config.get("api_key") or "", cookie)
    if not sub:
        return None
    person = accounts.get(sub)
    if not person:
        return None
    accounts.seen(sub)
    row = accounts.as_row(person)
    request.state.account = person
    if person.get("scope") == "blocked":
        row["blocked"] = True
    return row


def require_key(request: Request, key: str = Query(default=""),
                token: str = Query(default="")) -> bool:
    expected = config.get("api_key") or ""
    if not expected:
        return True                      # no key set: nothing to check
    ip = _client_ip(request)

    # Somebody who has signed in is themselves, whatever link they arrived
    # on originally. Checked first: an account can be taken away, and a link
    # they still hold shouldn't outrank that.
    row = _account_row(request)
    if row is not None:
        if row.get("blocked"):
            raise HTTPException(status_code=403, detail="That account is blocked")
        bans.good_key(ip)
        request.state.pass_row = row
        return _check_ip_lock(ip)

    # Where the key belongs. A header stays out of browser history, out of
    # access logs and out of Referer, which a query string does not.
    header = (request.headers.get("X-Music-Key")
              or request.headers.get("X-API-Key") or "")
    if header and same_key(header, expected):
        bans.good_key(ip)
        return _check_ip_lock(ip)
    if header:
        row = sec.read_token(expected, header)
        if row:
            bans.good_key(ip)
            request.state.pass_row = row
            return _check_ip_lock(ip)

    # <audio>.src and EventSource take a URL and nothing else, and a link you
    # send someone is a URL by definition. Those carry a signed token that
    # expires instead of the key itself.
    for candidate in (token, key):
        row = sec.read_token(expected, candidate) if candidate else None
        if row:
            bans.good_key(ip)
            request.state.pass_row = row      # scope is checked per-route
            return _check_ip_lock(ip)

    # The raw key in a URL still works until it's switched off, so an iOS
    # Shortcut built against the old scheme doesn't break on upgrade.
    if key and _key_in_url_ok(request) and same_key(key, expected):
        bans.good_key(ip)
        return _check_ip_lock(ip)

    _refuse(ip)
    return False                          # unreachable; _refuse raises


def is_owner(request: Request, key: str = "") -> bool:
    """Did this arrive with the actual key, rather than a token?"""
    expected = config.get("api_key") or ""
    if not expected:
        return True
    header = (request.headers.get("X-Music-Key")
              or request.headers.get("X-API-Key") or "")
    if header and same_key(header, expected):
        return True
    if key and _key_in_url_ok(request) and same_key(key, expected):
        return True
    # The owner's own pass, for their phone. Deliberately as powerful as the
    # key — it exists because the key can't safely travel in a link.
    row = getattr(request.state, "pass_row", None)
    return bool(row and row.get("owner"))


def require_admin(request: Request, key: str = Query(default=""),
                  token: str = Query(default="")) -> bool:
    """For anything that changes the machine rather than the music.

    A token is deliberately not enough here. Tokens go into links you hand
    out and into URLs that end up in someone's history — whoever holds one
    should be able to listen, not rewrite the settings, read a backup with
    the key in it, or mint themselves a fresh token when theirs expires.
    """
    require_key(request, key, token)          # bans + 403 for anything invalid
    if not is_owner(request, key):
        raise HTTPException(status_code=403,
                            detail="That needs the key, not a shared link")
    return True


def _check_ip_lock(ip: str) -> bool:
    if config.get("lock_ips"):
        allowed = config.get("allowed_ips") or []
        if allowed and ip not in allowed and not ip.startswith("127."):
            raise HTTPException(status_code=403, detail="IP not allowed")
    return True


Auth = Depends(require_key)
Owner = Depends(require_admin)


# ── pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, key: str = Query(default=""),
                token: str = Query(default="")):
    """The front door.

    Everyone lands here and there is one thing to do: sign in. The owner (at
    home, or arriving with the key) and anyone already signed in are sent
    straight on to the player -- a home screen you have to click past is one
    nobody wants -- and everyone else meets the sign-in page. A signed pass
    is still an invitation credential; signing in gives that person a durable
    identity after the invitation has been checked.
    """
    from .security import is_home
    ip = _client_ip(request)
    offered = (token or key
               or request.headers.get("X-Music-Key")
               or request.headers.get("X-API-Key") or "")
    home_owner = is_home(ip) and config.get("lan_open", True) and not offered
    row = _account_row(request)

    def landing(blocked=False):
        return templates.TemplateResponse(request, "landing.html", {
            "google": google.configured(),
            "server_name": config.get("server_name", "Music Request"),
            "blocked": blocked})

    if row and row.get("blocked"):
        return landing(blocked=True)
    owner = is_owner(request, key)
    if home_owner or (row is not None) or owner:
        # A key in the url is the owner's; keep it so the player still gets
        # the owner credential. A signed-in cookie carries itself.
        tail = (f"?key={urllib.parse.quote(key, safe='')}"
                if key and row is None and _key_in_url_ok(request)
                and same_key(key, config.get("api_key") or "") else "")
        return RedirectResponse("/player" + tail, status_code=302)
    return landing()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, key: str = Query(default=""),
                     token: str = Query(default="")):
    """The iOS Shortcut recipe, and the key it needs.

    Owner-only, because it prints the master key straight into the html. On
    the home network it opens as it always has.
    """
    import socket
    from .security import is_home

    ip = _client_ip(request)
    offered = (token or key
               or request.headers.get("X-Music-Key")
               or request.headers.get("X-API-Key") or "")
    home = is_home(ip) and config.get("lan_open", True) and not offered
    if not home:
        require_key(request, key, token)
        if not is_owner(request, key):
            raise HTTPException(
                status_code=403,
                detail="That page has the master key on it — it needs the key, "
                       "not a shared link")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        host = s.getsockname()[0]
        s.close()
    except Exception:
        host = "127.0.0.1"
    from ..core import net
    return templates.TemplateResponse(request, "setup.html", {
        "host": host, "port": net.live_port(),
        "api_key": config.get("api_key", ""),
        "key_in_url": bool(config.get("allow_key_in_url", False))})


@app.post("/")
async def siri(request: Request, _: bool = Auth):
    """The iOS Shortcut endpoint. Replies as soon as the request is understood.

    Guarded by the same dependency as the other ninety-three routes, and for
    the reason those exist: this one checked its credential by hand, and the
    hand-written version declared `key` and not `token`. So a shared link —
    the one you hand out, the one that carries a token rather than the key —
    could GET anything and POST nothing, which is a 403 on the only route
    Shortcuts uses. Nothing about the link was wrong; the door didn't have
    that keyhole.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("input") or body.get("q") or "").strip()
    if not text:
        form = await request.form()
        text = (form.get("input") or "").strip()
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    target = room.queue if room else None
    # Resolution + download happen on a worker; Siri gets an answer immediately.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: handle_request(text, queue=target, lists=_lists_for(request)))
    return JSONResponse(result)


@app.get("/remote", response_class=HTMLResponse)
async def remote_page(request: Request, key: str = Query(default=""),
                      token: str = Query(default="")):
    """Full remote for a phone: controls, search, queue, playlists."""
    return _serve_page(request, "remote.html", key, token)


@app.get("/player", response_class=HTMLResponse)
async def player_page(request: Request, key: str = Query(default=""),
                      token: str = Query(default="")):
    """The player, and the credential it gets to keep.

    This used to be unauthenticated *and* embed the key, which meant anyone
    who could load the page owned the server — every other check here was
    decoration. Now it needs a key or a token, and it hands back whichever
    one arrived: turn up with the key and the page can do everything, turn up
    on a shared link and it can listen and nothing else.
    """
    return _serve_page(request, "player.html", key, token)


def _serve_page(request: Request, name: str, key: str, token: str):
    """A page, and the credential it gets to keep.

    On the home network this behaves as it always did: open the address and
    it loads. From anywhere else it needs a key or a token, because the page
    embeds a credential and serving it to whoever asks handed the server away.

    What it embeds depends on how you arrived. The key, and the page can do
    everything; a shared link's token, and it can listen and nothing else.
    """
    ip = _client_ip(request)
    from .security import is_home

    # Someone who arrived holding a credential is judged on it, wherever they
    # are. Without this, a guest on a phone-only link who happens to be in the
    # house gets handed the master key by the open-LAN rule — including one
    # whose link you revoked an hour ago.
    offered = (token or key
               or request.headers.get("X-Music-Key")
               or request.headers.get("X-API-Key")
               # Somebody signed in is somebody in particular, and what they
               # may do is their account's business. Without this, a guest
               # account opening the page from the sofa would be handed the
               # owner's copy by the open-home rule.
               or request.cookies.get(SESSION_COOKIE, "") or "")
    home = is_home(ip) and config.get("lan_open", True) and not offered

    if not home:
        require_key(request, key, token)
    owner = home or is_owner(request, key)
    row = getattr(request.state, "pass_row", None) or {}
    creds = config.get("api_key", "") if owner else (token or key)
    signed_in = getattr(request.state, "account", None)
    if signed_in:
        # The cookie is the credential, and it goes with every request this
        # page makes on its own. Nothing has to be baked into the html —
        # which is the point of signing in rather than holding a link.
        creds = config.get("api_key", "") if owner else ""
    if not creds and not signed_in and row.get("id"):
        # Authenticated by header rather than by query string, so there's no
        # credential in the url to hand on. Rebuild the one that got them in —
        # otherwise the page loads and then can't call anything, which is a
        # stranger failure to debug than being turned away.
        creds = sec.reissue_token(config.get("api_key", ""), row["id"])
    # Who this is, decided here rather than a round trip later. The page used
    # to load neutral and ask, which left a window where its own requests went
    # out saying the wrong thing about where they should play — and left the
    # capsule showing whatever the markup happened to say.
    return templates.TemplateResponse(request, name, {
        "api_key": creds,
        # Which calls are reads, from the same table the server enforces, so
        # the pages can't drift from it.
        "api_read_only": sorted(f"/api/{k}" for k in READ_ONLY_ROUTES
                                if "{" not in k),
        "is_guest": "0" if owner else "1",
        "scope": "owner" if owner else (row.get("scope") or "full"),
        "announce_duck_db": config.get("announce_duck_db", -12.0),
        "announce_voice_gain_db": config.get("announce_voice_gain_db", 0.0),
        "signed_in_as": (signed_in or {}).get("email", ""),
        "signed_in_name": (signed_in or {}).get("name", ""),
        "signed_in_pic": (signed_in or {}).get("picture", ""),
        "sign_in_offered": "1" if (google.configured() and not signed_in
                                   and not owner) else "0",
    })


# ── health + events ───────────────────────────────────────────────────

@app.get("/api/ping")
async def ping():
    return {"status": "ok", "uptime": round(time.time() - _start, 1),
            "app": "music-request-server", "version": 2,
            "release": __version__}


def _sse(evt: dict) -> str:
    """One event as a wire frame, and never an exception.

    A payload json can't encode used to raise inside the generator, which
    breaks the stream for every client at once — and because sticky events
    are replayed to whoever reconnects, they'd all break again on the way
    back in. Anything odd gets str()'d instead.
    """
    try:
        body = json.dumps(evt, default=str)
    except Exception as exc:
        log.warning("undeliverable %s event: %s", evt.get("type"), exc)
        body = json.dumps({"type": evt.get("type") or "toast", "data": None})
    return "data: " + body + NEWLINE + NEWLINE


def _mine(evt: dict, session: str) -> bool:
    """Is this event for the listener on the other end of this stream?

    Events carry the session that produced them. Anything unstamped is the
    shared player or genuinely global — a library scan, a toast — and goes to
    everyone. Without this a guest would receive the owner's now-playing and
    the owner would receive theirs.
    """
    data = evt.get("data")
    stamped = data.get("session", "") if isinstance(data, dict) else ""
    return stamped == session


@app.get("/api/events")
async def events(request: Request, key: str = Query(default=""),
                 token: str = Query(default=""), here: str = Query(default="1")):
    require_key(request, key, token)
    row = getattr(request.state, "pass_row", None)
    # The player fetches itself a pass so <audio> and EventSource have
    # something to put in a URL. That pass is the owner's own page, not a
    # guest — treating it as one filtered every status event out of the
    # stream, and the page sat there looking broken with nothing playing.
    #
    # `here` is the capsule: a guest listening on their own phone wants their
    # session, and a guest who has switched to the computer's speakers wants
    # the shared player, because that is now what they're controlling. An
    # EventSource can't send a header, so this one thing rides in the url and
    # the page reconnects when the capsule moves.
    solo = bool(row) and not (row.get("internal") or row.get("owner")) \
        and (here == "1" or row.get("scope") == "phone")
    mine = row.get("id", "") if solo else ""
    queue = bus.subscribe()

    async def stream():
        try:
            yield _sse({"type": "hello"})
            # Whoever just connected needs the picture as it is, not only the
            # next change to it. Without this a client that arrives after the
            # state settles waits for something to happen before it learns
            # there is anything playing — which for a browser session means
            # it never starts, because nothing will happen until it does.
            from ..core.session import blank_status, sessions
            try:
                room = sessions.find(mine) if mine else None
                # A listener on their own device gets their own player even
                # when they haven't got one yet. Falling through to the
                # shared player here opened their page on the owner's
                # now-playing — and after their session had been let go,
                # that is exactly the moment they reconnect.
                first = (room.status() if room else
                         blank_status(mine) if mine else player.status())
                yield _sse({"type": "status", "data": first})
            except Exception as exc:
                log.debug("couldn't send the opening status: %s", exc)
            while True:
                if await request.is_disconnected():
                    break
                # An open stream is the connection. Nothing else a browser
                # does is reliable — it never says goodbye, and a phone that
                # walks out of range simply stops. This is what lets a
                # dropped guest be paused rather than played to an empty room.
                #
                # Looked up each time rather than held: the session may not
                # exist yet when the page first connects.
                if mine:
                    live = sessions.find(mine)
                    if live:
                        live.touch()
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                    if _mine(evt, mine):
                        yield _sse(evt)
                except asyncio.TimeoutError:
                    yield ": keepalive" + NEWLINE + NEWLINE
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive"})


@app.get("/api/backup")
def api_backup(_: bool = Owner):
    """Copy the profile into a zip next to it. Not served over HTTP: it has
    the api key in it, so it goes on disk where the Open folder button is."""
    from ..core.backup import make_backup
    try:
        return make_backup()
    except Exception as exc:
        log.warning("backup failed: %s", exc)
        return {"ok": False, "message": f"Backup failed: {exc}"}


@app.get("/api/restore")
def api_restore(path: str = "", _: bool = Owner):
    from ..core.backup import restore
    if not path:
        return {"ok": False, "message": "Give it the path to a backup zip"}
    return restore(path)


@app.get("/api/unskip")
def api_unskip(_: bool = Owner):
    """Bring back the last skipped track and unlearn the skip."""
    return player.queue.unskip()


@app.get("/api/health")
def health(_: bool = Owner):
    """What the outside services are actually doing.

    Every queue bug worth the name this year was one of these quietly
    returning nothing: MusicBrainz 503ing on the anchor and switching the era
    check off for a whole run, Deezer handing back a stub with no related
    artists, the affinity cache never being filled for the song everything is
    measured against. None of it was visible from inside the program.
    """
    from ..core.tags import tagstore
    from ..core.era import era
    from ..core.kin import kin
    from ..core.downloader import downloader
    from ..resolve import catalog as cat
    from ..core.gate import gate

    def safe(fn, fallback=None):
        try:
            return fn()
        except Exception as exc:
            return {"error": str(exc)[:120]} if fallback is None else fallback

    return {
        "tags": safe(tagstore.stats),
        "era": safe(era.stats),
        "kin": safe(kin.stats),
        "catalog": safe(cat.stats),
        "searches": safe(lambda: {"cached": len(cat._cache)}),
        "traffic": safe(gate.stats),
        "downloads": safe(downloader.cache_stats),
        "queue": safe(lambda: {
            "pool": len(player.queue._pool),
            "ready": player.queue.ready_ahead(),
            "minutes_ahead": round(player.queue.minutes_ahead(), 1),
        }),
        "log": str(log_path()),
    }


@app.get("/api/status")
def status(request: Request, _: bool = Auth):
    room = _session_for(request)
    if room:
        return {"status": "ok", **room.status(), "queue": room.queue.snapshot()}
    return {"status": "ok", **player.status(), "queue": player.queue.snapshot()}


# ── playback ──────────────────────────────────────────────────────────

@app.get("/api/play")
def api_play(request: Request, q: str = "", song: str = "", artist: str = "", mode: str = "play",
             source: str = "", cast: bool = False, _: bool = Auth):
    text = (q or song or "").strip()
    if artist and text:
        text = f"{text} by {artist}"
    elif artist:
        text = artist
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return handle_request(text, mode=mode, source=source or None, cast=cast,
                          queue=room.queue if room else None,
                          lists=_lists_for(request))


@app.get("/api/play/video/{video_id}")
def api_play_video(request: Request, video_id: str, title: str = "", artist: str = "", art: str = "",
                   mode: str = "play", _: bool = Auth):
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return play_video(video_id, title=title, artist=artist, art=art, mode=mode,
                      queue=room.queue if room else None)


@app.get("/api/control/{action}")
def api_control(request: Request, action: str, value: int | None = None,
                _: bool = Auth):
    room = _session_for(request)
    if not room:
        return {"status": "ok", **player.control(action, value)}
    # A browser session has no mpv to command: the phone is the transport, so
    # these only move the queue and the client follows.
    q, sink = room.queue, room.sink
    if action in ("next", "skip", "previous"):
        # Leaving a track early is the clearest thing anybody ever tells a
        # radio, and a guest's was going unheard: only /api/session/ended
        # recorded anything, and that fires when a track runs out. Somebody
        # who skips everything they're offered — which is what you do when
        # you're being offered the wrong thing — taught their profile nothing
        # at all, and its history stayed empty however long they listened.
        room.note_played(room.current(), room.position)
    if action in ("next", "skip"):
        sink.advance()
        room.rewound()          # the clock belonged to the track that just went
    elif action == "previous":
        pos = sink.pos() or 0
        sink.jump(max(0, pos - 1))
        room.rewound()
    elif action in ("playpause", "pause", "resume"):
        sink.set_paused(action == "pause" or
                        (action == "playpause" and not sink.paused))
    elif action == "shuffle":
        # A mode, not a one-shot. The owner's button has been a toggle for a
        # while; this one shuffled the queue once and left the light on it
        # lit by a setting it never wrote to, so it looked like a state and
        # behaved like a button. It is the guest's own preference, kept in
        # their profile, and it now decides how their albums and playlists
        # arrive as well as reordering what is already waiting.
        prof = getattr(room, "profile", None)
        on = not bool(prof.get("shuffle") if prof is not None else False)
        if prof is not None:
            prof.set("shuffle", on)
        if on:
            q.shuffle_upcoming()
    elif action == "like":
        # Into their own liked list. There is one now, so the heart works.
        track = room.current()
        if track:
            q.taste.toggle_like(track)
    q.publish_queue(force=True)
    return {"status": "ok", **room.status()}


@app.get("/api/session/ended")
def api_session_ended(request: Request, _: bool = Auth):
    """The phone finished a track and wants the next one.

    The client owns the clock — this is the only thing that advances a
    browser session, which is what makes it survive the phone sleeping.
    """
    room = _session_for(request)
    if not room:
        return {"status": "ok", "shared": True}
    # Their taste learns from what they actually sat through, the same way
    # the owner's does from mpv's monitor loop.
    room.note_played(room.current(), room.position)
    sec.note_use(room.id, plays=1)
    room.sink.advance()
    room.rewound()
    room.queue.publish_queue(force=True)
    return {"status": "ok", **room.status()}


@app.get("/api/session/progress")
def api_session_progress(request: Request, pos: float = 0.0, _: bool = Auth):
    """Where the phone has got to, so the owner's guest list can say.

    The browser is the clock here — there is no mpv to ask — so this is the
    only place the number ever comes from.
    """
    room = _session_for(request)
    if room:
        moved = room.mark_position(pos)
        room.touch()
        if moved:
            sec.note_use(room.id, seconds=moved)
    return {"status": "ok"}


@app.get("/api/session/here")
def api_session_here(request: Request, on: int = 1, at: float = 0.0,
                     carry: int = 1, _: bool = Auth):
    """The capsule moved. Carry the song across, and park what's left behind.

    Switching output used to change only where the *next* request went: the
    song you were listening to stayed where it was, so moving rooms meant
    searching for it again. This takes the track and the position with you.

    Looked up by pass rather than through _session_for, which answers with
    the shared player once "play here" is off — and pausing that would have
    stopped the music in the owner's front room.
    """
    from ..core.session import sessions
    row = getattr(request.state, "pass_row", None)
    if not row or row.get("internal") or row.get("owner"):
        return {"status": "ok", "shared": True}
    room = sessions.find(row["id"])
    moved = ""
    if carry:
        moved = _carry_over(room, bool(on), at)
    if room:
        room.sink.set_paused(not on)
        room.touch()
    return {"status": "ok", "session": room.id if room else "",
            "moved": moved, "paused": room.sink.paused if room else None}


def _carry_over(room, to_phone: bool, at: float) -> str:
    """Move what's playing between a guest's session and the speakers.

    Only the track and where they'd got to. Deliberately not the whole
    queue: a guest stepping onto the speakers should not wipe out the room's
    running order, and coming back off them should not drag it away.
    """
    try:
        if to_phone:
            track = player.queue.current_track()
            if not track or not room:
                return ""
            # adopt, not play_now: the file is already here, and going back
            # round the download path is the whole of "switching is slow".
            if not room.queue.adopt(track):
                room.queue.play_now([track])
            room.mark_position(max(0.0, at))
            player.control("pause")
            return f"{track.title} — {track.artist}"
        if not room:
            return ""
        track = room.current()
        if not track:
            return ""
        if not player.queue.adopt(track):
            player.queue.play_now([track])
        # mpv has the clock on this side, so it gets seeked once it's loaded.
        spawn(lambda: _seek_when_ready(track, at), name="handoff seek")
        return f"{track.title} — {track.artist}"
    except Exception as exc:
        log.warning("handoff failed: %s", exc)
        return ""


def _seek_when_ready(track, at: float, tries: int = 40) -> None:
    """mpv can't be told a position for a file it hasn't opened yet."""
    if at <= 1:
        return
    for _ in range(tries):
        time.sleep(0.25)
        cur = player.queue.current_track()
        if cur and cur.video_id == track.video_id:
            try:
                player.seek(at)
            except Exception as exc:
                log.debug("handoff seek: %s", exc)
            return


@app.get("/api/sessions")
def api_sessions(close: str = "", _: bool = Owner):
    """Who's listening, to what, and how hard they're asking."""
    from ..core.session import sessions
    if close:
        sessions.close(close)
    return {"status": "ok", "sessions": sessions.listing()}


@app.get("/api/seek")
def api_seek(request: Request, pos: float, _: bool = Auth):
    room = _session_for(request)
    if room:
        room.mark_position(pos)                # the phone does the seeking
        return {"status": "ok", "position": room.position}
    return {"status": "ok", **player.seek(pos)}


@app.get("/api/restart")
def api_restart(_: bool = Owner):
    player.restart()
    return {"status": "ok", "message": "Player restarted"}


# ── queue ─────────────────────────────────────────────────────────────

@app.get("/api/queue/{op}")
def api_queue(request: Request, op: str, index: int = 0, to: int = 0,
              frm: int = Query(default=-1, alias="from"), _: bool = Auth):
    """Reorder, remove, jump. Whosever queue is on the other end of this.

    These used to say player.queue outright, so a guest dragging a track in
    their own list reordered the owner's — from a phone that couldn't see it.
    """
    room = _session_for(request)
    q = room.queue if room else player.queue
    if op == "move":
        return {"status": "ok", "ok": q.move(frm if frm >= 0 else index, to)}
    if op == "remove":
        return {"status": "ok", "ok": q.remove(index)}
    if op == "jump":
        ok = q.jump(index)
        if room:
            room.rewound()
        return {"status": "ok", "ok": ok}
    if op == "undo":
        return {"status": "ok", "message": q.undo()}
    if op == "stats":
        return {"status": "ok", **q.stats()}
    raise HTTPException(404, "unknown queue operation")


@app.get("/api/cancel")
def api_cancel(request: Request, _: bool = Auth):
    """The X beside the progress bar."""
    room = _session_for(request)
    return {"status": "ok", **(room.queue if room else player.queue).cancel()}


@app.get("/api/radio")
def api_radio(request: Request, count: int = 8, _: bool = Auth):
    """More like this one — into whichever queue asked."""
    from ..core import radio as radio_mod

    room = _session_for(request)
    q = room.queue if room else player.queue
    track = q.current_track()
    # Not during a stream, and above all not before letting go of the hold.
    # Two things went wrong here and the second is the loud one. A station's
    # own track has no video id, so asking YouTube what resembles an empty
    # one returns an armful of whatever it felt like; and release_hold is
    # what permits the queue to start topping itself up, so pressing this on
    # air handed a stream that never ends a growing queue of strangers
    # behind it. Neither of them could ever play.
    if radio_mod.is_station(track):
        return {"status": "ok", "ok": False, "ignored": True,
                "message": "Radio has no next track to queue behind — ask "
                           "for the song by name and you'll get a queue"}
    if not track:
        return {"status": "ok", "ok": False, "message": "Nothing playing"}
    count = max(1, min(20, int(count)))
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    q.release_hold()
    similar = catalog.related(track.video_id, limit=count)
    q.enqueue(similar)
    return {"status": "ok", "ok": True, "added": len(similar),
            "message": f"Queued {len(similar)} more like this"}


# ── search / metadata ─────────────────────────────────────────────────

_ELSEWHERE = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sc-search")


def _elsewhere_start(q: str):
    """Ask SoundCloud, and don't wait around for the answer yet.

    It is a yt-dlp call — about two seconds, against milliseconds for the
    rest — so it must run *beside* the other lookups rather than after them.
    Started here and collected at the end, it costs whatever it has left over
    when everything else is done, which is usually nothing.
    """
    try:
        return _ELSEWHERE.submit(catalog.search_soundcloud, q, 4)
    except Exception as exc:
        log.debug("couldn't ask soundcloud: %s", exc)
        return None


def _elsewhere_collect(fut, seconds: float = 1.5) -> list[dict]:
    """Whatever arrived in time. A slow answer is dropped, never waited on:
    search staying quick matters more than search being complete, and the
    next keystroke asks again anyway."""
    if fut is None:
        return []
    try:
        return [t.to_dict() for t in fut.result(timeout=seconds)]
    except FuturesTimeout:
        log.debug("soundcloud was too slow to make the cut")
    except Exception as exc:
        log.debug("soundcloud search failed: %s", exc)
    return []


@app.get("/api/search")
def api_search(request: Request, q: str, limit: int = 12, _: bool = Auth):
    """Songs, artists, albums, your playlists and your own files.

    Your playlists come first — they're the things you made, so they should
    outrank anything YouTube suggests.
    """
    q = (q or "").strip()
    if not q:
        return {"status": "error", "message": "q required"}
    if len(q) > 160:
        raise HTTPException(400, "Search is limited to 160 characters")
    limit = max(1, min(20, int(limit)))
    _guard_rate(_session_for(request), request)
    if spotify.is_spotify_url(q):
        return {"status": "ok", "spotify": True, "results": [], "playlists": [],
                "artists": [], "albums": [], "library": [],
                "message": "Spotify link — press Enter to import it"}
    # Started before anything else, so its two seconds overlap the rest
    # instead of being added to them.
    elsewhere = _elsewhere_start(q)
    body = {
        "status": "ok",
        "playlists": [{"kind": "playlist", **p}
                      for p in playlists.summary()
                      if q.lower() in p["name"].lower()],
        "library": [t.to_dict() for t in library.search(q, limit=4)],
        "artists": catalog.search_artists(q, limit=2),
        "albums": catalog.search_albums(q, limit=2),
        "stations": [t.to_dict() for t in radio_mod.search(q, limit=3)],
        "results": [t.to_dict() for t in catalog.search_candidates(q, limit=limit)],
    }
    body["soundcloud"] = _elsewhere_collect(elsewhere)
    return body


@app.get("/api/play/artist")
def api_play_artist(request: Request, name: str, _: bool = Auth):
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return handle_request(f"songs by {name}", queue=room.queue if room else None,
                          lists=_lists_for(request))


@app.get("/api/play/album")
def api_play_album(request: Request, name: str, artist: str = "", _: bool = Auth):
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return handle_request(f"play the {name} album" + (f" by {artist}" if artist else ""),
                          queue=room.queue if room else None,
                          lists=_lists_for(request))


@app.get("/api/lyrics")
def api_lyrics(request: Request, _: bool = Auth):
    """Words to whatever the caller is listening to.

    This said player.queue outright, so it answered with the owner's track
    whoever asked — which for a listener on their own device meant the wrong
    song's words, or, far more often, "no lyrics for this one" because the
    computer's speakers weren't playing anything at all. It also meant a link
    could read what the owner was listening to.
    """
    from ..core import radio

    room = _session_for(request)
    # The song, not the station. On radio these are different things and
    # the words of a record called "BBC Radio 6 Music" do not exist.
    track = radio.on_air(room.current() if room else
                         player.queue.current_track())
    if not track:
        return {"status": "ok", "lyrics": None}
    if request.method == "GET":
        # Legacy GET support is deliberately cache-only.  A prefetch or a
        # browser retry must not create LRCLIB traffic or change cache state.
        data = lyrics_mod.cached_lyrics(track.title, track.artist, track.duration)
    else:
        # A lyrics refresh can issue three LRCLIB requests.  It is external
        # work just like a music request, so a shared credential cannot fan it
        # out.
        _guard_rate(room, request)
        data = lyrics_mod.get_lyrics(track.title, track.artist, track.duration)
    return {"status": "ok", "lyrics": data}


@app.get("/api/lyrics/search")
def api_lyrics_search(request: Request, q: str = "", _: bool = Auth):
    """Which song has these words in it.

    Search, not playback — the caller decides what to do with the answer, so
    a phone can offer the three candidates rather than committing to the
    first. `verified` says whether the words were actually found in that
    recording's lyrics or whether it is the model's guess unchecked.
    """
    q = (q or "").strip()
    if len(q) > 280:
        raise HTTPException(400, "Lyrics search is limited to 280 characters")
    # Fragments are rejected by hunt without network work.  Charge every
    # query that can reach the paid/remote lookup path.
    if len(q) >= 3:
        _guard_rate(_session_for(request), request)
    rows = lyrics_mod.hunt(q)
    return {"status": "ok", "query": q, "results": rows}


@app.get("/api/about")
def api_about(request: Request, wait: int = 0, _: bool = Auth):
    """Where this song came from, for whoever is listening to it.

    A POST with `wait=0` answers from what's already looked up and starts the
    lookup if it hasn't been — the panel opens instantly and fills itself a
    moment later rather than staring at a spinner for eight seconds. Legacy
    GET is cache-only and never starts an enrichment job.
    """
    from ..core import radio
    from ..resolve import insights

    room = _session_for(request)
    # Same again: a station's own track names the station, and the story
    # behind Radio 6 Music is not what anybody opened this panel for.
    track = radio.on_air(room.current() if room else
                         player.queue.current_track())
    if not track:
        return {"status": "ok", "about": None}
    mine = room.queue.taste if room else player.queue.taste
    if request.method == "GET":
        data = insights.about(track, taste=mine, fetch=False)
    else:
        # A waiting panel asks Last.fm, Wikipedia, and sometimes the LLM.
        # Charge the credential before it can start that work; warm() below
        # is charged too when this is a cache miss.
        if wait:
            _guard_rate(room, request)
        data = insights.about(track, taste=mine, fetch=bool(wait), enrich=True)
        if not data.get("ready"):
            if not wait:
                _guard_rate(room, request)
            insights.warm(track)
    return {"status": "ok", "about": data}


@app.get("/api/history")
def api_history(request: Request, _: bool = Auth):
    """What's been played here. Yours, and only yours.

    A shared link gets an empty list rather than a 403: the tab is hidden for
    them anyway, and this is a history of the owner's evenings, not a
    permission to argue about. Nothing a guest plays reaches it either — a
    session records into a neutral store that swallows writes.
    """
    if not _owner_view(request):
        # Their own, if they have one. A permanent link builds a history the
        # same way the owner does, and it's theirs to look at — what it must
        # never be is a window onto the owner's.
        me = _profile_for(request)
        if me is None or not me.permanent:
            return {"status": "ok", "history": [], "top_artists": [],
                    "mine": False}
        return {"status": "ok", "history": me.taste.recent(),
                "top_artists": me.taste.top_artists(), "mine": True,
                "whose": me.name}
    return {"status": "ok", "history": taste.recent(),
            "top_artists": taste.top_artists(), "mine": True}


@app.get("/api/block")
def api_block(request: Request, artist: str = "", on: int = 1,
              video_id: str = "",
              _: bool = Auth):
    """Never play this again — this recording, or this act at all.

    Whosever radio it is. A skip is a nudge the scoring weighs against
    everything else; this is an answer, and the ranker doesn't get a vote.
    Blocking the thing that is playing skips it, because being asked to
    stop hearing something and then hearing the rest of it is silly.
    """
    room = _session_for(request)
    me = _profile_for(request)
    store = (me.taste if me is not None else None) or (
        room.queue.taste if room else taste)
    if me is not None and not me.permanent:
        return {"status": "error",
                "message": "This link doesn't keep anything between songs"}
    track = (room.current() if room else player.queue.current_track())
    want = bool(on)
    if artist:
        done = store.block(artist=artist, on=want)
        label = artist
        undo = {"artist": artist} if want and done else None
    else:
        # Blocking immediately advances past the current song. A delayed undo
        # must name that recording, rather than affect whatever plays next.
        # An explicit ID is only accepted for unblocking; new blocks must
        # still describe the song actually playing.
        if video_id:
            if want:
                raise HTTPException(status_code=400,
                                    detail="A song can only be blocked while it is playing")
            from ..models import Track
            track = Track(video_id=video_id)
        if not track:
            return {"status": "error", "message": "Nothing playing to block"}
        done = store.block(track=track, on=want)
        label = track.title or "song"
        undo = ({"video_id": track.video_id} if want and done and
                track.video_id else None)
    if want and done and track and not artist:
        _skip_current(room)
    elif want and done and artist and track and store.is_blocked(track):
        _skip_current(room)
    return {"status": "ok", "blocked": want, "changed": done,
            "message": (f"Blocked {label}" if want else f"Unblocked {label}"),
            "undo": undo,
            **store.blocks()}


def _skip_current(room) -> None:
    """Move past whatever is playing, whosever player it is."""
    try:
        if room:
            room.sink.advance()
            room.rewound()
            room.queue.publish_queue(force=True)
        else:
            player.control("next")
    except Exception as exc:
        log.debug("couldn't skip the blocked track: %s", exc)


@app.get("/api/blocks")
def api_blocks(request: Request, _: bool = Auth):
    """What's been blocked, so the settings page can show and undo it."""
    me = _profile_for(request)
    room = _session_for(request)
    store = (me.taste if me is not None else None) or (
        room.queue.taste if room else taste)
    return {"status": "ok", **store.blocks()}


@app.get("/api/history/forget")
def api_history_forget(request: Request, video_id: str = "", artist: str = "",
                       _: bool = Auth):
    """Take a song or an artist out of your recents — and out of the radio.

    Whosever history it is. A permanent link edits its own; the owner edits
    the machine's; a link that expires has nothing to edit, and says so
    rather than quietly doing nothing.
    """
    if not video_id and not artist:
        return {"status": "error", "message": "Forget what?"}
    store = taste
    if not _owner_view(request):
        me = _profile_for(request)
        if me is None or not me.permanent:
            return {"status": "error",
                    "message": "This link doesn't keep a history"}
        store = me.taste
    gone = store.forget(video_id=video_id, artist=artist)
    return {"status": "ok" if gone else "error",
            "message": "Forgotten" if gone else "Nothing to forget",
            "history": store.recent(), "top_artists": store.top_artists()}


@app.get("/api/liked")
def api_liked(request: Request, _: bool = Auth):
    if not _owner_view(request):
        me = _profile_for(request)
        return {"status": "ok",
                "liked": me.taste.liked() if me and me.permanent else []}
    return {"status": "ok", "liked": taste.liked()}


def _owner_view(request: Request) -> bool:
    """Is this the owner's own listening, or somebody holding a link?

    A full-access guest on the computer's speakers is still a guest: they're
    driving the owner's player, which is theirs to drive, but the history and
    the preferences behind it are not theirs to read.
    """
    row = getattr(request.state, "pass_row", None)
    return not row or bool(row.get("internal") or row.get("owner"))


# ── playlists ─────────────────────────────────────────────────────────

def _lists_for(request: Request):
    """Whose playlists these are.

    The owner's, or a permanent link's own. A link that expires gets None:
    saving a list to a credential that dies at midnight is a promise the
    thing can't keep, and it's better to say so than to lose it quietly.
    """
    me = _profile_for(request)
    if me is None:
        return playlists
    return me.lists          # None when the link isn't permanent


def _smart_store(request: Request):
    """The listening history these dynamic lists are allowed to use."""
    if _owner_view(request):
        return taste
    me = _profile_for(request)
    return me.taste if me is not None and me.permanent else None


def _smart_rows(store, kind: str, limit: int = 100):
    """Build a live playlist from one listener's own saved listening data."""
    from ..models import Track

    liked = store.liked() or []
    recent = store.recent(200) or []
    metadata: dict[str, dict] = {}
    for row in [*recent, *liked]:
        if isinstance(row, dict) and isinstance(row.get("video_id"), str):
            metadata.setdefault(row["video_id"], row)

    if kind == "liked":
        source = liked
    elif kind == "recent":
        source = recent
    elif kind == "most_played":
        try:
            raw_counts = store.play_counts() or {}
        except Exception:
            raw_counts = {}
        counts = {}
        if isinstance(raw_counts, dict):
            for video_id, value in raw_counts.items():
                if not isinstance(video_id, str):
                    continue
                try:
                    counts[video_id] = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
        source = [metadata[vid] for vid, _ in sorted(
            counts.items(), key=lambda item: (-item[1], item[0]))
                  if vid in metadata]
    else:
        raise HTTPException(400, "Unknown smart playlist")

    rows = []
    seen: set[str] = set()
    for row in source:
        try:
            if not isinstance(row, dict):
                continue
            track = Track.from_dict(row)
            if (not isinstance(track.video_id, str) or not track.video_id.strip()
                    or not isinstance(track.title, str) or not track.title.strip()
                    or track.video_id in seen or store.is_blocked(track)):
                continue
            seen.add(track.video_id)
            rows.append(track.to_dict())
            if len(rows) >= max(1, min(200, int(limit))):
                break
        except (AttributeError, TypeError, ValueError):
            # One corrupt historical row should not make the whole dynamic
            # playlist unusable.
            continue
    return rows


_SMART_LABELS = {
    "liked": "Liked songs",
    "recent": "Recently played",
    "most_played": "Most played",
}


@app.get("/api/smartplaylists")
def api_smartplaylists(request: Request, kind: str = "", _: bool = Auth):
    """Read live playlists derived from this listener's own history."""
    store = _smart_store(request)
    if kind:
        if kind not in _SMART_LABELS:
            raise HTTPException(400, "Unknown smart playlist")
        rows = _smart_rows(store, kind) if store is not None else []
        return {"status": "ok", "kind": kind,
                "name": _SMART_LABELS[kind], "tracks": rows}
    return {"status": "ok", "playlists": [
        {"kind": name, "name": label,
         "count": len(_smart_rows(store, name)) if store is not None else 0}
        for name, label in _SMART_LABELS.items()]}


@app.get("/api/smartplaylists/play")
def api_smartplaylist_play(request: Request, kind: str, _: bool = Auth):
    """Add up to fifty current smart-list tracks to the caller's queue."""
    if kind not in _SMART_LABELS:
        raise HTTPException(400, "Unknown smart playlist")
    store = _smart_store(request)
    if store is None:
        raise HTTPException(403, "Smart playlists need a permanent link")
    from ..models import Track
    tracks = [Track.from_dict(row) for row in _smart_rows(store, kind, 50)]
    if not tracks:
        return {"status": "ok", "added": 0,
                "message": f"{_SMART_LABELS[kind]} is empty"}
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    queue = room.queue if room else player.queue
    queue.enqueue(tracks, imported=True)
    return {"status": "ok", "added": len(tracks),
            "message": f"Added {len(tracks)} from {_SMART_LABELS[kind]}"}


@app.get("/api/playlists")
def api_playlists(request: Request, _: bool = Auth):
    mine = _lists_for(request)
    # Lists the house shares. One copy, the owner's, and everybody sees the
    # same rows in it — including a link that expires, which has nowhere to
    # keep a list of its own but can still put a song in the shared one.
    house = ([r for r in playlists.summary() if r.get("shared")]
             if mine is not playlists else [])
    if mine is None:
        return {"status": "ok", "playlists": [], "folder": "",
                "download": False, "temporary": True, "shared": house,
                "message": "Playlists need a permanent link"}
    return {"status": "ok", "playlists": mine.summary(), "shared": house,
            "folder": str(mine.root()) if mine is playlists else "",
            "download": bool(config.get("playlist_download"))}


@app.get("/api/station")
def api_station(request: Request, url: str = "", name: str = "", art: str = "", _: bool = Auth):
    """Tune a live radio station."""
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    if not radio_mod.is_known_stream(url):
        raise HTTPException(400, "Choose a station from the search results")
    return play_station(url, name, art, queue=room.queue if room else None)


@app.get("/api/foryou")
def api_foryou(request: Request, _: bool = Auth):
    """A queue built from what you actually ask for."""
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return play_for_you(announce=False, queue=room.queue if room else None)


@app.get("/api/spectrum")
def api_spectrum(_: bool = Auth):
    """The visualiser envelope for whatever is playing, base64'd.

    Read off the file itself, so the meter follows mpv and not whatever else
    the machine happens to be playing.
    """
    import base64

    from ..core import spectrum as spec

    track = player.queue.current_track()
    path = player.mpv.get("path", "") or (track.path if track else "")
    if not path:
        return {"status": "ok", "ready": False}
    data = spec.cached(path)
    if data is None:
        spec.ensure(path)
        return {"status": "ok", "ready": False}
    return {"status": "ok", "ready": True, "fps": spec.FPS,
            "bands": len(spec.BANDS),
            "data": base64.b64encode(data).decode("ascii")}


@app.get("/api/spotify/add")
def api_spotify_add(request: Request, url: str = "", _: bool = Auth):
    """Save a Spotify link as a playlist without hijacking what's playing.

    Into the caller's own library. A guest pasting a link here used to file
    it in the owner's collection, which is both a surprise and a mess.
    """
    mine = _lists_for(request)
    if mine is None:
        raise HTTPException(
            403, "Saving a list needs a permanent link — this one expires")
    room = _session_for(request)
    return add_spotify(url, queue=room.queue if room else None,
                       room=room.id if room else "", lists=mine)


def _whoami(request: Request) -> str:
    """The name on the link, for signing what you add to a shared list."""
    row = getattr(request.state, "pass_row", None)
    if not row or row.get("internal") or row.get("owner"):
        return ""
    return (row.get("name") or "guest").strip()


@app.get("/api/playlist/{op}")
def api_playlist(request: Request, op: str, name: str = "",
                 shuffle: bool = False, video_id: str = "", title: str = "",
                 artist: str = "", art: str = "", start: int = 0,
                 shared: bool = False, on: int = 1,
                 _: bool = Auth):
    """Make and play lists — the caller's own, not always the owner's.

    `shared=1` names a list in the owner's library that has been opened to
    the house. There is one copy of it, so everyone is looking at the same
    list and adding to the same list; who added what is recorded so the
    rows can say so, and so somebody can take back their own.
    """
    who = _whoami(request)
    mine = _lists_for(request)
    if shared:
        # The flag is the permission. A guest naming any other list of the
        # owner's gets the same answer as if it weren't there.
        if not playlists.is_shared(name):
            raise HTTPException(403, "That list isn't shared")
        if op in ("delete", "download", "share", "create"):
            raise HTTPException(403, "That's the owner's to do")
        mine = playlists
    elif mine is None:
        raise HTTPException(
            403, "Playlists need a permanent link — this one expires")
    room = _session_for(request)

    def changed():
        room_id = room.id if room else ""
        bus.publish(Ev.SETTINGS, {"playlists": True, "session": room_id}
                    if room_id else {"playlists": True})

    if op == "create":
        mine.create(name)
        changed()
        return {"status": "ok", "message": f"Created {name}"}
    if op == "share":
        if _profile_for(request) is not None:
            raise HTTPException(403, "That's the owner's to do")
        got = playlists.set_shared(name, bool(on))
        changed()
        return {"status": "ok", **got}
    if op == "add":
        from ..models import Track as _T
        if video_id:
            got = mine.add(
                name, _T(video_id=video_id, title=title, artist=artist, art=art),
                by=who)
        elif room:
            # "add what's on" has to mean what's on *their* player.
            cur = room.current()
            if not cur:
                return {"status": "ok", "ok": False, "message": "Nothing playing"}
            got = mine.add(name, cur, by=who)
        else:
            got = player.playlist_add_current(name)
        changed()
        return {"status": "ok", **got}
    if op == "remove":
        # On a shared list you can take back what you put in. Everything
        # else in it is somebody else's, and the owner's list is the
        # owner's to prune.
        if shared and who and playlists.credit(name).get(video_id) != who:
            raise HTTPException(403, "You can only take out what you put in")
        got = mine.remove(name, video_id)
        changed()
        return {"status": "ok", **got}
    if op == "delete":
        got = mine.delete(name)
        changed()
        return {"status": "ok", **got}
    if op == "play":
        if room:
            tracks = list(mine.tracks(name))
            if not tracks:
                return {"status": "ok", "ok": False, "message": "That list is empty"}
            room.queue.play_now(tracks, shuffle=shuffle, hold_radio=True,
                                kind="playlist")
            return {"status": "ok", "ok": True,
                    "message": f"Playing {name}"}
        return {"status": "ok", **player.playlist_play(name, shuffle, start)}
    if op == "download":
        if room:
            raise HTTPException(403, "Keeping lists on disk is the computer's")
        mine.download_async(name)
        return {"status": "ok", "message": f"Saving {name} offline"}
    if op == "tracks":
        by = mine.credit(name)
        rows = []
        for t in mine.tracks(name):
            row = t.to_dict()
            row["added_by"] = by.get(t.video_id, "")
            rows.append(row)
        return {"status": "ok", "tracks": rows,
                "shared": mine.is_shared(name), "me": who}
    raise HTTPException(404, "unknown playlist operation")


# ── settings ──────────────────────────────────────────────────────────

# What a guest's page legitimately needs to render itself. Everything else —
# library paths, allowed addresses, which browser holds the cookies, where
# Tailscale used to live — is the owner's business and none of theirs.
#
# Deliberately not crossfade, volume, repeat or shuffle: those are the
# owner's playback preferences, they belong to the owner's player, and a
# guest's session neither reads nor obeys them. Sending them only invited a
# guest's page to display settings that don't apply to it.
_GUEST_SETTINGS = ("theme", "show_visualiser", "eq_presets",
                   "party_mode", "block_full_guests")

# What each outside tool is actually for, in words that mean something to
# somebody who has just unzipped this and doesn't know what yt-dlp is.
_TOOL_INFO = (
    ("mpv", True, "Plays the audio. Without it nothing makes a sound."),
    ("yt-dlp", True, "Fetches the audio from YouTube. Without it there's "
                     "nothing to play."),
    ("node", False, "YouTube's player needs a JavaScript engine to hand over "
                    "audio formats. Without it every download comes back empty."),
)


@app.get("/api/settings")
def api_settings(request: Request, _: bool = Auth):
    full = player.settings()
    if not is_owner(request):
        # A shared link shouldn't be handed the whole configuration just
        # because the page it loads happens to read from here. What it gets
        # instead is its own: the handful of things a guest chooses, at
        # whatever they've set them to.
        from ..core.profile import GUEST_SETTINGS
        me = _profile_for(request)
        mine = me.all() if me else dict(GUEST_SETTINGS)
        return {"status": "ok", "guest": True,
                "settable": sorted(GUEST_SETTINGS),
                "persistent": bool(me and me.permanent),
                "eq_presets": full.get("eq_presets", []),
                **{k: full[k] for k in _GUEST_SETTINGS if k in full},
                **mine}
    return {"status": "ok", **full, "release": __version__,
            "groq": llm.status(), "cookies": dict(cookie_mod.state),
            "start_before_signin": bool(config.get("start_before_signin")),
            "spotdl": spotify.available()}


@app.get("/api/audio")
def api_audio(eq: str = "", normalize: int | None = None,
              crossfade: int | None = None, _: bool = Owner):
    changed = bool(eq) or normalize is not None or crossfade is not None
    if eq:
        config.set("eq", eq)
    if normalize is not None:
        config.set("normalize", bool(normalize))
    if crossfade is not None:
        config.set("crossfade", max(0, min(12, int(crossfade))))
    if changed:
        player.audio.apply()
        bus.publish(Ev.SETTINGS, player.settings())
    return {"status": "ok", "eq": config.get("eq"),
            "normalize": config.get("normalize"),
            "crossfade": config.get("crossfade")}


# Settings the UI is allowed to change, with how to coerce them.
_SETTABLE = {
    "artist_cohesion": float, "anchor_pull": float, "show_visualiser": bool, "queue_target": int, "queue_min_ready": int,
    "artist_run_limit": int, "min_duration": int, "dedupe_hours": int,
    "cookie_close_browser_optin": bool, "cookie_auto_refresh": bool,
    "playlist_download": bool, "queue_max": int, "artist_track_count": int,
    "cast_all": bool, "queue_minutes": int,
    "cookie_check_interval": int, "completion_ratio": float,
    "announce": bool, "tts_voice": str, "download_workers": int,
    "announce_duck_db": float, "announce_voice_gain_db": float,
    "allow_legacy_get_mutations": bool, "audit_log_days": int,
    "library_monitor_minutes": int,
    "device_eq_enabled": bool, "device_eq_auto": bool,
    "google_client_id": str, "google_client_secret": str, "owner_email": str,
    "new_account_scope": str, "server_name": str,
    "tailscale": str, "tailscale_exe": str, "cache_size_mb": int,
    "allow_key_in_url": bool, "port": int,
    "block_full_guests": bool, "lan_open": bool, "party_mode": bool,
    "ddns_provider": str, "ddns_hostname": str, "ddns_user": str,
    "max_downloads": int, "guest_requests_hour": int,
    "cast_queue_minutes": int, "guest_quiet_pause": int, "guest_quiet_close": int,
    "auto_volume": bool, "evening_hour": int, "quiet_hour": int,
    "wake_hour": int, "evening_level": int, "quiet_level": int,
}


@app.get("/api/setting")
def api_setting(request: Request, key: str, value: str = "", _: bool = Auth):
    """Change a setting. Whose depends on who's asking.

    A guest writes into their own profile and can only reach the short list
    that is theirs — how their queue behaves and how it sounds on their
    device. Everything about the machine still needs the key.
    """
    me = _profile_for(request)
    if me is not None:
        from ..core.profile import GUEST_SETTINGS
        if key not in GUEST_SETTINGS:
            raise HTTPException(403, f"{key} is the computer's, not yours")
        got = me.set(key, value)
        if got is None:
            raise HTTPException(400, f"bad value for {key}")
        # Only stamped onto their own stream — nobody else's page should
        # redraw because somebody changed their own crossfade.
        bus.publish(Ev.SETTINGS, {"session": me.id, **me.all()})
        return {"status": "ok", "key": key, "value": got,
                "kept": me.permanent}

    if not is_owner(request):
        raise HTTPException(403, "That needs the key, not a shared link")
    caster_type = _SETTABLE.get(key)
    if caster_type is None:
        raise HTTPException(400, f"{key} isn't settable from here")
    try:
        if caster_type is bool:
            parsed = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            parsed = caster_type(value)
    except Exception:
        raise HTTPException(400, f"bad value for {key}")
    if caster_type is float and not math.isfinite(parsed):
        raise HTTPException(400, f"bad value for {key}")
    if key == "announce_duck_db":
        parsed = max(-60.0, min(0.0, float(parsed)))
    elif key == "announce_voice_gain_db":
        parsed = max(-24.0, min(12.0, float(parsed)))
    elif key == "audit_log_days":
        parsed = max(1, min(365, int(parsed)))
    elif key == "library_monitor_minutes":
        parsed = 0 if int(parsed) <= 0 else max(5, min(10080, int(parsed)))
    elif key == "port" and not 1025 <= int(parsed) <= 65535:
        raise HTTPException(400, "port must be between 1025 and 65535")
    elif key == "new_account_scope" and parsed not in accounts.NEW_ACCOUNT_SCOPES:
        raise HTTPException(400, "new accounts may be full, phone, or blocked")
    config.set(key, parsed)
    if key == "library_monitor_minutes" and parsed:
        library.start_monitor()
    if key in ("device_eq_enabled", "device_eq_auto"):
        if key == "device_eq_auto" and parsed:
            autoeq.settle(autoeq.output_name())     # match what's plugged in now
        player.audio.apply()
    bus.publish(Ev.SETTINGS, player.settings())
    return {"status": "ok", "key": key, "value": parsed}


@app.get("/api/audio/devices")
def api_audio_devices(_: bool = Owner):
    """Output devices mpv can see, plus which one we're using."""
    return {"status": "ok", **player.audio_devices()}


def _session_for(request: Request):
    """The queue this caller is acting on.

    Owner, or a guest with "play here" off: the shared player. A guest
    playing on their own device: their own session, created the first time
    they ask for anything.
    """
    from ..core.profile import profiles
    from ..core.session import sessions
    row = getattr(request.state, "pass_row", None)
    if not row or row.get("internal") or row.get("owner"):
        return None                       # the owner's, i.e. the shared one
    here = request.headers.get("X-Play-Here", "") == "1"
    if row.get("scope") == "phone" or here:
        return sessions.for_pass(row["id"], row.get("name", ""),
                                 row.get("scope", "full"),
                                 profiles.for_row(row))
    return None


def _profile_for(request: Request):
    """This caller's profile, or None if they're the owner.

    A permanent link is a person and gets a folder; one that expires is an
    evening and gets defaults it can change for as long as it lasts.
    """
    from ..core.profile import profiles
    row = getattr(request.state, "pass_row", None)
    if not row or row.get("internal") or row.get("owner"):
        return None
    return profiles.for_row(row)


_shared_rate_lock = threading.Lock()
_shared_rate: dict[str, list[float]] = {}


def _guard_rate(room, request: Request | None = None) -> None:
    """One guest can't spend everyone's evening, and it goes on their tab.

    Counted per pass rather than per address, because the pass is the person
    — moving to mobile data shouldn't reset anybody's allowance. Charged
    whether it played here or out of the computer's speakers: what the owner
    wants to know is what a link has been used for, not where it came out.
    """
    cap = int(config.get("guest_requests_hour", 40))
    if room:
        if cap and room.queue.recent_requests() >= cap:
            left = 60 - int((time.time() - room.queue.oldest_request()) / 60)
            raise HTTPException(
                429, f"That's {cap} songs in an hour — try again in "
                     f"{max(1, left)} minutes")
    row = getattr(request.state, "pass_row", None) if request is not None else None
    if row and not (row.get("internal") or row.get("owner")):
        # Shared-player callers do not have a personal QueueManager, so the
        # queue's hourly history cannot protect them. Keep the same bounded
        # one-hour window by pass id in-process instead of silently treating
        # the counter below as a limiter.
        if not room and cap:
            now = time.monotonic()
            with _shared_rate_lock:
                recent = [at for at in _shared_rate.get(row["id"], [])
                          if now - at < 3600]
                if len(recent) >= cap:
                    raise HTTPException(
                        429, f"That's {cap} requests in an hour — try again later")
                recent.append(now)
                _shared_rate[row["id"]] = recent
                if len(_shared_rate) > 512:
                    for pid in [pid for pid, times in _shared_rate.items()
                                if not any(now - at < 3600 for at in times)]:
                        _shared_rate.pop(pid, None)
        sec.note_use(row["id"], requests=1, ip=_client_ip(request))
    else:
        stats.note(stats.HOUSE, requests=1)


def _guard_shared(request: Request) -> None:
    """The owner's toggle: while it's on, full guests keep off the speakers.

    Only applies to the shared player. Someone listening on their own phone
    is not in the room this protects, so they are never refused.
    """
    row = getattr(request.state, "pass_row", None)
    if not row or not config.get("block_full_guests"):
        return
    raise HTTPException(
        status_code=403,
        detail="The speakers are in use — switch on \"Play on this device\" "
               "to listen on your own phone")


def _pass_scope(request: Request) -> str:
    """"" for the owner, otherwise the scope of the pass that got them in."""
    row = getattr(request.state, "pass_row", None)
    return (row or {}).get("scope", "") if row else ""


@app.get("/api/audio/device")
def api_audio_device(request: Request, name: str = "auto", client: str = "",
                     _: bool = Owner):
    """Which PC speaker the shared player uses.

    A phone-scoped guest has no business here — refused outright rather than
    merely hidden, because a hidden control is only hidden until somebody
    types the url in themselves.
    """
    if _pass_scope(request) == "phone" and name != CAST_DEVICE:
        raise HTTPException(403, "That link plays on your own device only")
    return {"status": "ok", **player.set_audio_device(name, client=client)}


# ── the first-run guide ───────────────────────────────────────────────

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request, key: str = Query(default=""),
                       token: str = Query(default="")):
    """What all this is, before you're asked to decide anything about it."""
    return _serve_page(request, "welcome.html", key, token)


@app.get("/api/setup/state")
def api_setup_state(_: bool = Owner):
    """Everything the guide needs to say where you're up to.

    One call rather than six, because every one of these is a status dot on
    the same screen and they should never be able to disagree with each other.
    """
    from ..core import net
    from ..server import missing_tools

    gone = missing_tools()
    ck = dict(cookie_mod.state)
    return {
        "status": "ok",
        "done": bool(config.get("setup_done")),
        "tools": [{"name": name, "have": name not in gone, "needed": fatal,
                   "why": why}
                  for name, fatal, why in _TOOL_INFO],
        "missing": gone,
        "cookies": {"ok": ck.get("ok"), "source": ck.get("source", ""),
                    "message": ck.get("message", ""),
                    "checking": bool(ck.get("checking"))},
        "groq": bool(config.get("groq_api_key")),
        "lastfm": bool(config.get("lastfm_session")),
        "spotify": spotify.available(),
        "announce": bool(config.get("announce", True)),
        "key_set": bool(config.get("api_key")),
        "port": net.live_port(),
        "release": __version__,
    }


@app.get("/api/setup/tools")
def api_setup_tools(_: bool = Owner):
    """Fetch whatever's missing, and nothing else.

    Runs the same repair path the app uses when it starts and finds a gap —
    a visible console, only the named tools, no cookie dance and no config
    rewrite for something that isn't broken.
    """
    from ..server import missing_tools, repair, setup_script

    gone = missing_tools()
    if not gone:
        return {"status": "ok", "installed": [], "missing": [],
                "message": "Everything's already here"}
    if not setup_script():
        return {"status": "error",
                "message": "setup.ps1 isn't next to the app — install "
                           + ", ".join(gone) + " by hand"}
    repair(gone)
    still = missing_tools()
    got = [g for g in gone if g not in still]
    return {"status": "ok", "installed": got, "missing": still,
            "message": ("Installed " + ", ".join(got) if got else
                        "Nothing installed — see setup-log.txt")
                       + (f". Still missing: {', '.join(still)}" if still else "")}


@app.get("/api/setup/done")
def api_setup_done(done: int = 1, _: bool = Owner):
    """Stop opening by itself. Reopenable from Settings whenever."""
    config.set("setup_done", bool(done))
    return {"status": "ok", "done": bool(done)}


@app.get("/api/whoami")
def api_whoami(request: Request, _: bool = Auth):
    """What this client may do, so the page can shape itself to it."""
    row = getattr(request.state, "pass_row", None)
    if row and (row.get("internal") or row.get("owner")):
        row = None                        # the owner's own page
    owner = not row
    return {"status": "ok", "owner": owner,
            "scope": "owner" if owner else row.get("scope", "full"),
            "name": "" if owner else row.get("name", ""),
            "pass_id": "" if owner else row.get("id", ""),
            "expires": 0 if owner else row.get("expires", 0),
            "block_full_guests": bool(config.get("block_full_guests"))}


# ── casting the audio to whichever browser is asking ─────────────────────

# Written down rather than asked of `mimetypes`, which on Windows reads the
# registry. This machine's registry says .m4a is "audio/m4a" -- not a
# registered type -- and .aac is "audio/vnd.dlna.adts"; the answer changes
# with whatever software last claimed the extension. The old code hard-coded
# audio/mp4 for m4a, which is correct, and .m4a is now the usual cast format.
_AUDIO_TYPES = {
    ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


def _media_type(path: Path) -> str:
    """The standard type for an audio file, independent of the machine."""
    return (_AUDIO_TYPES.get(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream")


def _note_served(request: Request, sent: int) -> None:
    """Bytes that left this machine, against whoever asked for them."""
    if sent <= 0:
        return
    row = getattr(request.state, "pass_row", None) or {}
    who = row.get("id") if row.get("id") and not (row.get("internal")
                                                  or row.get("owner")) else stats.HOUSE
    stats.note(who, bytes_out=sent)


def _range_response(request: Request, path: Path):
    """Serve one completed file with correct single-range semantics.

    Multi-range requests are deliberately answered with the complete file;
    that is allowed when a server does not implement multipart ranges and is
    more interoperable than pretending a partial prefix is a valid container.
    """
    size = path.stat().st_size
    media = _media_type(path)
    common = {"Accept-Ranges": "bytes",
              "Cache-Control": "private, max-age=3600"}
    raw = (request.headers.get("range") or "").strip()
    if not raw or not raw.lower().startswith("bytes="):
        _note_served(request, size)
        return FileResponse(path, media_type=media, headers=common)
    if size <= 0:
        return Response(status_code=416,
                        headers={**common, "Content-Range": "bytes */0"})

    spec = raw[6:]
    if "," in spec:
        # We do not implement multipart/byteranges; ignoring Range and
        # returning 200 is explicitly preferable to a malformed 206.
        _note_served(request, size)
        return FileResponse(path, media_type=media, headers=common)
    first, sep, last = spec.partition("-")
    if not sep:
        return Response(status_code=416,
                        headers={**common, "Content-Range": f"bytes */{size}"})
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(first)
            end = size - 1 if not last else int(last)
            if start < 0 or start >= size or end < start:
                raise ValueError
            end = min(end, size - 1)
    except (TypeError, ValueError):
        return Response(status_code=416,
                        headers={**common, "Content-Range": f"bytes */{size}"})

    length = end - start + 1

    def body():
        sent = 0
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    sent += len(chunk)
                    yield chunk
        finally:
            # What actually went out. A phone that changes its mind halfway
            # through a song did not stream the other half.
            _note_served(request, sent)

    return StreamingResponse(
        body(), status_code=206, media_type=media,
        headers={**common, "Content-Range": f"bytes {start}-{end}/{size}",
                 "Content-Length": str(length)})


@app.get("/api/output/stream/{video_id}")
def api_output_stream(request: Request, video_id: str, tune: str = "",
                      fmt: str = "", _: bool = Auth):
    """The track mpv is playing, as bytes a phone will accept.

    FileResponse handles Range itself, which is what gives the phone a
    draggable timeline instead of a take-it-or-leave-it download. `tune`
    names the speaker it's playing out of (cast.TUNES); `fmt` is what the
    browser said it plays (cast.FORMATS).
    """
    # A caller can skip the preload endpoint entirely.  Charge the first
    # range request only when it is about to start real conversion work;
    # follow-up byte ranges see "converting" or "ready" and remain free.
    _path, initial = cast_mod.playable(video_id, tune, fmt)
    if initial == "needs conversion":
        _guard_rate(_session_for(request), request)
    path, state = cast_mod.serve(video_id, tune, fmt)
    if state in ("partial", "arriving"):
        return JSONResponse({"status": "arriving", "detail": "still fetching"},
                            status_code=503, headers={"Retry-After": "1"})
    if state == "missing":
        # Not "no such track" — almost always "the download hasn't finished".
        # A 404 tells the player to give up; a 503 tells it to come back, and
        # coming back is right, because it will be here shortly.
        return JSONResponse({"status": "not ready", "detail": "still fetching"},
                            status_code=503, headers={"Retry-After": "3"})
    if state != "ready" or not path:
        # 503 rather than an error: the client retries, it isn't broken.
        return JSONResponse({"status": "converting", "detail": state},
                            status_code=503)
    return _range_response(request, Path(path))


@app.get("/api/output/prepare/{video_id}")
def api_output_prepare(request: Request, video_id: str, tune: str = "", fmt: str = "",
                       _: bool = Auth):
    """Warm the next track so the handover isn't audible."""
    # Only the file that will be asked for. Warming the untuned one as well
    # ran two encodes side by side for a fallback a warmed track never needs.
    room = _session_for(request)
    _guard_rate(room, request)
    _, state = cast_mod.playable(video_id, tune, fmt)
    if state == "needs conversion":
        state = "converting" if cast_mod.warm(video_id, tune, fmt) else "busy"
    return {"status": "ok", "state": state}


@app.get("/api/autoeq/search")
def api_autoeq_search(request: Request, q: str = "", _: bool = Auth):
    """Headphone models AutoEq has a correction for. Anyone with a link: it's
    how a phone picks its own headphones."""
    q = (q or "").strip()[:80]
    if len(q) >= 2:
        _guard_rate(_session_for(request), request)
    return {"status": "ok", "results": autoeq.search(q)}


@app.get("/api/autoeq/profile")
def api_autoeq_profile(request: Request, id: str = "", _: bool = Auth):
    """One model's correction, fetched once and kept. The phone asks for this
    before asking for a stream tuned with it."""
    if not id or len(id) > 40:
        raise HTTPException(404, "no such AutoEq entry")
    if request.method == "GET":
        # Compatibility mode still permits old GET callers, but a GET must
        # never turn into a GitHub request or a persistent cache write.
        e = autoeq.cached_entry(id)
        prof = autoeq.profile(id, fetch=False)
        if not e or not prof:
            return JSONResponse({"status": "not_ready",
                                 "detail": "Use POST to fetch this profile"},
                                status_code=409)
        return {"status": "ok", **autoeq.public(e), "tune": f"aeq-{e['id']}",
                "preamp": prof["preamp"], "filters": len(prof["filters"]),
                "curve": autoeq.curve(prof)}
    _guard_rate(_session_for(request), request)
    e = autoeq.entry(id)
    if not e:
        raise HTTPException(404, "no such AutoEq entry")
    prof = autoeq.profile(id)
    if not prof:
        return JSONResponse({"status": "unavailable",
                             "detail": "couldn't fetch that profile from AutoEq"},
                            status_code=503)
    return {"status": "ok", **autoeq.public(e), "tune": f"aeq-{e['id']}",
            "preamp": prof["preamp"], "filters": len(prof["filters"]),
            "curve": autoeq.curve(prof)}


@app.get("/api/autoeq/match")
def api_autoeq_match(request: Request, name: str = "", _: bool = Auth):
    """A device label from a browser, looked up the same way Windows names are."""
    name = (name or "").strip()[:120]
    if name:
        _guard_rate(_session_for(request), request)
    found, certain = autoeq.match(name)
    return {"status": "ok", "match": autoeq.public(found) if found else None,
            "certain": certain}


@app.get("/api/autoeq/status")
def api_autoeq_status(_: bool = Owner):
    """What the PC is playing through and the correction on it."""
    return {"status": "ok", **autoeq.status()}


@app.get("/api/autoeq/assign")
def api_autoeq_assign(id: str = "", clear: int = 0, _: bool = Owner):
    """Choose the model for the output in use. id "" means none for this one;
    clear forgets the choice, so a certain name match can apply again."""
    name = autoeq.output_name()
    if not name:
        raise HTTPException(409, "not playing through a PC output right now")
    if clear:
        rows = dict(config.get("device_eq") or {})
        rows.pop(name, None)
        config.set("device_eq", rows)
        autoeq.settle(name)
    else:
        if id and not autoeq.profile(id):
            return JSONResponse({"status": "unavailable",
                                 "detail": "couldn't fetch that profile from AutoEq"},
                                status_code=503)
        try:
            autoeq.assign(name, id)
        except ValueError:
            raise HTTPException(404, "no such AutoEq entry")
    autoeq.refresh_now(player._output_changed)
    return {"status": "ok", **autoeq.status()}


@app.get("/api/output/stats")
def api_output_stats(_: bool = Auth):
    return {"status": "ok", "casting": player.casting(),
            "ao": player.current_ao(), "alt_ao": player.current_ao(alt=True),
            "media_controls": player.mpv.get("media-controls", None),
            **cast_mod.stats()}


@app.get("/api/announce/{aid}.mp3")
def api_announce_file(aid: str, _: bool = Auth):
    """The spoken track name, for the browser acting as the speaker."""
    path = player.announce_file(aid)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "that clip has gone")
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/token")
def api_token(hours: int = 12, _: bool = Owner):
    """The player's own pass, for the URLs a header can't reach.

    Not a shared link — this is the page fetching something to put in
    <audio>.src and EventSource so the key itself never rides in a URL.
    """
    key = config.get("api_key") or ""
    hours = _pass_hours(hours, permanent=False)
    got = sec.issue(key, name="this player", hours=hours, scope="full",
                    internal=True)
    if not got.get("token"):
        raise HTTPException(503, "Couldn't save the player credential")
    return {"status": "ok", "token": got.get("token", ""),
            "expires_in": int(hours) * 3600}


def _pass_hours(value: float, *, permanent: bool = True) -> float:
    """Validate a pass lifetime before it reaches timestamp arithmetic."""
    try:
        hours = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "hours must be a number")
    if (not math.isfinite(hours) or hours < 0 or hours > sec.MAX_LINK_HOURS
            or (not permanent and hours <= 0)):
        raise HTTPException(
            400, f"hours must be {'between 1 and' if not permanent else 'between 0 and'} "
            f"{sec.MAX_LINK_HOURS:g}")
    return hours


@app.get("/api/passes")
def api_passes(_: bool = Owner):
    """Every link you've handed out, and who has it."""
    from ..core.session import sessions

    # Whether anyone is on the other end, asked of the sessions rather than
    # guessed from how recently the link was touched. A timestamp can only
    # say "not long ago", so a link went on claiming somebody was listening
    # for minutes after their session had been ended or had timed out.
    live = {r["id"] for r in sessions.listing() if r["active"]}
    rows = sec.list_passes()
    for row in rows:
        row["listening"] = row["id"] in live
    return {"status": "ok", "passes": rows}


@app.get("/api/passes/new")
def api_pass_new(name: str = "", hours: float = 24, scope: str = "full",
                 kind: str = "wan", _: bool = Owner):
    """Mint a named link for somebody.

    hours=0 makes it permanent. scope "phone" means they can play it on
    their own phone and nowhere else — handy when you'd rather a guest
    couldn't take over the speakers in your front room.
    """
    from ..core import net
    hours = _pass_hours(hours)
    key = config.get("api_key") or ""
    if not key:
        return {"status": "error", "message": "Set an API key first"}
    got = sec.issue(key, name=name, hours=hours, scope=scope)
    if not got.get("token"):
        raise HTTPException(503, "Couldn't save that link")
    rows = net.addresses(got["token"])["addresses"]
    row = next((r for r in rows if r["kind"] == kind), rows[-1])
    return {"status": "ok", "url": row["url"], "kind": row["kind"], **got}


@app.get("/api/profiles")
def api_profiles(_: bool = Owner):
    """Who has a profile, and a little about them.

    Their top artists and how much they've played — the fun bit — and
    nothing that amounts to reading over their shoulder. Not what they
    played last night, not their history, not their queue.
    """
    from ..core.profile import profiles
    return {"status": "ok", "profiles": profiles.listing()}


@app.get("/api/passes/extend")
def api_pass_extend(id: str = "", hours: float = 24, _: bool = Owner):
    """Give a link more time — including one that has already lapsed.

    The link the person already has starts working again. Nothing new needs
    sending, because the registry decides when a pass dies rather than the
    date signed into the token; see read_token.
    """
    if not id:
        return {"status": "error", "message": "Which one?"}
    hours = _pass_hours(hours)
    got = sec.extend(id, hours)
    return {"status": "ok" if got.get("ok") else "error",
            **got, "passes": sec.list_passes()}


@app.get("/api/passes/revoke")
def api_pass_revoke(id: str = "", restore: int = 0, forget: int = 0,
                    wipe: int = 0, _: bool = Owner):
    """Ban a link by name, or let it back in.

    `wipe` also deletes what that link had built up — its settings, its
    taste, its playlists. Off by default: banning a link is usually "stop
    this working", not "erase the person", and the two shouldn't be the
    same button.
    """
    from ..core.session import sessions

    if not id:
        return {"status": "error", "message": "Which one?"}
    if forget:
        if wipe:
            from ..core.profile import profiles
            profiles.wipe(id)
        ok = sec.forget_pass(id)
    elif restore:
        ok = sec.restore_pass(id)
    else:
        ok = sec.revoke(id)
    if ok and not restore:
        # End it here rather than leaving it to the reaper. "That link stops
        # working now" was true of the next request and not of the music
        # already playing on it, which carried on for up to five seconds
        # while the list still showed them listening.
        sessions.close(id, "revoked")
    return {"status": "ok" if ok else "error", "passes": sec.list_passes()}


@app.get("/api/lockdown")
def api_lockdown(port: int = 1, _: bool = Owner):
    """Everything you handed out, taken back, in one press.

    Bans every pass, ends the sessions playing on them, and moves to a new
    port. Deliberately one call: doing it link by link while somebody is
    already inside is the wrong shape for the moment you'd want this.

    Your own pass goes too — it's a link like any other, and the whole point
    is that nothing you've shared survives. A new one is minted next time the
    settings page asks for an address.
    """
    from ..core.session import sessions
    killed = 0
    for row in sec.list_passes():
        if not row["revoked"]:
            sec.revoke(row["id"])
            killed += 1
    killed += sec.revoke_owner_pass()
    ended = sum(1 for r in sessions.listing()
                if sessions.close(r["id"], "revoked"))
    moved = 0
    if port:
        from .security import random_port
        moved = random_port()
        config.set("port", moved)
    log.warning("lockdown: %d links revoked, %d sessions ended, port -> %s",
                killed, ended, moved or "unchanged")
    return {"status": "ok", "revoked": killed, "ended": ended, "port": moved,
            "message": (f"{killed} link{'s' if killed != 1 else ''} revoked"
                        + (f", {ended} session{'s' if ended != 1 else ''} ended"
                           if ended else "")
                        + (f" — port {moved} after a restart" if moved else ""))}


@app.get("/api/port/shuffle")
def api_port_shuffle(to: int = 0, _: bool = Owner):
    """Move to a port nothing scans by habit. Takes effect on restart.

    Takes an explicit port too, so a wrong turn can be undone — a shuffled
    port that a forward rule no longer matches is otherwise a puzzle rather
    than a setting.
    """
    from .security import random_port
    port = int(to) if 1024 < int(to) < 65536 else random_port()
    config.set("port", port)
    return {"status": "ok", "port": port,
            "message": f"Port {port} after a restart — update your forward rule"}


@app.get("/api/blocked")
def api_blocked(forgive: str = "", clear: int = 0, _: bool = Owner):
    """Who's been shut out, and letting them back in."""
    if clear:
        return {"status": "ok", "forgiven": bans.forgive(), "blocked": []}
    if forgive:
        return {"status": "ok", "forgiven": bans.forgive(forgive),
                "blocked": bans.listing()}
    return {"status": "ok", "blocked": bans.listing()}


@app.get("/api/ddns")
def api_ddns(hostname: str = "", user: str = "", secret: str = "",
             provider: str = "", now: int = 0, _: bool = Owner):
    """Set up, or kick, the thing that keeps a hostname pointed here.

    The password is written straight to config and never read back — the UI
    only ever learns whether one is set.
    """
    from ..core import ddns
    if provider:
        config.set("ddns_provider", provider if provider in ddns.PROVIDERS else "dynu")
    if hostname:
        config.set("ddns_hostname", hostname.strip())
    if user:
        config.set("ddns_user", user.strip())
    if secret:
        config.set("ddns_password", secret)
    if hostname or user or secret or now:
        got = ddns.update(force=True)
        if ddns.configured():
            ddns.start()
        return {"status": "ok", **got}
    return {"status": "ok", **ddns.status()}


@app.get("/api/network")
def api_network(pass_id: str = "", check: int = 0, _: bool = Owner):
    """Which addresses reach this player.

    Pass an id and every address comes back carrying that pass, which is
    what makes a copied link work for the person you send it to.
    """
    from ..core import net
    key = config.get("api_key") or ""
    # No particular person asked for: these are the owner's own addresses, so
    # they carry the owner's pass and work on a phone as well as here.
    token = (sec.reissue_token(key, pass_id) if pass_id else sec.owner_pass(key))
    if not token:
        raise HTTPException(503, "Couldn't save the owner credential")
    out = net.addresses(token)
    if check:
        out["port_open"] = net.port_open(net.live_port())
    from ..server import cert_days_left
    out["cert_days"] = round(cert_days_left(), 1) if out.get("https") else 0
    return {"status": "ok", **out}


@app.get("/api/qr")
def api_qr(kind: str = "lan", pass_id: str = "", _: bool = Owner):
    """A scannable code for one of our own addresses.

    Takes a kind, not a url: an endpoint that renders any string handed to it
    is a QR generator for whoever finds it, and these carry the api key.

    Owner only. It was Auth, and the body hands out credentials: with no
    pass_id it renders the owner's own pass, and with one it reissues that
    pass's token. So any shared link could read the owner's credential out
    of an SVG, or re-mint anybody else's link by id — every other scope in
    the app was decoration while this was open. The only screens that call
    it are the owner's Sharing tab and the link they just made.
    """
    from ..core import net
    key = config.get("api_key") or ""
    token = (sec.reissue_token(key, pass_id) if pass_id else sec.owner_pass(key))
    if not token:
        raise HTTPException(503, "Couldn't save the owner credential")
    rows = net.addresses(token)["addresses"]
    row = next((r for r in rows if r["kind"] == kind), None)
    if not row:
        raise HTTPException(404, "no such address")
    return Response(net.qr_svg(row["url"]), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/cache")
def api_cache(prune: int = 0, _: bool = Owner):
    """How much downloaded music is on disk, and optionally trim it now."""
    removed = 0
    if prune:
        removed = downloader.prune_cache(keep=player.queue.keep_paths())
    return {"status": "ok", "removed": removed, **downloader.cache_stats()}


@app.get("/api/theme")
def api_theme(value: str = "default", _: bool = Owner):
    config.set("theme", value or "default")
    return {"status": "ok", "theme": config.get("theme")}


@app.get("/api/announce")
def api_announce(enabled: int = 1, _: bool = Owner):
    config.set("announce", bool(enabled))
    return {"status": "ok", "announce": config.get("announce")}


@app.get("/api/sleep")
def api_sleep(minutes: int = 0, _: bool = Owner):
    return {"status": "ok", **player.set_sleep(minutes)}


@app.get("/api/download")
def api_download(_: bool = Owner):
    return {"status": "ok", **player.export_current()}


@app.get("/api/pin")
def api_pin(_: bool = Owner):
    return {"status": "ok", **player.pin_current()}


@app.get("/api/source")
def api_source(request: Request, value: str = "youtube", _: bool = Auth):
    """Where this listener's songs come from.

    The row is marked guestok and this has always accepted a link's token,
    and it wrote to the machine's config — so a guest picking SoundCloud on
    their own phone moved the whole house onto SoundCloud, and their own
    profile's setting, which is the one their requests are resolved against,
    was never written at all. Whoever asked, it is theirs.
    """
    want = (value or "youtube").lower()
    me = None if is_owner(request) else _profile_for(request)
    if me is not None:
        me.set("source", want)           # coerce() rejects anything silly
        return {"status": "ok", "source": me.get("source"), "mine": True}
    config.set("source", want)
    return {"status": "ok", "source": config.get("source")}


@app.get("/api/lockips")
def api_lockips(enabled: int = 0, _: bool = Owner):
    config.set("lock_ips", bool(enabled))
    return {"status": "ok", "lock_ips": config.get("lock_ips")}


@app.get("/api/groqkey")
def api_groqkey(value: str = "", _: bool = Owner):
    config.update({"groq_api_key": value.strip(), "use_groq": bool(value.strip())})
    working = llm.ensure_model() if value.strip() else False
    return {"status": "ok", "groq": llm.available(), "working": working,
            "model": config.get("groq_model"), "detail": llm.status()}


@app.get("/api/groqmodels")
def api_groqmodels(request: Request, refresh: int = 0, _: bool = Owner):
    """What Groq will serve, without letting legacy GETs contact Groq."""
    models = (llm.cached_models() if request.method == "GET"
              else llm.models(force=bool(refresh)))
    return {"status": "ok", "models": models,
            "current": config.get("groq_model") or llm.DEFAULT_MODEL,
            "default": llm.DEFAULT_MODEL}


@app.get("/api/groqmodel")
def api_groqmodel(value: str = "", _: bool = Owner):
    """Pick the model. Tested before it's kept, so a bad choice can't quietly
    turn request parsing off — that failure looks exactly like a bad key."""
    want = (value or "").strip()
    if not want:
        return {"status": "error", "detail": "no model given"}
    if not llm.test(want):
        return {"status": "error", "working": False,
                "current": config.get("groq_model"),
                "detail": llm.status().get("last_error") or "model wouldn't answer"}
    config.set("groq_model", want)
    return {"status": "ok", "working": True, "current": want,
            "detail": llm.status()}


@app.get("/api/boot")
def api_boot(enabled: int = 0, _: bool = Owner):
    ok = _set_run_at_boot(bool(enabled))
    config.set("start_on_boot", bool(enabled))
    return {"status": "ok", "start_on_boot": bool(enabled), "applied": ok}


def _set_run_at_boot(enable: bool) -> bool:
    import sys
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    name = "MusicRequestServer"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --hidden'
                else:
                    root = Path(__file__).resolve().parents[3]
                    pyw = sys.executable.replace("python.exe", "pythonw.exe")
                    cmd = f'"{pyw}" "{root / "launcher.pyw"}" --hidden'
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        log.warning("boot registry write failed: %s", exc)
        return False


TASK_NAME = "MusicRequestServer-BeforeSignIn"


def _headless_command() -> str:
    import sys
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --headless'
    root = Path(__file__).resolve().parents[3]
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{root / "launcher.pyw"}" --headless'


def _run_ps(script: str) -> tuple[bool, str]:
    import subprocess
    try:
        got = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, str(exc)
    out = (got.stdout or "").strip() or (got.stderr or "").strip()
    return got.returncode == 0, out


def _run_ps_elevated(script: str) -> tuple[bool, str]:
    """Same, but through UAC.

    A task that runs when nobody is signed in is a privileged thing to
    register — Windows answers a plain Register-ScheduledTask with "Access is
    denied" however ordinary the program asking. So this raises the prompt
    once, and the answer is read back from Windows afterwards rather than
    from an exit code the elevated process can't easily hand back.

    A script file, run in a window you can see. It used to be base64 in
    -EncodedCommand with -WindowStyle Hidden, which is a reasonable way to
    survive quoting and an unreasonable thing to do on somebody's computer:
    Windows Defender scores exactly that shape as Trojan:Win32/Commando.A
    and kills it. Which means the one moment this program asks for
    administrator — registering the task that starts it before sign-in —
    was being blocked by the antivirus, silently, leaving a task that was
    half registered or not registered at all. "I don't trust the on-boot to
    work" was a correct read of it.

    So: a plain .ps1 in the temp directory, and a visible window. Nothing
    encoded, nothing hidden, and the file is there to be read afterwards if
    anyone wants to know what was run as administrator.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    folder = Path(tempfile.mkdtemp(prefix="mrs-setup-"))
    path = folder / "register-boot-task.ps1"
    try:
        path.write_text(script, encoding="utf-8")
    except Exception as exc:
        return False, str(exc)
    try:
        got = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command",
             "Start-Process powershell -Verb RunAs -Wait -ArgumentList "
             "'-NoProfile','-ExecutionPolicy','Bypass','-File',"
             f"'{path}'"],
            capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, str(exc)
    finally:
        # Left behind on failure, on purpose: if the task didn't register,
        # the script that tried is the most useful thing to be able to look at.
        try:
            if got.returncode == 0:
                path.unlink(missing_ok=True)
                folder.rmdir()
        except Exception:
            pass
    out = (got.stderr or "").strip() or (got.stdout or "").strip()
    if got.returncode != 0:
        log.warning("elevated setup failed (script kept at %s): %s", path, out)
    return got.returncode == 0, out


def _set_run_before_signin(enable: bool) -> tuple[bool, str]:
    """A scheduled task that starts the server with the machine.

    As the user, not SYSTEM. SYSTEM has its own profile, so it would look for
    the config, the links and the cookies in a folder that has none of them
    and quietly come up as a stranger's first run.

    S4U, so no password has to be stored anywhere for it. The cost is that
    the task gets no network credentials, which matters not at all to a
    server that only listens.
    """
    import getpass
    import os as _os

    if enable:
        cmd = _headless_command()
        exe, _, args = cmd.partition('" ')
        exe = exe.strip('"')
        # Whose account, decided here rather than inside the elevated shell —
        # elevating can land in a different user, and a task registered
        # against the wrong one looks in the wrong profile for everything.
        domain = _os.environ.get("USERDOMAIN") or _os.environ.get("COMPUTERNAME") or ""
        user = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
        script = f"""
$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args.strip()}'
$t = New-ScheduledTaskTrigger -AtStartup
# Half a minute of grace. The task fires the moment Windows will let it,
# which is before the network has an address — and the app now retries on
# its own, but not starting into a broken machine is cheaper than
# recovering from one.
$t.Delay = 'PT30S'
$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
# A server is meant to still be running tomorrow. Left at the default the
# task is killed after three days, which is the sort of thing you discover
# by finding the music off on a Thursday.
$s.MultipleInstances = 2          # IgnoreNew: never two of these at once
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t `
        -Principal $p -Settings $s -Force | Out-Null
"""
    else:
        script = (f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
                  f"-Confirm:$false -ErrorAction SilentlyContinue")
    _run_ps_elevated(script)
    # Ask Windows what actually happened. The elevated shell is a separate
    # process behind a prompt the user can decline, so its exit code says
    # nothing useful about whether the task is there.
    there = _before_signin_installed()
    ok = there if enable else not there
    if not ok:
        log.warning("couldn't %s the before-sign-in task",
                    "install" if enable else "remove")
    return ok, "" if ok else (
        "Windows refused, or the permission prompt was declined")


def boot_state() -> dict:
    """Everything about whether this will actually come back after a reboot.

    Written because "is the checkbox ticked" and "will it start" are not the
    same question, and the gap between them is where this feature has lived.
    A task can be registered and disabled, or registered against an exe that
    has since moved, or have failed its last run — and the settings page
    reported all three as a tick.
    """
    import os as _os
    import sys as _sys
    import winreg

    exe = _headless_command().partition('" ')[0].strip('"')
    # Comparing paths only means something in a build. Run from source the
    # command is the interpreter, so every registration correctly points
    # somewhere else and saying so is crying wolf.
    packaged = bool(getattr(_sys, "frozen", False))
    out = {"exe": exe, "exe_exists": _os.path.isfile(exe),
           "packaged": packaged, "warnings": []}

    # Sign-in: a plain Run entry.
    want = _os.path.abspath(exe).lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            value, _ = winreg.QueryValueEx(k, "MusicRequestServer")
        out["at_signin"] = True
        out["signin_command"] = value
        out["signin_matches"] = want in (value or "").lower()
    except FileNotFoundError:
        out["at_signin"] = False
        out["signin_matches"] = False
    except Exception as exc:
        out["at_signin"] = None
        out["signin_matches"] = None
        out["warnings"].append(f"couldn't read the sign-in entry: {exc}")

    # Before sign-in: the scheduled task, asked in detail.
    ok, raw = _run_ps(
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction "
        f"SilentlyContinue; if (-not $t) {{ 'none' }} else {{ "
        f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'; "
        f"@($t.State, $t.Settings.Enabled, $i.LastTaskResult, "
        f"$i.LastRunTime, $t.Actions[0].Execute) -join '|' }}")
    out["task"] = None
    if ok and raw.strip() and raw.strip() != "none":
        bits = (raw.strip().split("|") + [""] * 5)[:5]
        try:
            result = int(bits[2] or 0)
        except ValueError:
            result = 0
        out["task"] = {
            "state": bits[0], "enabled": bits[1].strip().lower() == "true",
            "last_result": result, "last_result_hex": f"0x{result & 0xFFFFFFFF:X}",
            "last_run": bits[3], "exe": bits[4],
            "exe_matches": want in (bits[4] or "").lower(),
        }
        t = out["task"]
        if not t["enabled"] or t["state"] == "Disabled":
            out["warnings"].append("the before-sign-in task is registered but "
                                   "disabled")
        if packaged and not t["exe_matches"]:
            out["warnings"].append("the task points at a different copy of "
                                   f"the program: {t['exe']}")
        if not _os.path.isfile(t["exe"] or ""):
            out["warnings"].append("the task points at a program that isn't "
                                   f"there any more: {t['exe']}")
        # 0x41306 is "terminated", which is what a clean handover used to
        # look like. Worth naming rather than showing as a raw number.
        if result not in (0, 267009, 267014):
            out["warnings"].append(
                f"its last run ended with {t['last_result_hex']}")
    elif config.get("start_before_signin"):
        # A status report must never rewrite the user's preference. The task
        # may have been removed deliberately or be temporarily unavailable;
        # reporting drift lets the owner decide whether to recreate it.
        if ok:
            out["warnings"].append("start-before-sign-in is enabled but its "
                                   "Windows task is missing")
        else:
            out["warnings"].append("couldn't check the before-sign-in task "
                                   "just now")

    if config.get("start_on_boot") and out.get("at_signin") is False:
        out["warnings"].append("start-at-sign-in is enabled but its Windows "
                               "entry is missing")
    if packaged and out["at_signin"] and not out.get("signin_matches", True):
        out["warnings"].append("the sign-in entry points at a different copy: "
                               + str(out.get("signin_command"))[:120])
    if not out["exe_exists"]:
        out["warnings"].append(f"the program isn't where boot expects it: {exe}")
    out["ok"] = not out["warnings"]
    return out


@app.get("/api/boot/status")
def api_boot_status(_: bool = Owner):
    """Whether this will really come back after a restart."""
    return {"status": "ok", **boot_state()}


def _before_signin_installed() -> bool:
    ok, out = _run_ps(
        f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' "
        f"-ErrorAction SilentlyContinue) {{'yes'}} else {{'no'}}")
    return ok and out.strip() == "yes"


@app.get("/api/boot/early")
def api_boot_early(enabled: int = 0, _: bool = Owner):
    """Start with the machine rather than with the desktop.

    Worth being plain about what this can and can't do: before anyone signs
    in there is no audio device for Windows to give us, so the computer's own
    speakers stay silent until you log in. Links play on their own devices
    and work exactly as they always do — which is the point of it.
    """
    want = bool(enabled)
    ok, out = _set_run_before_signin(want)
    config.set("start_before_signin", want and ok)
    if want and ok and not config.get("start_on_boot"):
        # The two go together. The early copy has no desktop, which means no
        # tray and no sound out of this machine; what makes that acceptable
        # is that signing in starts the ordinary copy, which takes the port
        # off it. Without that second half you sign in to a player that has
        # no icon and plays to nobody, which is precisely the state this was
        # meant to avoid.
        if _set_run_at_boot(True):
            config.set("start_on_boot", True)
            log.info("turned on start-at-sign-in too, so something takes over")
    return {"status": "ok" if ok else "error",
            "start_before_signin": want and ok,
            "start_on_boot": bool(config.get("start_on_boot")),
            "message": ("Serving from startup. Signing in hands it over to "
                        "the normal player — until then this computer's own "
                        "speakers stay silent"
                        if want and ok else
                        "Back to starting when you sign in" if ok else
                        f"Windows wouldn't take the task: {out[:200]}")}


# ── cookies ───────────────────────────────────────────────────────────

@app.get("/api/cookies")
def api_cookies(_: bool = Owner):
    return {"status": "ok", **cookie_mod.state,
            "path": str(cookie_mod.cookie_path()),
            "file": cookie_mod.inspect(),
            "browsers": cookie_mod.installed_browsers()}


@app.get("/api/cookies/find")
def api_cookies_find(close: int = 0, _: bool = Owner):
    """Test the cookies; only closes a browser if explicitly asked (close=1)."""
    return {"status": "ok", **cookie_mod.find_now(close_browsers=bool(close)),
            "path": str(cookie_mod.cookie_path())}


@app.get("/api/cookies/extension")
def api_cookies_extension(_: bool = Owner):
    """Chrome can't be decrypted, so use the export extension instead: this
    tells the user what to install and watches Downloads for the result."""
    return {"status": "ok", **cookie_mod.extension_flow()}


@app.get("/api/cookies/signedin")
def api_cookies_signedin(saved: int = 0, _: bool = Owner):
    """The sign-in window finished. Say so, rather than just vanishing.

    Checks, and only checks. This used to call find_now(), which on a failed
    check goes rummaging through the browsers and writes whatever it finds
    over the master file — so a successful sign-in could be overwritten by
    the very thing it exists to work around, seconds after it happened.
    """
    ok, why = cookie_mod.check()
    cookie_mod.state.update(ok=ok, checked_at=time.time(),
                            message="ok" if ok else why,
                            source="signed in" if ok else cookie_mod.state.get("source", ""))
    if saved and ok:
        bus.publish(Ev.TOAST, f"Signed in — {saved} cookies saved")
    elif saved:
        # Distinguish "these cookies are no good" from "something else is
        # wrong": the two have completely different fixes, and calling a
        # player-client problem a cookie problem sends you round in circles.
        bus.publish(Ev.TOAST, f"Saved {saved} cookies — but {why}")
    else:
        bus.publish(Ev.TOAST, "Sign-in finished without any YouTube cookies")
    return {"status": "ok", "ok": ok, "message": why, "saved": saved}


@app.get("/api/cookies/import")
def api_cookies_import(path: str = "", _: bool = Owner):
    """Import a cookies.txt the user points at (or the newest in Downloads)."""
    from pathlib import Path as _P
    src = _P(path) if path else cookie_mod.scan_downloads(max_age=86400)
    if not src or not _P(src).is_file():
        return {"status": "error", "ok": False,
                "message": "No cookies file found to import"}
    return {"status": "ok", **cookie_mod.import_file(_P(src))}


@app.get("/api/cookies/grab")
def api_cookies_grab(browser: str, close: int = 0, _: bool = Owner):
    """Opt-in: wait for a browser to close (or close it) and take its cookies."""
    import threading
    if close:
        if not config.get("cookie_close_browser_optin"):
            return {"status": "error",
                    "message": "Enable 'let me close your browser' in settings first"}
        cookie_mod.close_browser(browser)
    spawn(cookie_mod.grab_after_close, browser, name="cookie grab")
    return {"status": "ok",
            "message": f"Watching for {browser} to close, then grabbing cookies"}


# ── library ───────────────────────────────────────────────────────────

@app.get("/api/openfolder")
def api_open_folder(_: bool = Owner):
    """Open the data folder in Explorer."""
    import subprocess
    from ..paths import data_dir
    try:
        subprocess.Popen(["explorer", str(data_dir())])
        return {"status": "ok", "message": f"Opened {data_dir()}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/library/scan")
def api_library_scan(_: bool = Owner):
    library.scan_async()
    return {"status": "ok", "message": "Scanning your library"}


@app.get("/api/library/paths")
def api_library_paths(add: str = "", remove: str = "", _: bool = Owner):
    paths = list(config.get("library_paths") or [])
    changed = False
    if add and add not in paths:
        paths.append(add)
        changed = True
    if remove and remove in paths:
        paths.remove(remove)
        changed = True
    if changed:
        config.set("library_paths", paths)
        if paths and config.get("library_monitor_minutes"):
            library.start_monitor()
    return {"status": "ok", "paths": paths, "count": library.count()}


# ── last.fm / alarms / cast ───────────────────────────────────────────

@app.get("/api/lastfm")
def api_lastfm(step: str = "", api_key: str = "", secret: str = "", _: bool = Owner):
    if api_key:
        config.set("lastfm_api_key", api_key.strip())
    if secret:
        config.set("lastfm_secret", secret.strip())
    if step == "begin":
        return {"status": "ok", **scrobbler.begin_auth()}
    if step == "finish":
        return {"status": "ok", **scrobbler.complete_auth()}
    return {"status": "ok", **scrobbler.status()}


@app.get("/api/alarms")
def api_alarms(add: str = "", time_: str = Query(default="", alias="time"),
               days: str = "", remove: int = -1, _: bool = Owner):
    alarms = list(config.get("alarms") or [])
    changed = False
    if remove >= 0 and remove < len(alarms):
        alarms.pop(remove)
        changed = True
    if add and time_:
        alarms.append({"query": add, "time": time_, "enabled": True,
                       "days": [int(d) for d in days.split(",") if d.strip().isdigit()]})
        changed = True
    if changed:
        config.set("alarms", alarms)
    return {"status": "ok", "alarms": alarms}


@app.get("/api/cast")
def api_cast(add: str = "", remove: str = "", text: str = "", _: bool = Owner):
    peers = list(config.get("cast_peers") or [])
    changed = False
    if add and add not in peers:
        peers.append(add)
        changed = True
    if remove and remove in peers:
        peers.remove(remove)
        changed = True
    if changed:
        config.set("cast_peers", peers)
    sent = caster.broadcast(text) if text else []
    return {"status": "ok", "peers": peers, "sent": sent}


@app.get("/api/stream/{video_id}")
def api_stream(video_id: str, _: bool = Owner):
    """Serve a cached file so a peer can play the exact same audio."""
    path = downloader.cached(video_id)
    if not path:
        raise HTTPException(404, "not cached")
    return FileResponse(path)


# ── diagnostics ───────────────────────────────────────────────────────

@app.get("/auth/google/start")
def auth_google_start(request: Request, next: str = "/player"):
    """Begin a sign-in. Open to anyone -- a front door is meant to be knocked
    on, and no link or key is needed to prove who you are to Google.

    What a newcomer becomes is decided when they come back: a face already
    known keeps its account, and a new one lands at new_account_scope, which
    the owner sets -- to "blocked" if they would rather look people over
    before letting them in. Ownership is never granted here; only the
    configured owner email can be an owner.

    The rate limit stays: a public endpoint that mints server-side state is
    something to keep a lid on.
    """
    if not google.configured():
        raise HTTPException(503, "Signing in with Google isn't set up here")
    try:
        url = google.start(next_path=_safe_next(next),
                           client_ip=_client_ip(request))
    except google.SignInBusy as exc:
        raise HTTPException(429, str(exc))
    if not url:
        raise HTTPException(503, "No hostname set, so Google has nowhere to "
                                 "send anybody back to")
    return RedirectResponse(url, status_code=302)


def _safe_next(path: str) -> str:
    """Only ever back into this server, and only to a page."""
    path = (path or "/player").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/player"
    return path[:120]


@app.get("/auth/google/callback")
def auth_google_callback(request: Request, code: str = "", state: str = "",
                         error: str = ""):
    """Google sending somebody back. Everything here is checked, not trusted."""
    if error:
        return _signin_page("Google says: " + error[:120])
    row = google.pending(state)
    if not row:
        # Also what an old tab, a refresh of this url, or somebody else's
        # forged link looks like.
        return _signin_page("That sign-in had already been used or has "
                            "expired. Open the link again.")
    who = google.finish(code, row)
    if not who:
        return _signin_page("Google couldn't confirm who that was.")
    try:
        person = accounts.admit(
            who["sub"], who["email"], who["name"],
            picture=who.get("picture", ""),
            invited_by=str(row.get("invited_by") or ""),
            scope=str(row.get("scope") or ""))
    except accounts.AccountPersistenceError:
        # Do not issue a valid session for an identity we failed to remember.
        return _signin_page("Couldn't save that sign-in. Please try again.", 503)
    if person.get("scope") == "blocked":
        return _signin_page("That account is blocked here.")
    resp = RedirectResponse(_safe_next(row.get("next", "/player")), status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        sec.session_cookie(config.get("api_key") or "", person["sub"]),
        max_age=30 * 86400, httponly=True, samesite="lax",
        # Only over TLS when there is TLS: marking it secure on a plain http
        # LAN means the browser never sends it and nobody can stay signed in.
        secure=_net_scheme() == "https", path="/")
    log.info("%s signed in (%s)", person.get("email") or person["sub"],
             person.get("scope"))
    return resp


@app.get("/auth/signout")
def auth_signout(request: Request):
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _net_scheme() -> str:
    from ..core import net
    return net.scheme()


def _signin_page(message: str, status_code: int = 400):
    """A plain sentence rather than a JSON error: people see this one."""
    body = ("<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Sign in</title>"
            "<style>body{background:#0e0f16;color:#f5f6fb;font:15px/1.6 "
            "-apple-system,Segoe UI,Roboto,sans-serif;display:flex;"
            "min-height:100vh;align-items:center;justify-content:center;"
            "margin:0;padding:24px;text-align:center}a{color:#6d8bff}</style>"
            f"<div><p>{_esc(message)}</p><p><a href='/'>Back to the "
            "start page</a></p></div>")
    return HTMLResponse(body, status_code=status_code)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@app.get("/api/accounts")
def api_accounts(_: bool = Owner):
    """Everybody who has signed in, and what Google needs to be told."""
    return {"status": "ok", "people": accounts.everyone(),
            "configured": google.configured(),
            "redirect_uri": google.redirect_uri(),
            "owner_email": accounts.owner_email(),
            "new_account_scope": accounts.default_scope(),
            "new_account_scopes": accounts.NEW_ACCOUNT_SCOPES,
            "signed_in_count": accounts.count()}


@app.get("/api/accounts/scope")
def api_accounts_scope(sub: str = "", scope: str = "", _: bool = Owner):
    """Change what somebody may do, or block them."""
    try:
        got = accounts.set_scope(sub, scope)
    except (ValueError, accounts.AccountPersistenceError) as exc:
        raise HTTPException(503 if isinstance(exc, accounts.AccountPersistenceError) else 400,
                            str(exc))
    if not got:
        raise HTTPException(404, "no such account")
    return {"status": "ok", "person": got}


@app.get("/api/accounts/forget")
def api_accounts_forget(sub: str = "", _: bool = Owner):
    """Remove an account. Their next sign-in would start again as a stranger."""
    from ..core.profile import profiles
    from ..paths import data_dir

    if not accounts.get(sub):
        raise HTTPException(404, "no such account")
    profile_home = data_dir() / "profiles" / accounts.profile_id(sub)
    # Wipe the durable listening record before the account row.  If that
    # fails, retain the account so the owner can retry rather than claiming a
    # privacy deletion that left the profile intact.
    if profile_home.exists() and not profiles.wipe(accounts.profile_id(sub)):
        raise HTTPException(500, "Couldn't remove that account's profile")
    profiles.forget(accounts.profile_id(sub))
    try:
        deleted = accounts.forget(sub)
    except accounts.AccountPersistenceError:
        raise HTTPException(503, "Couldn't save the account removal")
    if not deleted:
        raise HTTPException(404, "no such account")
    return {"status": "ok"}


@app.get("/api/stats")
def api_stats(_: bool = Owner):
    """What this server has done: the house's totals and each link's.

    A link's record outlives the link — revoking somebody's access should
    not quietly rewrite what the month looked like.
    """
    from ..core import stats as stats_mod
    named = {}
    try:
        for row in sec.list_passes():
            named[row["id"]] = {"name": row.get("name", ""),
                                "scope": row.get("scope", ""),
                                "revoked": bool(row.get("revoked")),
                                "expired": bool(row.get("expired"))}
    except Exception as exc:
        log.debug("couldn't name the links: %s", exc)
    links = []
    for row in stats_mod.links():
        links.append({**row, **named.get(row["id"], {})})
    return {"status": "ok", "house": stats_mod.house(months=6), "links": links,
            "month": stats_mod.month_of()}


@app.get("/api/diag")
def api_diag(_: bool = Owner):
    return {
        "status": "ok",
        "mpv": bool(shutil.which("mpv")),
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "node": bool(shutil.which("node")),
        "spotdl": spotify.available(),
        "mpv_alive": player.mpv.alive(),
        "crossfade_engine": player.alt.alive(),
        "cookies": dict(cookie_mod.state),
        "groq": llm.status(),
        "queue": player.queue.stats(),
        "library": library.count(),
        "log": str(log_path()),
        "uptime": round(time.time() - _start, 1),
    }


@app.get("/api/audit")
def api_audit(_: bool = Owner):
    """Recent changes, without request bodies, URLs, tokens or secret values."""
    from ..core.audit import entries
    return {"status": "ok", "entries": list(reversed(entries()))}


# The install step validates the route inventory and creates the POST surface
# only after every endpoint has been registered.
install_api_policy(app, require_admin)
