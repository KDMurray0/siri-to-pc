"""HTTP API.

FastAPI plus an SSE stream, so the UI doesn't poll. Blocking work (yt-dlp, mpv)
stays on threads via plain def routes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import config
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
    """Turn away known-bad addresses before any route runs, and make sure
    nothing this serves can leak a key onward through a Referer header."""
    ip = (request.client.host if request.client else "") or ""
    if bans.blocked(ip):
        return JSONResponse({"detail": "Blocked"}, status_code=403)
    resp = await call_next(request)
    # A page fetched with ?key= or ?token= in its URL would otherwise hand
    # that URL to every third-party it links to.
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


# ── auth ──────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


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


def require_key(request: Request, key: str = Query(default=""),
                token: str = Query(default="")) -> bool:
    expected = config.get("api_key") or ""
    if not expected:
        return True                      # no key set: nothing to check
    ip = _client_ip(request)

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
    if key and config.get("allow_key_in_url", True) and same_key(key, expected):
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
    if key and config.get("allow_key_in_url", True) and same_key(key, expected):
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
    """The setup page: the Shortcut recipe, and the key it needs.

    Guarded, because it prints the master key straight into the html. This
    was the one page that never got the treatment /player did — it predates
    the whole idea of the server being reachable from outside the house, and
    once the port was forwarded it meant anyone who found it owned the
    server. On the home network it opens as it always has.

    Owner-only rather than any-valid-link: a guest has no business here, and
    what's on it is the credential their link exists to avoid handing over.
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
        "api_key": config.get("api_key", "")})


@app.post("/")
async def siri(request: Request, key: str = Query(default="")):
    """The iOS Shortcut endpoint. Replies as soon as the request is understood."""
    require_key(request, key)
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
        None, lambda: handle_request(text, queue=target))
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
               or request.headers.get("X-API-Key") or "")
    home = is_home(ip) and config.get("lan_open", True) and not offered

    if not home:
        require_key(request, key, token)
    owner = home or is_owner(request, key)
    row = getattr(request.state, "pass_row", None) or {}
    creds = config.get("api_key", "") if owner else (token or key)
    if not creds and row.get("id"):
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
        "is_guest": "0" if owner else "1",
        "scope": "owner" if owner else (row.get("scope") or "full")})


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
def api_unskip(_: bool = Auth):
    """Bring back the last skipped track and unlearn the skip."""
    return player.queue.unskip()


@app.get("/api/health")
def health(_: bool = Auth):
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
    sessions.reap()
    return {"status": "ok", "sessions": sessions.listing()}


@app.get("/api/seek")
def api_seek(request: Request, pos: float, _: bool = Auth):
    room = _session_for(request)
    if room:
        room.mark_position(pos)                # the phone does the seeking
        return {"status": "ok", "position": room.position}
    return {"status": "ok", **player.seek(pos)}


@app.get("/api/restart")
def api_restart(_: bool = Auth):
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
    room = _session_for(request)
    q = room.queue if room else player.queue
    q.release_hold()
    track = q.current_track()
    if not track:
        return {"status": "ok", "ok": False, "message": "Nothing playing"}
    similar = catalog.related(track.video_id, limit=count)
    q.enqueue(similar)
    return {"status": "ok", "ok": True, "added": len(similar),
            "message": f"Queued {len(similar)} more like this"}


# ── search / metadata ─────────────────────────────────────────────────

