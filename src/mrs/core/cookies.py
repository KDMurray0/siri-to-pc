"""YouTube cookies.

What I found out the hard way:
  - no cookies -> "The page needs to be reloaded"
  - a running Chromium browser locks its cookie db
  - Chrome v127+ can't be decrypted even closed (DPAPI, yt-dlp #10927)
  - Firefox is fine either way

So an exported cookies.txt is the reliable route. Only YouTube/Google cookies
are kept, the rest of the export isn't our business.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib import request as urllib_request

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..paths import data_dir

log = get("cookies")

CREATE_NO_WINDOW = 0x08000000
TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TEST_ID = "dQw4w9WgXcQ"

# name, process image, does it lock its cookie DB while running
BROWSERS = [
    ("firefox", "firefox.exe", False),
    ("chrome", "chrome.exe", True),
    ("edge", "msedge.exe", True),
    ("brave", "brave.exe", True),
    ("vivaldi", "vivaldi.exe", True),
    ("opera", "opera.exe", True),
]

KEEP_DOMAINS = ("youtube.com", "youtu.be", "google.com", "ytimg.com",
                "googlevideo.com", "google.co.uk", "accounts.google.com")

state = {
    "ok": None,          # True / False / None = not checked yet
    "source": "",
    "message": "",
    "checked_at": 0.0,
    "checking": False,
}

_lock = threading.Lock()


def unreadable() -> list[str]:
    """Browsers we've already proven we can't decrypt (Chrome v127+ and friends).

    Closing one of these would cost the user their session and still fail, so
    once we've learned it we never close that browser again.
    """
    return list(config.get("unreadable_browsers") or [])


def _mark_unreadable(name: str) -> None:
    known = unreadable()
    if name not in known:
        known.append(name)
        config.set("unreadable_browsers", known)
        log.info("%s can't be decrypted — won't ask to close it again", name)


def cookie_path() -> Path:
    configured = (config.get("cookies_file") or "").strip()
    if configured:
        return Path(configured)
    return data_dir() / "youtube_cookies.txt"


def _publish() -> None:
    bus.publish(Ev.COOKIES, dict(state))


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def browser_running(image: str) -> bool:
    code, out = _run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], timeout=20)
    return image.lower() in (out or "").lower()


def installed_browsers() -> list[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    paths = {
        "firefox": [rf"{pf}\Mozilla Firefox\firefox.exe", rf"{pf86}\Mozilla Firefox\firefox.exe"],
        "chrome": [rf"{pf}\Google\Chrome\Application\chrome.exe",
                   rf"{pf86}\Google\Chrome\Application\chrome.exe"],
        "edge": [rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
                 rf"{pf}\Microsoft\Edge\Application\msedge.exe"],
        "brave": [rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe"],
        "vivaldi": [rf"{local}\Vivaldi\Application\vivaldi.exe"],
        "opera": [rf"{local}\Programs\Opera\opera.exe"],
    }
    return [name for name, opts in paths.items()
            if any(p and os.path.isfile(p) for p in opts)]


def filter_to_youtube(path: Path) -> int:
    """Strip every cookie that isn't YouTube/Google. Returns lines kept."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0
    kept = ["# Netscape HTTP Cookie File",
            "# Filtered to YouTube/Google by Music Request Server."]
    count = 0
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        domain = line.split("\t", 1)[0].lstrip(".").lower()
        if any(domain.endswith(d) for d in KEEP_DOMAINS):
            kept.append(line)
            count += 1
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception as exc:
        log.warning("could not rewrite cookie file: %s", exc)
    return count


# -- checking ----------------------------------------------------------

def _auth_args(cookies_file: str | None = None) -> list[str]:
    from .downloader import downloader
    if cookies_file:
        args = ["--cookies", cookies_file]
        if config.get("js_runtime"):
            args += ["--js-runtimes", str(config.get("js_runtime"))]
        if config.get("player_client"):
            args += ["--extractor-args",
                     f"youtube:player_client={config.get('player_client')}"]
        return args
    return downloader.auth_args()


def check(cookies_file: str | None = None) -> tuple[bool, str]:
    """Does YouTube actually let us fetch a video right now?"""
    exe = shutil.which("yt-dlp")
    if not exe:
        return False, "yt-dlp not on PATH"
    args = [exe, "--simulate", "--no-warnings", "--no-playlist",
            "--print", "%(id)s"] + _auth_args(cookies_file) + ["--", TEST_URL]
    code, out = _run(args)
    if code == 0 and TEST_ID in out:
        return True, "ok"
    err = next((l.strip() for l in reversed(out.splitlines()) if "ERROR" in l), "")
    return False, (err[:180] or "check failed")


