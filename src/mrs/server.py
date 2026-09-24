"""Boot and serve."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .config import config
from .core import cookies as cookie_mod
from .core.downloader import downloader
from .core.extras import scrobbler
from .core.library import library
from .events import bus
from .logging_setup import get, mark, spawn, setup
from .paths import data_dir, ensure_structure, migrate_legacy_data
from .player import player
from .resolve import catalog, llm

log = get("server")


# The port we actually ended up on, which may not be the configured one.
# `wanted_port` is set only when those differ, so the Sharing tab can say the
# links it's showing don't match the port the router forwards.
runtime = {"port": None, "wanted_port": None, "error": ""}


def proxy_tls() -> bool:
    """Whether a local gateway owns public HTTPS for this server.

    The gateway and this process must be on the same machine. This is not a
    generic 'trust any forwarded header' switch: the proxy-mode server binds
    loopback only and accepts forwarded headers only from loopback.
    """
    return config.get("https_mode") == "proxy"


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


def open_local(url: str, timeout: float = 1.5):
    """Open one of our own URLs, TLS and all.

    The certificate is one we issued to ourselves, so verifying it is both
    impossible and pointless — this is a loopback call to a socket we own.
    """
    from urllib import request as urlrequest
    ctx = None
    if url.startswith("https://"):
        import ssl
        ctx = ssl._create_unverified_context()
    return urlrequest.urlopen(url, timeout=timeout, context=ctx)


def local_url(port: int, path: str = "/api/ping") -> str:
    """Our own address, in plaintext.

    With a certificate loaded the network side speaks TLS, and this machine
    keeps a plain http socket on loopback for the window, the tray and every
    readiness check. A certificate is issued to a name; asking for it at
    127.0.0.1 is a name mismatch, and the window that has to load the player
    would be showing a certificate warning to its own server.
    """
    return f"http://127.0.0.1:{runtime.get('local_port') or port}{path}"


def tls_files() -> tuple[str, str] | None:
    """The certificate and key to serve with, if both are there and load.

    Checked by loading them, not by looking: a path that points at nothing,
    a key that doesn't match its certificate or a file half-written by a
    renewal all look fine until a browser asks, and then nothing works and
    the reason is in no log.
    """
    cert = str(config.get("tls_cert") or "").strip()
    key = str(config.get("tls_key") or "").strip()
    if not cert or not key:
        # The place certificate.ps1 puts them, so a renewal is a file write
        # and nothing has to be told about it.
        from .paths import data_dir
        pair = data_dir() / "certs" / "fullchain.pem", data_dir() / "certs" / "privkey.pem"
        if not all(p.is_file() for p in pair):
            return None
        cert, key = (str(p) for p in pair)
    import ssl
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    except (OSError, ValueError, ssl.SSLError) as exc:
        log.error("certificate not usable (%s) — serving plain http instead", exc)
        return None
    return cert, key


def cert_days_left(cert: str = "") -> float:
    """Days until the certificate expires, or 0 if it can't be read."""
    import ssl
    path = cert or str(config.get("tls_cert") or "")
    # certificate.ps1 deliberately leaves the explicit config fields empty:
    # it atomically renews the conventional pair under the data directory.
    # Looking only at tls_cert made the Sharing page claim every such valid
    # Dynu certificate had zero days left.
    if not path:
        pair = tls_files()
        path = pair[0] if pair else ""
    if not path:
        return 0.0
    try:
        got = ssl._ssl._test_decode_cert(path)
        ends = ssl.cert_time_to_seconds(got["notAfter"])
        return max(0.0, (ends - time.time()) / 86400)
    except Exception:
        return 0.0


def _port_free(port: int) -> bool:
    """Can we bind it right now?"""
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((config.get("host", "0.0.0.0"), port))
        return True
    except (OSError, OverflowError, ValueError):
        return False
    finally:
        s.close()


def _free_loopback_port(near: int) -> int:
    """A port on 127.0.0.1 nothing else holds, starting beside the main one."""
    for candidate in [near + 1, near + 2, 0]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", candidate))
            return int(s.getsockname()[1])
        except OSError:
            continue
        finally:
            s.close()
    return 0