@app.get("/api/search")
def api_search(q: str, limit: int = 12, _: bool = Auth):
    """Songs, artists, albums, your playlists and your own files.

    Your playlists come first — they're the things you made, so they should
    outrank anything YouTube suggests.
    """
    q = (q or "").strip()
    if not q:
        return {"status": "error", "message": "q required"}
    if spotify.is_spotify_url(q):
        return {"status": "ok", "spotify": True, "results": [], "playlists": [],
                "artists": [], "albums": [], "library": [],
                "message": "Spotify link — press Enter to import it"}
    return {
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


@app.get("/api/play/artist")
def api_play_artist(request: Request, name: str, _: bool = Auth):
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return handle_request(f"songs by {name}", queue=room.queue if room else None)


@app.get("/api/play/album")
def api_play_album(request: Request, name: str, artist: str = "", _: bool = Auth):
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
    return handle_request(f"play the {name} album" + (f" by {artist}" if artist else ""),
                          queue=room.queue if room else None)


@app.get("/api/lyrics")
def api_lyrics(request: Request, _: bool = Auth):
    """Words to whatever the caller is listening to.

    This said player.queue outright, so it answered with the owner's track
    whoever asked — which for a listener on their own device meant the wrong
    song's words, or, far more often, "no lyrics for this one" because the
    computer's speakers weren't playing anything at all. It also meant a link
    could read what the owner was listening to.
    """
    room = _session_for(request)
    track = room.current() if room else player.queue.current_track()
    if not track:
        return {"status": "ok", "lyrics": None}
    data = lyrics_mod.get_lyrics(track.title, track.artist, track.duration)
    return {"status": "ok", "lyrics": data}


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


@app.get("/api/playlists")
def api_playlists(request: Request, _: bool = Auth):
    mine = _lists_for(request)
    if mine is None:
        return {"status": "ok", "playlists": [], "folder": "",
                "download": False, "temporary": True,
                "message": "Playlists need a permanent link"}
    return {"status": "ok", "playlists": mine.summary(),
            "folder": str(mine.root()) if mine is playlists else "",
            "download": bool(config.get("playlist_download"))}


@app.get("/api/station")
def api_station(request: Request, url: str = "", name: str = "", art: str = "", _: bool = Auth):
    """Tune a live radio station."""
    room = _session_for(request)
    _guard_rate(room, request)
    if not room:
        _guard_shared(request)
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


@app.get("/api/playlist/{op}")
def api_playlist(request: Request, op: str, name: str = "",
                 shuffle: bool = False, video_id: str = "", title: str = "",
                 artist: str = "", art: str = "", start: int = 0,
                 _: bool = Auth):
    """Make and play lists — the caller's own, not always the owner's."""
    mine = _lists_for(request)
    if mine is None:
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
    if op == "add":
        from ..models import Track as _T
        if video_id:
            got = mine.add(
                name, _T(video_id=video_id, title=title, artist=artist, art=art))
        elif room:
            # "add what's on" has to mean what's on *their* player.
            cur = room.current()
            if not cur:
                return {"status": "ok", "ok": False, "message": "Nothing playing"}
            got = mine.add(name, cur)
        else:
            got = player.playlist_add_current(name)
        changed()
        return {"status": "ok", **got}
    if op == "remove":
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
        return {"status": "ok",
                "tracks": [t.to_dict() for t in mine.tracks(name)]}
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
            "spotdl": spotify.available()}


@app.get("/api/audio")
def api_audio(eq: str = "", normalize: int | None = None,
              crossfade: int | None = None, _: bool = Auth):
    if eq:
        config.set("eq", eq)
    if normalize is not None:
        config.set("normalize", bool(normalize))
    if crossfade is not None:
        config.set("crossfade", max(0, min(12, int(crossfade))))
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
    "tailscale": str, "tailscale_exe": str, "cache_size_mb": int,
    "allow_key_in_url": bool, "https": bool, "port": int,
    "block_full_guests": bool, "lan_open": bool, "party_mode": bool,
    "ddns_provider": str, "ddns_hostname": str, "ddns_user": str,
    "max_downloads": int, "guest_requests_hour": int,
    "cast_queue_minutes": int, "guest_quiet_pause": int, "guest_quiet_close": int,
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
    config.set(key, parsed)
    bus.publish(Ev.SETTINGS, player.settings())
    return {"status": "ok", "key": key, "value": parsed}


@app.get("/api/audio/devices")
def api_audio_devices(_: bool = Auth):
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