def extract_from(browser: str, dest: Path) -> bool:
    """Pull cookies out of one browser into `dest`. False if it can't be read."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return False
    tmp = dest.with_suffix(".new")
    args = [exe, "--cookies-from-browser", browser, "--cookies", str(tmp),
            "--simulate", "--no-warnings", "--no-playlist", "--print", "%(id)s"]
    if config.get("js_runtime"):
        args += ["--js-runtimes", str(config.get("js_runtime"))]
    if config.get("player_client"):
        args += ["--extractor-args",
                 f"youtube:player_client={config.get('player_client')}"]
    args += ["--", TEST_URL]
    code, out = _run(args)
    if code == 0 and TEST_ID in out and tmp.exists() and tmp.stat().st_size > 200:
        kept = filter_to_youtube(tmp)
        os.replace(tmp, dest)
        log.info("cookies refreshed from %s (%d YouTube cookies)", browser, kept)
        return True
    if "Could not copy" in out or "another process" in out:
        log.info("%s is open, so its cookie database is locked", browser)
    elif "DPAPI" in out or "decrypt" in out:
        log.info("%s encrypts its cookies (v127+) — can't be read at all", browser)
        _mark_unreadable(browser)
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return False


def refresh(*, only: list[str] | None = None, skip_running: bool = True) -> str | None:
    """Try each browser in turn. Returns the one that worked."""
    dest = cookie_path()
    preferred = only or [b.strip() for b in
                         (config.get("cookie_browsers") or "").split(",") if b.strip()]
    present = installed_browsers()
    order = [n for n, _img, _lock in BROWSERS if n in present]
    if preferred:
        order = [b for b in preferred if b in present] or preferred

    for name in order:
        image, locks = next(((i, l) for n, i, l in BROWSERS if n == name),
                            (f"{name}.exe", True))
        if skip_running and locks and browser_running(image):
            log.info("skipping %s — it's running and locks its cookie DB", name)
            continue
        if extract_from(name, dest):
            config.update({"cookies_file": str(dest), "cookies_from_browser": ""})
            return name
    return None


def ensure(*, allow_refresh: bool = True) -> bool:
    """Check the cookies; refresh them if they're dead and we're allowed to."""
    with _lock:
        state["checking"] = True
        _publish()
        try:
            ok, msg = check()
            if ok:
                state.update(ok=True, message="ok", checked_at=time.time(),
                             source="file" if config.get("cookies_file") else "browser")
                return True
            log.warning("cookie check failed: %s", msg)
            state.update(ok=False, message=msg, checked_at=time.time())
            if not allow_refresh:
                return False
            got = refresh()
            if got:
                ok2, msg2 = check()
                state.update(ok=ok2, source=f"browser:{got}",
                             message=f"refreshed from {got}" if ok2 else msg2,
                             checked_at=time.time())
                if ok2:
                    bus.publish(Ev.TOAST, f"Cookies refreshed from {got}")
                return ok2
            state["message"] = ("Cookies expired. Close your browser and press "
                                "Find cookies, or export youtube_cookies.txt.")
            return False
        finally:
            state["checking"] = False
            _publish()


def find_now(close_browsers: bool = False) -> dict:
    """The 'Find cookies' button.

    A running Chromium browser locks its cookie database, so with
    close_browsers the button closes it (the button says so), takes the
    cookies, and reports what happened. The browser is asked to close
    politely — no /F — so it can save your tabs and restore them next launch.
    """
    if check()[0]:
        state.update(ok=True, message="ok", checked_at=time.time())
        _publish()
        return {"ok": True, "message": "Cookies already working.", "closed": []}

    closed: list[str] = []
    got = refresh()                       # try anything readable first

    if not got and close_browsers:
        blocked = unreadable()
        for name, image, locks in BROWSERS:
            if not locks or name not in installed_browsers():
                continue
            if not browser_running(image):
                continue
            if name in blocked:
                log.info("not closing %s — its cookies can't be decrypted anyway",
                         name)
                continue
            log.info("closing %s to read its cookies (user pressed Find cookies)", name)
            if close_browser(name):
                closed.append(name)
                time.sleep(1.5)           # let it release the file
                if extract_from(name, cookie_path()):
                    got = name
                    break

    if got:
        config.update({"cookies_file": str(cookie_path()), "cookies_from_browser": ""})
        ok, msg = check()
        state.update(ok=ok, source=f"browser:{got}", checked_at=time.time(),
                     message="ok" if ok else msg)
        _publish()
        if ok:
            note = f"Got your cookies from {got}"
            if closed:
                note += f" (closed {', '.join(closed)})"
            return {"ok": True, "message": note + ". YouTube access is working.",
                    "closed": closed, "browser": got}
        return {"ok": False, "closed": closed,
                "message": f"Read {got}'s cookies but YouTube still refused: {msg}"}

    still_running = [n for n, img, locks in BROWSERS
                     if locks and n in installed_browsers() and browser_running(img)]
    state.update(ok=False, checked_at=time.time())
    _publish()
    dead = [b for b in unreadable() if b in installed_browsers()]
    if dead:
        why = (", ".join(dead) + " encrypt their cookies (Chrome v127+), so they "
               "can't be read even when closed. Install Firefox and sign into "
               "YouTube there, or export a cookies file.")
    else:
        why = "Couldn't read cookies from any browser."
    return {
        "ok": False,
        "closed": closed,
        "blocked_by": still_running,
        "unreadable": dead,
        "message": f"{why} Save an export as {cookie_path()}",
    }


def close_browser(name: str) -> bool:
    """Close a browser so its cookies can be read. Opt-in only."""
    image = next((img for n, img, _ in BROWSERS if n == name), None)
    if not image:
        return False
    log.info("closing %s at the user's request", name)
    _run(["taskkill", "/IM", image, "/T"], timeout=30)   # polite close, no /F
    for _ in range(20):
        if not browser_running(image):
            return True
        time.sleep(0.5)
    return not browser_running(image)


def grab_after_close(name: str, timeout: int = 600) -> bool:
    """Wait for a browser to exit, then take its cookies. Opt-in only."""
    image = next((img for n, img, _ in BROWSERS if n == name), None)
    if not image:
        return False
    log.info("waiting for %s to close so cookies can be read", name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not browser_running(image):
            time.sleep(1.5)          # let it release the file
            if extract_from(name, cookie_path()):
                bus.publish(Ev.TOAST, f"Cookies picked up from {name}")
                ensure(allow_refresh=False)
                return True
            return False
        time.sleep(3)
    return False


# ── the extension route ───────────────────────────────────────────────
# Chrome v127+ won't give up its cookies to yt-dlp at all, so the practical
# path is the "Get cookies.txt LOCALLY" extension: the user clicks Export, and
# we pick the file up out of Downloads and import it for them.

EXTENSION_URL = ("https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
                 "cclelndahbckbenkjhflpdbgdldlbecc")
_COOKIE_FILE_HINT = re.compile(r"cookies?.*\.txt$", re.I)


def downloads_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Downloads"


def scan_downloads(max_age: int = 900) -> Path | None:
    """A recently-downloaded cookies.txt that actually mentions YouTube."""
    folder = downloads_dir()
    if not folder.is_dir():
        return None
    best, best_time = None, 0.0
    now = time.time()
    for f in folder.glob("*.txt"):
        try:
            if not _COOKIE_FILE_HINT.search(f.name):
                continue
            age = now - f.stat().st_mtime
            if age > max_age:
                continue
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
            if "youtube.com" not in head.lower():
                continue
            if f.stat().st_mtime > best_time:
                best, best_time = f, f.stat().st_mtime
        except Exception:
            continue
    return best


def import_file(src: Path) -> dict:
    """Take an exported cookies.txt, keep only YouTube, and test it."""
    dest = cookie_path()
    try:
        shutil.copyfile(src, dest)
    except Exception as exc:
        return {"ok": False, "message": f"Couldn't copy that file: {exc}"}
    kept = filter_to_youtube(dest)
    config.update({"cookies_file": str(dest), "cookies_from_browser": ""})
    ok, msg = check(str(dest))
    state.update(ok=ok, source="file", checked_at=time.time(),
                 message="ok" if ok else msg)
    _publish()
    if ok:
        bus.publish(Ev.TOAST, "Cookies imported — YouTube access is working")
        return {"ok": True, "kept": kept,
                "message": f"Imported {kept} YouTube cookies from {src.name}. "
                           "YouTube access is working."}
    return {"ok": False, "kept": kept,
            "message": f"Imported {src.name} but YouTube still refused: {msg}"}


def watch_downloads(timeout: int = 300) -> dict:
    """Wait for the user to export cookies, then import them automatically."""
    log.info("watching %s for a cookies export", downloads_dir())
    seen_before = {f: f.stat().st_mtime for f in downloads_dir().glob("*.txt")
                   if f.is_file()} if downloads_dir().is_dir() else {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = scan_downloads(max_age=timeout)
        if found and (found not in seen_before
                      or found.stat().st_mtime > seen_before.get(found, 0)):
            time.sleep(1.0)                    # let the download finish
            return import_file(found)
        time.sleep(2.0)
    return {"ok": False, "message": "No cookies file appeared in Downloads."}


def extension_flow() -> dict:
    """What to tell the user, and start watching for their export."""
    existing = scan_downloads()
    if existing:
        return import_file(existing)
    threading.Thread(target=watch_downloads, daemon=True).start()
    return {
        "ok": False,
        "watching": True,
        "extension_url": EXTENSION_URL,
        "downloads": str(downloads_dir()),
        "message": ("Install “Get cookies.txt LOCALLY”, open youtube.com signed "
                    "in, and click Export. I'm watching your Downloads folder "
                    "and will import it automatically."),
    }


def start_watch() -> None:
    """Boot check, then re-check on a timer."""
    def loop() -> None:
        time.sleep(5)
        while True:
            try:
                if config.get("cookie_auto_refresh", True):
                    ensure()
                    if state.get("ok") is False and \
                            config.get("cookie_close_browser_optin"):
                        for name, image, locks in BROWSERS:
                            if locks and name in installed_browsers() \
                                    and browser_running(image):
                                threading.Thread(target=grab_after_close,
                                                 args=(name,), daemon=True).start()
                                break
            except Exception as exc:
                log.warning("cookie watch: %s", exc)
            time.sleep(max(300, int(config.get("cookie_check_interval", 3600))))

    threading.Thread(target=loop, daemon=True, name="cookies").start()


# ── the automatic route: ask Chrome itself ────────────────────────────
# You can't trigger the export extension from outside, and the cookie file is
# encrypted. But Chrome will hand its own cookies over the devtools protocol,
# because Chrome does the decrypting. It has to be restarted with the debug
# port on, so this is a button, not something we do behind your back.

DEBUG_PORT = 9222


def chrome_exe() -> str | None:
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        p = os.path.join(base, r"Google\Chrome\Application\chrome.exe")
        if base and os.path.isfile(p):
            return p
    return None


def _netscape(rows: list[dict]) -> str:
    out = ["# Netscape HTTP Cookie File", "# Pulled from Chrome by Music Request Server."]
    for c in rows:
        domain = c.get("domain", "")
        if not any(domain.lstrip(".").endswith(d) for d in KEEP_DOMAINS):
            continue
        out.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expires") or 0)) if (c.get("expires") or 0) > 0 else "0",
            c.get("name", ""),
            c.get("value", ""),
        ]))
    return "\n".join(out) + "\n"


async def _fetch_cookies(ws_url: str) -> list[dict]:
    import websockets
    async with websockets.connect(ws_url, max_size=40_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return (msg.get("result") or {}).get("cookies") or []


def grab_via_devtools(restart: bool = True) -> dict:
    """Restart Chrome with the debug port and read its cookies directly."""
    import asyncio

    exe = chrome_exe()
    if not exe:
        return {"ok": False, "message": "Chrome isn't installed here."}

    already = False
    try:
        with urllib_request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version",
                                    timeout=1.5) as r:
            already = True
            info = json.loads(r.read().decode())
            ws_url = info.get("webSocketDebuggerUrl")
    except Exception:
        ws_url = None

    if not already:
        if not restart:
            return {"ok": False, "needs_restart": True,
                    "message": "Chrome has to restart for this. Press again to allow it."}
        if browser_running("chrome.exe"):
            log.info("restarting Chrome with the debug port on")
            _run(["taskkill", "/IM", "chrome.exe", "/T"], timeout=30)
            for _ in range(20):
                if not browser_running("chrome.exe"):
                    break
                time.sleep(0.5)
            time.sleep(1.5)
        try:
            subprocess.Popen([exe, f"--remote-debugging-port={DEBUG_PORT}",
                              "--restore-last-session"],
                             creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return {"ok": False, "message": f"Couldn't start Chrome: {exc}"}
        for _ in range(30):
            time.sleep(0.7)
            try:
                with urllib_request.urlopen(
                        f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1) as r:
                    ws_url = json.loads(r.read().decode()).get("webSocketDebuggerUrl")
                break
            except Exception:
                continue

    if not ws_url:
        return {"ok": False,
                "message": "Chrome didn't open its debug port. Use the export "
                           "extension instead."}
    try:
        rows = asyncio.run(_fetch_cookies(ws_url))
    except Exception as exc:
        return {"ok": False, "message": f"Chrome wouldn't hand them over: {exc}"}

    text = _netscape(rows)
    kept = text.count("\n") - 2
    if kept <= 0:
        return {"ok": False,
                "message": "Chrome had no YouTube cookies — sign into YouTube first."}
    dest = cookie_path()
    dest.write_text(text, encoding="utf-8")
    config.update({"cookies_file": str(dest), "cookies_from_browser": ""})
    ok, msg = check(str(dest))
    state.update(ok=ok, source="chrome-devtools", checked_at=time.time(),
                 message="ok" if ok else msg)
    _publish()
    if ok:
        bus.publish(Ev.TOAST, f"Got {kept} cookies straight from Chrome")
        return {"ok": True, "kept": kept,
                "message": f"Pulled {kept} YouTube cookies from Chrome. Working."}
    return {"ok": False, "kept": kept,
            "message": f"Read {kept} cookies but YouTube still refused: {msg}"}