def _is_ours(port: int) -> bool:
    """Is the thing holding this port us?

    Both schemes, though we now only ever serve one. A copy left running
    from a build that served TLS still has to be recognisable as ours, or
    the handover takes it for a stranger and refuses to displace it — and
    the whole point of the handover is that a copy which cannot serve gets
    out of the way of one that can.

    Asking a single way is what made "switch HTTPS on" mean "the program
    never finishes starting": the server came up on TLS exactly as asked,
    every readiness check asked in plaintext, TLS answered by closing the
    connection, and the launcher concluded no server had ever arrived.
    """
    for scheme in ("http", "https"):
        try:
            with open_local(f"{scheme}://127.0.0.1:{port}/api/ping", 1.5) as r:
                if b"music-request-server" in r.read(200):
                    return True
        except Exception:
            continue
    return False


def pick_port(preferred: int) -> int:
    """Step aside when something else already holds the port.

    Windows lets a 127.0.0.1 bind win over our 0.0.0.0 bind, so a stale server
    on the same port silently swallows every request the player makes and the
    window just shows a bare 404.
    """
    try:
        preferred = int(preferred)
    except (TypeError, ValueError, OverflowError):
        preferred = 7420
    if not 1025 <= preferred <= 65535:
        log.warning("configured port %r is invalid; using 7420", preferred)
        preferred = 7420
    if _is_ours(preferred):
        # Another copy of us is already answering here. Starting anyway means
        # two servers sharing one set of mpv pipes, each killing the other's
        # player as a "stray" — which looks exactly like mpv crashing in a
        # loop and has cost hours to diagnose more than once.
        raise SystemExit(
            f"Music Request Server is already running on port {preferred}. "
            "Close the other copy (check the system tray) and try again.")
    # Wait for it rather than stepping aside at the first refusal. Almost
    # every time this port is busy it's our own previous process still letting
    # go of it, or the socket sitting in TIME_WAIT for a few seconds — and
    # moving means every link you've handed out, and the forward rule on the
    # router, now point at a port nothing is listening on. That reads as a
    # firewall problem and isn't one, so it's worth eight seconds to avoid.
    import time as _time
    for attempt in range(16):
        if _port_free(preferred):
            if attempt:
                log.info("port %s came free after %.1fs", preferred, attempt * 0.5)
            return preferred
        _time.sleep(0.5)

    for candidate in range(preferred + 1, preferred + 21):
        if _port_free(candidate):
            log.warning("port %s is still held by something else — serving on "
                        "%s. Links and your router's forward rule both name "
                        "%s, so they will not reach this until it's free.",
                        preferred, candidate, preferred)
            runtime["wanted_port"] = preferred
            return candidate
    return preferred


# What the app cannot do its job without, and what setup.ps1 calls each one.
TOOLS = (
    ("mpv", True, "plays the audio"),
    ("yt-dlp", True, "fetches it"),
    ("node", False, "YouTube returns no audio formats without it"),
)


def missing_tools() -> list[str]:
    """Which of the outside pieces aren't here."""
    return [name for name, _fatal, _why in TOOLS if not shutil.which(name)]


def preflight() -> list[str]:
    """Check the outside world. Returns a list of fatal problems."""
    problems = []
    for name, fatal, why in TOOLS:
        if shutil.which(name):
            continue
        if fatal:
            problems.append(f"{name} is not on PATH — it {why}")
        else:
            log.warning("%s missing — %s", name, why)
    return problems


def setup_script() -> str:
    """Where setup.ps1 is, whether we're frozen or running from source.

    Beside the exe first, because that's the copy someone may have edited,
    then the one packed into the bundle — which exists precisely so a deleted
    or moved script can't leave the app with no way to fix itself.
    """
    import sys
    from .paths import resource_dir

    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    roots = [here] if here else []
    roots += [Path(resource_dir()), Path(__file__).resolve().parents[2]]
    for root in roots:
        try:
            got = root / "setup.ps1"
            if got.is_file():
                return str(got)
        except Exception:
            continue
    return ""


