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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.templating import Jinja2Templates

from ..config import config
from ..core import cookies as cookie_mod
from ..core import radio as radio_mod
from ..core.downloader import downloader
from ..core.extras import caster, scrobbler
from ..core.library import library
from ..core.playlists import playlists
from ..core.taste import taste
from ..events import Ev, bus
from ..logging_setup import get, log_path, spawn
from ..paths import resource_dir
from ..player import player
from ..requests import (add_spotify, handle_request, play_for_you,
                        play_station, play_video)
from ..resolve import catalog, llm, lyrics as lyrics_mod, spotify

log = get("api")

NEWLINE = chr(10)

app = FastAPI(title="Music Request Server", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(resource_dir()) / "web" / "templates"))
_start = time.time()


# ── auth ──────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def require_key(request: Request, key: str = Query(default="")) -> bool:
    supplied = key or request.headers.get("X-API-Key", "")
    expected = config.get("api_key") or ""
    if expected and supplied != expected:
        raise HTTPException(status_code=403, detail="Not authorised")
    if config.get("lock_ips"):
        allowed = config.get("allowed_ips") or []
        ip = _client_ip(request)
        if allowed and ip not in allowed and not ip.startswith("127."):
            raise HTTPException(status_code=403, detail="IP not allowed")
    return True


Auth = Depends(require_key)


# ── pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        host = s.getsockname()[0]
        s.close()
    except Exception:
        host = "127.0.0.1"
    return templates.TemplateResponse(request, "setup.html", {
        "host": host, "port": config.get("port", 5000),
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
    # Resolution + download happen on a worker; Siri gets an answer immediately.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: handle_request(text))
    return JSONResponse(result)


@app.get("/remote", response_class=HTMLResponse)
async def remote_page(request: Request, key: str = Query(default="")):
    """Full remote for a phone: controls, search, queue, playlists."""
    require_key(request, key)
    return templates.TemplateResponse(request, "remote.html",
                                      {"api_key": config.get("api_key", "")})


@app.get("/player", response_class=HTMLResponse)
async def player_page(request: Request):
    return templates.TemplateResponse(request, "player.html",
                                      {"api_key": config.get("api_key", "")})


# ── health + events ───────────────────────────────────────────────────

@app.get("/api/ping")
async def ping():
    return {"status": "ok", "uptime": round(time.time() - _start, 1),
            "app": "music-request-server", "version": 2}


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


@app.get("/api/events")
async def events(request: Request, key: str = Query(default="")):
    require_key(request, key)
    queue = bus.subscribe()

    async def stream():
        try:
            yield _sse({"type": "hello"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse(evt)
                except asyncio.TimeoutError:
                    yield ": keepalive" + NEWLINE + NEWLINE
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive"})


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
        "downloads": safe(downloader.cache_stats),
        "queue": safe(lambda: {
            "pool": len(player.queue._pool),
            "ready": player.queue.ready_ahead(),
            "minutes_ahead": round(player.queue.minutes_ahead(), 1),
        }),
        "log": str(log_path()),
    }


@app.get("/api/status")
def status(_: bool = Auth):
    return {"status": "ok", **player.status(), "queue": player.queue.snapshot()}


# ── playback ──────────────────────────────────────────────────────────

@app.get("/api/play")
def api_play(q: str = "", song: str = "", artist: str = "", mode: str = "play",
             source: str = "", cast: bool = False, _: bool = Auth):
    text = (q or song or "").strip()
    if artist and text:
        text = f"{text} by {artist}"
    elif artist:
        text = artist
    return handle_request(text, mode=mode, source=source or None, cast=cast)


@app.get("/api/play/video/{video_id}")
def api_play_video(video_id: str, title: str = "", artist: str = "", art: str = "",
                   mode: str = "play", _: bool = Auth):
    return play_video(video_id, title=title, artist=artist, art=art, mode=mode)


@app.get("/api/control/{action}")
def api_control(action: str, value: int | None = None, _: bool = Auth):
    return {"status": "ok", **player.control(action, value)}


@app.get("/api/seek")
def api_seek(pos: float, _: bool = Auth):
    return {"status": "ok", **player.seek(pos)}


@app.get("/api/restart")
def api_restart(_: bool = Auth):
    player.restart()
    return {"status": "ok", "message": "Player restarted"}


# ── queue ─────────────────────────────────────────────────────────────

@app.get("/api/queue/{op}")
def api_queue(op: str, index: int = 0, to: int = 0,
              frm: int = Query(default=-1, alias="from"), _: bool = Auth):
    if op == "move":
        return {"status": "ok",
                "ok": player.queue.move(frm if frm >= 0 else index, to)}
    if op == "remove":
        return {"status": "ok", "ok": player.queue.remove(index)}
    if op == "jump":
        return {"status": "ok", "ok": player.queue.jump(index)}
    if op == "undo":
        return {"status": "ok", "message": player.queue.undo()}
    if op == "stats":
        return {"status": "ok", **player.queue.stats()}
    raise HTTPException(404, "unknown queue operation")


@app.get("/api/cancel")
def api_cancel(_: bool = Auth):
    """The X beside the progress bar."""
    return {"status": "ok", **player.queue.cancel()}


@app.get("/api/radio")
def api_radio(count: int = 8, _: bool = Auth):
    player.queue.release_hold()
    return {"status": "ok", **player.queue_similar(count)}


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
def api_play_artist(name: str, _: bool = Auth):
    return handle_request(f"songs by {name}")


@app.get("/api/play/album")
def api_play_album(name: str, artist: str = "", _: bool = Auth):
    return handle_request(f"play the {name} album" + (f" by {artist}" if artist else ""))


@app.get("/api/lyrics")
def api_lyrics(_: bool = Auth):
    track = player.queue.current_track()
    if not track:
        return {"status": "ok", "lyrics": None}
    data = lyrics_mod.get_lyrics(track.title, track.artist, track.duration)
    return {"status": "ok", "lyrics": data}


@app.get("/api/history")
def api_history(_: bool = Auth):
    return {"status": "ok", "history": taste.recent(),
            "top_artists": taste.top_artists()}


@app.get("/api/liked")
def api_liked(_: bool = Auth):
    return {"status": "ok", "liked": taste.liked()}


# ── playlists ─────────────────────────────────────────────────────────

@app.get("/api/playlists")
def api_playlists(_: bool = Auth):
    return {"status": "ok", "playlists": playlists.summary(),
            "folder": str(playlists.root()),
            "download": bool(config.get("playlist_download"))}


@app.get("/api/station")
def api_station(url: str = "", name: str = "", art: str = "", _: bool = Auth):
    """Tune a live radio station."""
    return play_station(url, name, art)


@app.get("/api/foryou")
def api_foryou(_: bool = Auth):
    """A queue built from what you actually ask for."""
    return play_for_you(announce=False)


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
def api_spotify_add(url: str = "", _: bool = Auth):
    """Save a Spotify link as a playlist without hijacking what's playing."""
    return add_spotify(url)


@app.get("/api/playlist/{op}")
def api_playlist(op: str, name: str = "", shuffle: bool = False,
                 video_id: str = "", title: str = "", artist: str = "",
                 art: str = "", _: bool = Auth):
    if op == "create":
        playlists.create(name)
        return {"status": "ok", "message": f"Created {name}"}
    if op == "add":
        if video_id:
            from ..models import Track as _T
            return {"status": "ok", **playlists.add(
                name, _T(video_id=video_id, title=title, artist=artist, art=art))}
        return {"status": "ok", **player.playlist_add_current(name)}
    if op == "remove":
        return {"status": "ok", **playlists.remove(name, video_id)}
    if op == "delete":
        return {"status": "ok", **playlists.delete(name)}
    if op == "play":
        return {"status": "ok", **player.playlist_play(name, shuffle)}
    if op == "download":
        playlists.download_async(name)
        return {"status": "ok", "message": f"Saving {name} offline"}
    if op == "tracks":
        return {"status": "ok",
                "tracks": [t.to_dict() for t in playlists.tracks(name)]}
    raise HTTPException(404, "unknown playlist operation")


# ── settings ──────────────────────────────────────────────────────────

@app.get("/api/settings")
def api_settings(_: bool = Auth):
    return {"status": "ok", **player.settings(),
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
}


@app.get("/api/setting")
def api_setting(key: str, value: str = "", _: bool = Auth):
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


@app.get("/api/audio/device")
def api_audio_device(name: str = "auto", _: bool = Auth):
    return {"status": "ok", **player.set_audio_device(name)}


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
def api_lockips(enabled: int = 0, _: bool = Auth):
    config.set("lock_ips", bool(enabled))
    return {"status": "ok", "lock_ips": config.get("lock_ips")}


@app.get("/api/groqkey")
def api_groqkey(value: str = "", _: bool = Auth):
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
def api_boot(enabled: int = 0, _: bool = Auth):
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
def api_library_paths(add: str = "", remove: str = "", _: bool = Auth):
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