def _guard_rate(room, request: Request | None = None) -> None:
    """One guest can't spend everyone's evening, and it goes on their tab.

    Counted per pass rather than per address, because the pass is the person
    — moving to mobile data shouldn't reset anybody's allowance. Charged
    whether it played here or out of the computer's speakers: what the owner
    wants to know is what a link has been used for, not where it came out.
    """
    if room:
        cap = int(config.get("guest_requests_hour", 40))
        if cap and room.queue.recent_requests() >= cap:
            left = 60 - int((time.time() - room.queue.oldest_request()) / 60)
            raise HTTPException(
                429, f"That's {cap} songs in an hour — try again in "
                     f"{max(1, left)} minutes")
    row = getattr(request.state, "pass_row", None) if request is not None else None
    if row and not (row.get("internal") or row.get("owner")):
        sec.note_use(row["id"], requests=1, ip=_client_ip(request))


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
                     _: bool = Auth):
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
@app.get("/api/output/stream/{video_id}")
def api_output_stream(video_id: str, _: bool = Auth):
    """The track mpv is playing, as bytes a phone will accept.

    FileResponse handles Range itself, which is what gives the phone a
    draggable timeline instead of a take-it-or-leave-it download.
    """
    path, state = cast_mod.playable(video_id)
    if state == "missing":
        # Not "no such track" — almost always "the download hasn't finished".
        # A 404 tells the player to give up; a 503 tells it to come back, and
        # coming back is right, because it will be here shortly.
        return JSONResponse({"status": "not ready", "detail": "still fetching"},
                            status_code=503, headers={"Retry-After": "3"})
    if state != "ready":
        path, state = cast_mod.convert(video_id)
        if state != "ready" or not path:
            # 503 rather than an error: the client retries, it isn't broken.
            return JSONResponse({"status": "converting", "detail": state},
                                status_code=503)
    media = "audio/mp4" if path.suffix.lower() in (".m4a", ".mp4") else None
    return FileResponse(path, media_type=media or "application/octet-stream",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "private, max-age=3600"})


@app.get("/api/output/prepare/{video_id}")
def api_output_prepare(video_id: str, _: bool = Auth):
    """Warm the next track so the handover isn't audible."""
    _, state = cast_mod.playable(video_id)
    if state == "needs conversion":
        cast_mod.warm(video_id)
        state = "converting"
    return {"status": "ok", "state": state}


@app.get("/api/output/stats")
def api_output_stats(_: bool = Auth):
    return {"status": "ok", "casting": player.casting(),
            "ao": player.current_ao(), "alt_ao": player.current_ao(alt=True),
            "media_controls": player.mpv.get("media-controls", None),
            **cast_mod.stats()}


@app.get("/api/announce/{aid}.mp3")
def api_announce(aid: str, _: bool = Auth):
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
    got = sec.issue(key, name="this player", hours=max(1, hours), scope="full",
                    internal=True)
    return {"status": "ok", "token": got.get("token", ""),
            "expires_in": int(hours) * 3600}


@app.get("/api/passes")
def api_passes(_: bool = Owner):
    """Every link you've handed out, and who has it."""
    from ..core.session import sessions

    sec.tidy_passes()
    sessions.reap()
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
    key = config.get("api_key") or ""
    if not key:
        return {"status": "error", "message": "Set an API key first"}
    got = sec.issue(key, name=name, hours=hours, scope=scope)
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
    out = net.addresses(token)
    if check:
        out["port_open"] = net.port_open(net.live_port())
    return {"status": "ok", **out}


@app.get("/api/qr")
def api_qr(kind: str = "lan", pass_id: str = "", _: bool = Auth):
    """A scannable code for one of our own addresses.

    Takes a kind, not a url: an endpoint that renders any string handed to it
    is a QR generator for whoever finds it, and these carry the api key.
    """
    from ..core import net
    key = config.get("api_key") or ""
    token = (sec.reissue_token(key, pass_id) if pass_id else sec.owner_pass(key))
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
def api_theme(value: str = "default", _: bool = Auth):
    config.set("theme", value or "default")
    return {"status": "ok", "theme": config.get("theme")}


@app.get("/api/announce")
def api_announce(enabled: int = 1, _: bool = Auth):
    config.set("announce", bool(enabled))
    return {"status": "ok", "announce": config.get("announce")}


@app.get("/api/sleep")
def api_sleep(minutes: int = 0, _: bool = Auth):
    return {"status": "ok", **player.set_sleep(minutes)}


@app.get("/api/download")
def api_download(_: bool = Auth):
    return {"status": "ok", **player.export_current()}


@app.get("/api/pin")
def api_pin(_: bool = Auth):
    return {"status": "ok", **player.pin_current()}