def repair(missing: list[str]) -> bool:
    """Hand the missing pieces to setup.ps1 and wait for it.

    The alternative — which is what used to happen — is that the app exits
    with a line in a log file nobody opens, and the tray launcher then puts a
    window on screen pointing at a server that isn't running. A visible
    console that says what it's installing is worth a great deal more.

    Only the missing ones are named, so this doesn't reinstall a working mpv
    or walk back through the cookie setup to fix a missing Node.
    """
    script = setup_script()
    if not script:
        log.error("setup.ps1 isn't next to the app — install %s by hand",
                  ", ".join(missing))
        return False
    log.warning("missing %s — running setup to fetch just those",
                ", ".join(missing))
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script, "-Repair", ",".join(missing)]
    try:
        # A new console, because this is a windowed app and the whole point is
        # that you can see it working.
        CREATE_NEW_CONSOLE = 0x00000010
        subprocess.run(cmd, timeout=900, creationflags=CREATE_NEW_CONSOLE)
    except Exception as exc:
        log.error("couldn't run setup: %s", exc)
        return False
    # winget puts things on PATH for *new* processes; ours already started.
    _reload_path()
    still = missing_tools()
    fixed = [m for m in missing if m not in still]
    if fixed:
        log.info("setup installed %s", ", ".join(fixed))
    return not [m for m in still if any(
        m == name and fatal for name, fatal, _ in TOOLS)]


def _reload_path() -> None:
    """Pick up what winget just added, without making anyone restart."""
    try:
        import winreg
        parts = []
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    parts.append(winreg.QueryValueEx(k, "Path")[0])
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])
    except Exception as exc:
        log.debug("couldn't reload PATH: %s", exc)


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
    if problems:
        # Don't just die with a line in a log file. Everything it needs is one
        # winget install away, and the script that does it ships alongside.
        for p in problems:
            log.error(p)
        if not repair(missing_tools()):
            for p in preflight():
                log.error("still missing after setup: %s", p)
            raise SystemExit(
                "Music Request Server needs mpv and yt-dlp. Setup ran but "
                "couldn't install them — run setup.ps1 by hand and check "
                "setup-log.txt.")
        log.info("setup finished — carrying on")

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
        spawn(llm.ensure_model, name="groq warmup")
        log.info("Groq enabled (%s)", config.get("groq_model"))
    else:
        log.info("Groq off — using the local parser")

    # If they connected Last.fm at some point, borrow what it knows.
    spawn(scrobbler.maybe_seed_taste, name="lastfm seed")

    cookie_mod.settle_path()
    if not downloader.have_cookies():
        log.warning("no cookies configured — YouTube will refuse downloads")
    cookie_mod.start_watch()

    if config.get("library_paths"):
        library.scan_async()
    if config.get("library_monitor_minutes"):
        library.start_monitor()

    downloader.prune_cache()
    player.start()
    # Guests get their own watchdog rather than riding on the owner's queue
    # loop: "pause them when they drop off the network" needs seconds, not
    # the minute that loop runs on.
    from .core.session import sessions
    sessions.watch()
    log.info("server ready")


