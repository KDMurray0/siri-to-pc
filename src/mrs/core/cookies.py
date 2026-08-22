"""Keeping a working YouTube session around.

Tested facts, not guesses:
  * with no cookies, yt-dlp fails with "The page needs to be reloaded";
  * with a valid cookies.txt, a real download succeeds;
  * a *running* Chromium browser locks its cookie database, so extraction says
    "Could not copy ... cookie database";
  * **Chrome v127+ cannot be decrypted even when closed** — "Failed to decrypt
    with DPAPI" (yt-dlp #10927). Closing it costs the user their session and
    still fails, so once we've seen that we record the browser as unreadable
    and never offer to close it again.
  * Firefox has neither problem: readable while open.

Refreshing is therefore opportunistic, and an exported cookies.txt remains the
reliable route on a Chrome-only machine.

Only YouTube/Google cookies are kept — an export contains every site you've
visited, and none of that belongs in this app's data folder.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

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


def find_now(close_browsers: bool = True) -> dict:
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