@app.get("/api/source")
def api_source(value: str = "youtube", _: bool = Auth):
    config.set("source", (value or "youtube").lower())
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
def api_groqmodels(refresh: int = 0, _: bool = Auth):
    """What Groq will serve, so the picker can't offer a retired model."""
    return {"status": "ok", "models": llm.models(force=bool(refresh)),
            "current": config.get("groq_model") or llm.DEFAULT_MODEL,
            "default": llm.DEFAULT_MODEL}


@app.get("/api/groqmodel")
def api_groqmodel(value: str = "", _: bool = Auth):
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


# ── cookies ───────────────────────────────────────────────────────────

@app.get("/api/cookies")
def api_cookies(_: bool = Auth):
    return {"status": "ok", **cookie_mod.state,
            "path": str(cookie_mod.cookie_path()),
            "file": cookie_mod.inspect(),
            "browsers": cookie_mod.installed_browsers()}


@app.get("/api/cookies/find")
def api_cookies_find(close: int = 0, _: bool = Auth):
    """Test the cookies; only closes a browser if explicitly asked (close=1)."""
    return {"status": "ok", **cookie_mod.find_now(close_browsers=bool(close)),
            "path": str(cookie_mod.cookie_path())}


@app.get("/api/cookies/extension")
def api_cookies_extension(_: bool = Auth):
    """Chrome can't be decrypted, so use the export extension instead: this
    tells the user what to install and watches Downloads for the result."""
    return {"status": "ok", **cookie_mod.extension_flow()}


@app.get("/api/cookies/signedin")
def api_cookies_signedin(saved: int = 0, _: bool = Auth):
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
def api_cookies_import(path: str = "", _: bool = Auth):
    """Import a cookies.txt the user points at (or the newest in Downloads)."""
    from pathlib import Path as _P
    src = _P(path) if path else cookie_mod.scan_downloads(max_age=86400)
    if not src or not _P(src).is_file():
        return {"status": "error", "ok": False,
                "message": "No cookies file found to import"}
    return {"status": "ok", **cookie_mod.import_file(_P(src))}


@app.get("/api/cookies/grab")
def api_cookies_grab(browser: str, close: int = 0, _: bool = Auth):
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
def api_open_folder(_: bool = Auth):
    """Open the data folder in Explorer."""
    import subprocess
    from ..paths import data_dir
    try:
        subprocess.Popen(["explorer", str(data_dir())])
        return {"status": "ok", "message": f"Opened {data_dir()}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/library/scan")
def api_library_scan(_: bool = Auth):
    library.scan_async()
    return {"status": "ok", "message": "Scanning your library"}


@app.get("/api/library/paths")
def api_library_paths(add: str = "", remove: str = "", _: bool = Owner):
    paths = list(config.get("library_paths") or [])
    if add and add not in paths:
        paths.append(add)
    if remove and remove in paths:
        paths.remove(remove)
    config.set("library_paths", paths)
    return {"status": "ok", "paths": paths, "count": library.count()}


# ── last.fm / alarms / cast ───────────────────────────────────────────

@app.get("/api/lastfm")
def api_lastfm(step: str = "", api_key: str = "", secret: str = "", _: bool = Auth):
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
               days: str = "", remove: int = -1, _: bool = Auth):
    alarms = list(config.get("alarms") or [])
    if remove >= 0 and remove < len(alarms):
        alarms.pop(remove)
    if add and time_:
        alarms.append({"query": add, "time": time_, "enabled": True,
                       "days": [int(d) for d in days.split(",") if d.strip().isdigit()]})
    config.set("alarms", alarms)
    return {"status": "ok", "alarms": alarms}


@app.get("/api/cast")
def api_cast(add: str = "", remove: str = "", text: str = "", _: bool = Auth):
    peers = list(config.get("cast_peers") or [])
    if add and add not in peers:
        peers.append(add)
    if remove and remove in peers:
        peers.remove(remove)
    config.set("cast_peers", peers)
    sent = caster.broadcast(text) if text else []
    return {"status": "ok", "peers": peers, "sent": sent}


@app.get("/api/stream/{video_id}")
def api_stream(video_id: str, _: bool = Auth):
    """Serve a cached file so a peer can play the exact same audio."""
    path = downloader.cached(video_id)
    if not path:
        raise HTTPException(404, "not cached")
    return FileResponse(path)


# ── diagnostics ───────────────────────────────────────────────────────

@app.get("/api/diag")
def api_diag(_: bool = Auth):
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
