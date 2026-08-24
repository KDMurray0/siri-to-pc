"""Boot and serve."""

from __future__ import annotations

import asyncio
import shutil
import threading

import socket

from .config import config
from .core import cookies as cookie_mod
from .core.downloader import downloader
from .core.library import library
from .events import bus
from .logging_setup import get, setup
from .paths import data_dir, ensure_structure, migrate_legacy_data
from .player import player
from .resolve import catalog, llm

log = get("server")


# The port we actually ended up on, which may not be the configured one.
runtime = {"port": None}


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_ours(port: int) -> bool:
    from urllib import request as urlrequest
    try:
        with urlrequest.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return b"music-request-server" in r.read(200)
    except Exception:
        return False


def pick_port(preferred: int) -> int:
    """Step aside when something else already holds the port.

    Windows lets a 127.0.0.1 bind win over our 0.0.0.0 bind, so a stale server
    on the same port silently swallows every request the player makes and the
    window just shows a bare 404.
    """
    if _port_free(preferred) or _is_ours(preferred):
        return preferred
    for candidate in range(preferred + 1, preferred + 21):
        if _port_free(candidate):
            log.warning("port %s is held by another server — using %s instead",
                        preferred, candidate)
            return candidate
    return preferred


def preflight() -> list[str]:
    """Check the outside world. Returns a list of fatal problems."""
    problems = []
    if not shutil.which("mpv"):
        problems.append("mpv is not on PATH — run setup.ps1")
    if not shutil.which("yt-dlp"):
        problems.append("yt-dlp is not on PATH — run setup.ps1")
    if not shutil.which("node"):
        log.warning("Node.js missing — YouTube will return no audio formats")
    return problems


def be_polite(quiet: bool = False) -> None:
    """Run below normal priority.

    This sits in the background all day and the actual audio comes out of mpv,
    which is a separate process and stays at normal. Nothing here is worth
    stealing a timeslice from whatever you're doing.

    Called repeatedly, not once: PortAudio's WASAPI backend puts the process
    back to normal whenever the loopback capture stream gets going, so setting
    this at startup on its own held for about four seconds. `quiet` is for the
    repeat calls, which shouldn't fill the log.
    """
    try:
        import ctypes
        BELOW_NORMAL = 0x00004000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # restype matters on 64-bit: the default int truncates the handle
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
        me = k32.GetCurrentProcess()
        if k32.GetPriorityClass(me) == BELOW_NORMAL:
            return
        if not k32.SetPriorityClass(me, BELOW_NORMAL):
            # Say so. This used to only log on success, so when it silently
            # stopped working the app just quietly ran at normal priority.
            if not quiet:
                log.warning("couldn't lower priority (error %d)",
                            ctypes.get_last_error())
            return
        if not quiet:
            log.info("running at below-normal priority (0x%x)",
                     k32.GetPriorityClass(me))
    except Exception as exc:
        if not quiet:
            log.warning("couldn't lower priority: %s", exc)


def startup() -> None:
    be_polite()
    setup(config.get("api_key", ""))
    made = ensure_structure()
    if made["created"]:
        log.info("first run — created %d paths under %s",
                 len(made["created"]), made["root"])
    moved = migrate_legacy_data()
    for m in moved:
        log.info("migrated %s", m)
    log.info("data directory: %s", data_dir())

    problems = preflight()
    for p in problems:
        log.error(p)
    if problems:
        raise SystemExit(1)

    try:
        catalog.validate()
        log.info("ytmusicapi ready")
    except Exception as exc:
        # Not fatal: ytmusicapi breaks on individual result shapes from time to
        # time, and that says nothing about whether normal searches work.
        log.warning("ytmusicapi check failed (continuing anyway): %s",
                    str(exc)[:200])

    # Groq self-heals a config pinned to a model Groq has retired.
    if llm.available():
        threading.Thread(target=llm.ensure_model, daemon=True).start()
        log.info("Groq enabled (%s)", config.get("groq_model"))
    else:
        log.info("Groq off — using the local parser")

    if not downloader.have_cookies():
        log.warning("no cookies configured — YouTube will refuse downloads")
    cookie_mod.start_watch()

    if config.get("library_paths"):
        library.scan_async()

    downloader.prune_cache()
    player.start()
    log.info("server ready")


def run() -> None:
    import uvicorn

    from .web.api import app

    startup()
    port = pick_port(int(config.get("port", 5000)))
    runtime["port"] = port
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bus.bind_loop(loop)

    cfg = uvicorn.Config(app, host=config.get("host", "0.0.0.0"),
                         port=port,
                         log_config=None, access_log=False, loop="asyncio")
    server = uvicorn.Server(cfg)
    try:
        loop.run_until_complete(server.serve())
    finally:
        player.stop()


def run_in_thread(port: int | None = None) -> threading.Thread:
    """Used by the tray launcher, which owns the main thread for the GUI."""
    if port:
        config.set("port", port, save=False)

    def target() -> None:
        try:
            run()
        except SystemExit:
            pass
        except Exception:
            log.exception("server crashed")

    t = threading.Thread(target=target, daemon=True, name="server")
    t.start()
    return t


if __name__ == "__main__":
    run()