def run() -> None:
    # These marks are the only trace of everything that happens before
    # logging exists — the imports, and startup() up to the point it attaches
    # a handler. A boot that stops in there used to leave the log completely
    # empty, so the error box had nothing to say and neither did anyone else.
    mark("loading")
    import uvicorn

    from .web.api import app

    mark("loaded, starting up")
    startup()
    from .core import ddns
    ddns.start()
    port = pick_port(config.get("port", 7420))
    runtime["port"] = port
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bus.bind_loop(loop)

    # A certificate this machine signs for itself is one no client will
    # accept without being told to: Safari refuses it outright, iOS offers no
    # exception for a bare IP, and the window needed a Chromium flag to load
    # its own player. So there is no self-signed option and no way to turn
    # one on. A certificate signed by an authority, for a name that resolves
    # here, is a different thing entirely — that is what tls_cert is for, and
    # everything this machine asks itself still goes over loopback in plain
    # http so none of the old breakage can come back.
    def server_for(**kw):
        return uvicorn.Server(uvicorn.Config(
            app, log_config=None, access_log=False, loop="asyncio", **kw))

    tls = tls_files()
    edge_tls = proxy_tls()
    runtime["proxy_tls"] = edge_tls
    runtime["tls"] = bool(tls) and not edge_tls
    try:
        if edge_tls:
            # Cloudflared/nginx/Caddy terminates TLS and must use this plain
            # loopback origin. Pointing a gateway at https://127.0.0.1:PORT
            # when the app is in proxy mode sends TLS bytes to an HTTP server
            # and produces the opaque 'origin gateway refused' failure.
            runtime.pop("local_port", None)
            log.info("public https is handled by a local gateway; origin is "
                     "http://127.0.0.1:%d", port)
            loop.run_until_complete(server_for(
                host="127.0.0.1", port=port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1").serve())
        elif tls:
            loop.run_until_complete(_serve_secure(server_for, port, tls))
        else:
            runtime.pop("local_port", None)
            runtime.pop("proxy_tls", None)
            loop.run_until_complete(
                server_for(host=config.get("host", "0.0.0.0"), port=port,
                           proxy_headers=False).serve())
    finally:
        runtime.pop("proxy_tls", None)
        player.stop()


def _cert_stamp(cert: str, key: str) -> tuple:
    """Enough of the files to notice a renewal writing new ones."""
    out = []
    for path in (cert, key):
        try:
            st = os.stat(path)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


async def _serve_secure(server_for, port: int, tls: tuple[str, str]) -> None:
    """TLS to the network, plain http to this machine, renewals followed.

    uvicorn reads a certificate once, when it binds. A renewal sixty days
    from now writes new files in place and the old certificate would keep
    being served until somebody restarted the music, so the socket is rebuilt
    here instead — the player, the queue and whatever is playing don't notice.
    """
    cert, key = tls
    local_port = _free_loopback_port(port)
    runtime["local_port"] = local_port
    plain = server_for(host="127.0.0.1", port=local_port)
    plain_task = asyncio.ensure_future(plain.serve())
    log.info("https on port %d (%.0f days on the certificate); plain http on "
             "127.0.0.1:%d for this machine", port, cert_days_left(cert), local_port)
    try:
        while True:
            stamp = _cert_stamp(cert, key)
            secure = server_for(host=config.get("host", "0.0.0.0"), port=port,
                                ssl_certfile=cert, ssl_keyfile=key)
            task = asyncio.ensure_future(secure.serve())
            renewed = False
            while not task.done():
                await asyncio.sleep(20)
                if _cert_stamp(cert, key) != stamp and tls_files():
                    log.info("certificate renewed — taking up the new one")
                    secure.should_exit = True
                    await task
                    renewed = True
                    break
            if not renewed:
                await task          # it stopped on its own, so do we
                return
            # The old socket has just let go. Windows can take a moment to
            # agree, and failing to rebind here would leave the network side
            # dark until somebody restarted the app.
            for wait in (0.5, 1, 2, 4, 8):
                await asyncio.sleep(wait)
                if _port_free(port):
                    break
    finally:
        plain.should_exit = True
        try:
            await plain_task
        except Exception:
            pass


def run_in_thread(port: int | None = None) -> threading.Thread:
    """Used by the tray launcher, which owns the main thread for the GUI."""
    if port:
        config.set("port", port, save=False)

    def target() -> None:
        try:
            run()
        except SystemExit as exc:
            # Keep the reason. Swallowing it meant the launcher had nothing to
            # show but a guess, and the guess was wrong — "nothing obvious is
            # missing" when what actually happened was "another copy of this
            # is already running on that port".
            runtime["error"] = str(exc) or "the server stopped during startup"
            log.error("server did not start: %s", runtime["error"])
        except Exception as exc:
            runtime["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("server crashed")

    t = threading.Thread(target=target, daemon=True, name="server")
    t.start()
    return t


if __name__ == "__main__":
    run()
