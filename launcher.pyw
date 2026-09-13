"""Music Request Server — tray icon + the flyout player window.

Double-click to run. The server runs in a background thread; this process owns
the GUI (pywebview needs the main thread).
"""

from __future__ import annotations

import json
import os
import sys

FROZEN = getattr(sys, "frozen", False)
_HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))

if not FROZEN:
    sys.path.insert(0, os.path.join(_HERE, "src"))

# Test entry points must precede config, GUI, logging and player imports.
if __name__ == "__main__" and any(x in sys.argv for x in ("--check", "--selftest")):
    from mrs.testing import isolated
    with isolated():
        if "--selftest" in sys.argv:
            from mrs.selftest import main as test_main
        else:
            from mrs.checks import main as test_main
        raise SystemExit(test_main())


def _trace(note: str) -> None:
    """A line in the log using nothing but the standard library.

    mark() lives in mrs.logging_setup, which is imported after webview, PIL
    and pystray — so a copy that dies or hangs while loading those writes
    nothing at all, anywhere. That is precisely the copy that starts at
    sign-in and never serves, and it is why four attempts to diagnose it
    found an empty log and a bound port.

    This runs before any of that. It computes the path by hand, uses no
    logging machinery, and cannot fail in a way that matters.
    """
    import time as _t
    line = (f"{_t.strftime('%H:%M:%S')} ----    trace"
            f"          {note} (pid {os.getpid()})\n")
    # Two files, in two genuinely different places, every time — not one
    # with a fallback. A boot launched from the shell writes a full trace
    # and a boot launched by double-clicking the exe writes nothing at all,
    # and no amount of reading the first file explains the second. The one
    # beside the exe is the tie-breaker: it is not under AppData, so nothing
    # that redirects a user profile — a packaged app's container, a roaming
    # profile, a sandbox — can quietly send it somewhere else. Whichever of
    # the two exists after a bad boot, one of them is the truth.
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    for target in (os.path.join(base, "MusicRequestServer", "server.log"),
                   os.path.join(_HERE, "boot-trace.log")):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        except Exception:
            continue


_trace(f"process start, argv={sys.argv[1:]}, frozen={FROZEN}")


def _reexec_if_needed() -> bool:
    """Relaunch under the interpreter that actually has pywebview."""
    if FROZEN:
        return False
    try:
        import webview  # noqa: F401
        return False
    except Exception:
        pass
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        cfg_path = os.path.join(base, "MusicRequestServer", "config.json")
        py = ""
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8-sig") as f:
                py = (json.load(f).get("python_path") or "")
        pyw = py.replace("python.exe", "pythonw.exe")
        if os.path.exists(pyw):
            py = pyw
        if py and os.path.abspath(py) != os.path.abspath(sys.executable):
            import subprocess
            subprocess.Popen([py, os.path.abspath(__file__)] + sys.argv[1:])
            return True
    except Exception:
        pass
    return False


if _reexec_if_needed():
    sys.exit(0)

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from urllib import request as urlrequest
from urllib.parse import urlparse

_trace("importing webview")
import webview
_trace("importing pillow")
from PIL import Image, ImageDraw
_trace("importing pystray")
from pystray import Icon as TrayIcon
from pystray import Menu, MenuItem

_trace("importing mrs")
from mrs.config import config
from mrs.logging_setup import get, log_path, mark, tail
from mrs import server as srv
_trace("imports done")

log = get("launcher")

U32 = ctypes.windll.user32
U32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                            wintypes.LPCWSTR, wintypes.UINT]
U32.GetWindowLongW.restype = ctypes.c_long
U32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
U32.SetWindowLongW.restype = ctypes.c_long
U32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
U32.MonitorFromWindow.restype = ctypes.c_void_p
U32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]

GWL_EXSTYLE = -20
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
LWA_ALPHA = 2
TITLE = "Music Request"
# Set when Quit is chosen, so the tray keep-alive loop knows the icon
# went away on purpose.
_tray_quit = threading.Event()
# Set when the flyout died on its own. The server carries on without it;
# the tray has to stop offering to show a window that isn't there.
_window_gone = threading.Event()


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class Flyout:
    W, H = 400, 640
    MINI_W = 344
    MINI_IDLE_H = 80
    MINI_HOVER_H = 108

    def __init__(self) -> None:
        self.window = None
        self._hwnd = None
        self._visible = True
        self._shown_at = time.monotonic()
        self._pinned = False
        self._moving = False
        self._mini = False
        self._resize_gen = 0
        self._ever_focused = False   # don't auto-hide before you've used it
        self._fs_active = False
        self._click_through = False
        self._force_interactive = False

    # -- window handle --
    def hwnd(self):
        if self._hwnd:
            return self._hwnd
        h = U32.FindWindowW(None, TITLE)
        if not h:
            # FindWindowW misses it occasionally; walk the list instead.
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def cb(hwnd, _l):
                buf = ctypes.create_unicode_buffer(256)
                U32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == TITLE:
                    found.append(hwnd)
                    return False
                return True

            U32.EnumWindows(cb, None)
            h = found[0] if found else None
        self._hwnd = h or None
        return self._hwnd

    # -- JS bridge --
    def on_blur(self) -> None:
        if self._pinned or self._fs_active:
            return
        if not os.environ.get("MRS_NO_AUTOHIDE"):
            self.hide()

    def set_pinned(self, on) -> bool:
        self._pinned = bool(on)
        if self._fs_active and self._pinned:
            return self._pinned
        try:
            if self._pinned and self.window:
                self.window.show()
                self._visible = True
            self._topmost(self._pinned)
        except Exception:
            pass
        return self._pinned

    def begin_move(self) -> None:
        # WebView2 ignores -webkit-app-region: drag, so move the window ourselves.
        if self._moving:
            return
        self._moving = True
        threading.Thread(target=self._move_loop, daemon=True).start()

    def _move_loop(self) -> None:
        try:
            h = self.hwnd()
            if not h:
                return
            pt = wintypes.POINT()
            U32.GetCursorPos(ctypes.byref(pt))
            r = wintypes.RECT()
            U32.GetWindowRect(h, ctypes.byref(r))
            ox, oy = pt.x - r.left, pt.y - r.top
            while U32.GetAsyncKeyState(0x01) & 0x8000:
                U32.GetCursorPos(ctypes.byref(pt))
                U32.GetWindowRect(h, ctypes.byref(r))
                x, y = self._clamp_drag(pt.x - ox, pt.y - oy,
                                        r.right - r.left, r.bottom - r.top)
                U32.SetWindowPos(h, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
                time.sleep(0.008)
        except Exception:
            pass
        finally:
            self._moving = False

    # -- sizing (anchored to the top middle) --
    def _rect(self):
        h = self.hwnd()
        r = wintypes.RECT()
        if h:
            U32.GetWindowRect(h, ctypes.byref(r))
        return r

    def _work_area(self):
        """Usable desktop for our monitor (excludes the taskbar)."""
        h = self.hwnd()
        mon = U32.MonitorFromWindow(ctypes.c_void_p(h) if h else None, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if mon and U32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            return mi.rcWork
        r = wintypes.RECT()
        r.left, r.top = 0, 0
        r.right = U32.GetSystemMetrics(0)
        r.bottom = U32.GetSystemMetrics(1)
        return r

    def _clamp(self, x: int, y: int, w: int, h: int):
        """Never let the window sit off-screen; push it back from the edge."""
        wa = self._work_area()
        x = min(max(x, wa.left), max(wa.left, wa.right - w))
        y = min(max(y, wa.top), max(wa.top, wa.bottom - h))
        return x, y

    @staticmethod
    def _desktop():
        """Every monitor together, not just the one we happen to be on."""
        r = wintypes.RECT()
        r.left = U32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        r.top = U32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        r.right = r.left + U32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        r.bottom = r.top + U32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if r.right <= r.left or r.bottom <= r.top:      # single monitor
            r.left, r.top = 0, 0
            r.right, r.bottom = U32.GetSystemMetrics(0), U32.GetSystemMetrics(1)
        return r

    def _clamp_drag(self, x: int, y: int, w: int, h: int):
        """Dragging is allowed to cross monitors.

        Clamping to the current monitor's work area meant the window stopped
        dead at the edge of the screen it started on and could never be moved
        to the other one. Bound it to the whole desktop instead, and let it
        overhang as long as enough stays on screen to grab hold of.
        """
        d = self._desktop()
        edge = max(60, min(160, w // 3))
        x = min(max(x, d.left - (w - edge)), d.right - edge)
        y = min(max(y, d.top), d.bottom - 28)
        return x, y

    def _resize_from_top(self, tw: int, th: int, dur: float = 0.16, steps: int = 10) -> None:
        """Grow and shrink from the top middle, clamped to the screen.

        The top edge stays put and the width opens out either side of the
        centre. Anchoring the centre instead meant every hover moved the
        window both ways at once, so the thing you were reaching for slid out
        from under the pointer — and the title, the artwork and the controls
        all sat somewhere new each time. With the top pinned, everything you
        actually look at holds still and the window only ever grows downwards.

        It also removes a whole class of drift: half of "the height we got
        isn't the height we asked for" was being turned into vertical
        movement by the centring maths.
        """
        self._resize_gen += 1
        gen = self._resize_gen
        h = self.hwnd()
        if not h:
            return
        r = self._rect()
        cw, ch = r.right - r.left, r.bottom - r.top
        cx, top = r.left + cw / 2, r.top

        def place(w: int, ht: int) -> tuple[int, int]:
            return self._clamp(int(round(cx - w / 2)), int(top), w, ht)

        def run() -> None:
            for i in range(1, steps + 1):
                if gen != self._resize_gen:
                    return
                f = i / steps
                w = int(cw + (tw - cw) * f)
                ht = int(ch + (th - ch) * f)
                x, y = place(w, ht)
                try:
                    U32.SetWindowPos(h, 0, x, y, w, ht, SWP_NOZORDER | SWP_NOACTIVATE)
                except Exception:
                    return
                time.sleep(dur / steps)

            # A window can refuse a size, so settle on what it actually became
            # rather than on what was asked for.
            if gen != self._resize_gen:
                return
            got = self._rect()
            gw, gh = got.right - got.left, got.bottom - got.top
            fx, fy = place(gw, gh)
            if (fx, fy) != (got.left, got.top):
                try:
                    U32.SetWindowPos(h, 0, fx, fy, gw, gh,
                                     SWP_NOZORDER | SWP_NOACTIVATE)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def set_mini(self, on) -> bool:
        self._mini = bool(on)
        if on:
            self._resize_from_top(Flyout.MINI_W, Flyout.MINI_HOVER_H)
        else:
            self._resize_from_top(Flyout.W, Flyout.H, dur=0.2)
        return bool(on)

    def set_mini_hover(self, on) -> bool:
        if not self._mini:
            return False
        self._resize_from_top(Flyout.MINI_W,
                              Flyout.MINI_HOVER_H if on else Flyout.MINI_IDLE_H)
        return bool(on)

    # -- visibility --
    def show(self) -> None:
        try:
            self.window.show()
            self._visible = True
            self._ever_focused = False
            self._shown_at = time.monotonic()
            h = self.hwnd()
            if h:
                U32.SetForegroundWindow(h)
        except Exception:
            pass

    def hide(self) -> None:
        try:
            self.window.hide()
        except Exception:
            pass
        self._visible = False

    def toggle(self) -> None:
        self.show() if not self._visible else self.hide()

    def _topmost(self, on: bool) -> None:
        h = self.hwnd()
        if h:
            U32.SetWindowPos(h, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                             SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def round_corners(self) -> None:
        try:
            h = self.hwnd()
            if h:
                val = ctypes.c_int(2)   # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(h, 33, ctypes.byref(val),
                                                           ctypes.sizeof(val))
        except Exception:
            pass

    def hide_from_taskbar(self) -> None:
        try:
            h = self.hwnd()
            if not h:
                return
            style = U32.GetWindowLongW(h, GWL_EXSTYLE)
            U32.SetWindowLongW(h, GWL_EXSTYLE,
                               (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            if self._visible:
                U32.ShowWindow(h, 0)
                U32.ShowWindow(h, 5)
                U32.SetForegroundWindow(h)
        except Exception:
            pass

    # -- watchers --
    def focus_watch(self) -> None:
        """Hide when you click away — but only once you've actually been on it.

        A freshly launched flyout often never wins focus, and since it has no
        taskbar button, hiding at that point makes the app look like it didn't
        start at all.
        """
        while True:
            time.sleep(0.25)
            try:
                if self._pinned or self._fs_active or os.environ.get("MRS_NO_AUTOHIDE"):
                    continue
                if not self._visible:
                    continue
                h = self.hwnd()
                if not h:
                    continue
                focused = U32.GetForegroundWindow() == h
                if focused:
                    self._ever_focused = True
                    continue
                if not self._ever_focused:
                    continue                     # never been used; leave it up
                if (time.monotonic() - self._shown_at) > 0.6:
                    self.hide()
            except Exception:
                pass

    def _foreground_is_fullscreen(self) -> bool:
        fg = U32.GetForegroundWindow()
        if not fg:
            return False
        h = self.hwnd()
        if h and fg == ctypes.c_void_p(h).value:
            return False
        buf = ctypes.create_unicode_buffer(256)
        U32.GetClassNameW(fg, buf, 256)
        if buf.value in ("WorkerW", "Progman", "Shell_TrayWnd",
                         "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"):
            return False
        r = wintypes.RECT()
        U32.GetWindowRect(fg, ctypes.byref(r))
        mon = U32.MonitorFromWindow(fg, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        U32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        m = mi.rcMonitor
        covers = (r.left <= m.left and r.top <= m.top
                  and r.right >= m.right and r.bottom >= m.bottom)
        if not covers:
            return False
        # only yield on OUR monitor; fullscreen elsewhere is irrelevant
        if h:
            ours = U32.MonitorFromWindow(ctypes.c_void_p(h), 2)
            if ours and mon and ours != mon:
                return False
        return True

    def set_click_through(self, on: bool) -> None:
        """Overlay mode: visible and on top, but the cursor passes through."""
        if on == self._click_through:
            return
        try:
            h = self.hwnd()
            if not h:
                return
            style = U32.GetWindowLongW(h, GWL_EXSTYLE)
            if on:
                U32.SetWindowLongW(h, GWL_EXSTYLE,
                                   style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
                U32.SetLayeredWindowAttributes(h, 0, 255, LWA_ALPHA)
                self._topmost(True)
            else:
                U32.SetWindowLongW(h, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
            self._click_through = on
        except Exception:
            pass

    def fullscreen_watch(self) -> None:
        while True:
            time.sleep(0.7)
            try:
                fs = self._foreground_is_fullscreen()
                if fs and not self._fs_active:
                    self._fs_active = True
                    if not self._force_interactive:
                        self.set_click_through(True)
                elif fs and self._fs_active and self._click_through:
                    self._topmost(True)      # borderless apps keep re-raising
                elif not fs and self._fs_active:
                    self._fs_active = False
                    self._force_interactive = False
                    self.set_click_through(False)
                    self._topmost(self._pinned)
            except Exception:
                pass

    def toggle_interactive(self) -> None:
        if not self._fs_active:
            return
        self._force_interactive = not self._force_interactive
        self.set_click_through(not self._force_interactive)
        if self._force_interactive:
            try:
                U32.SetForegroundWindow(ctypes.c_void_p(self.hwnd()))
            except Exception:
                pass

    def hotkey_watch(self) -> None:
        prev = False
        while True:
            time.sleep(0.05)
            try:
                down = ((U32.GetAsyncKeyState(0x11) & 0x8000) and   # Ctrl
                        (U32.GetAsyncKeyState(0x12) & 0x8000) and   # Alt
                        (U32.GetAsyncKeyState(0x4D) & 0x8000))      # M
                if down and not prev:
                    self.toggle_interactive()
                prev = bool(down)
            except Exception:
                pass


class Bridge:
    """Deliberately tiny and window-free.

    Handing pywebview an object that references the window makes it walk the
    WebView2 COM object and flood the log with recursion errors.
    """

    def on_blur(self):
        if flyout:
            flyout.on_blur()

    def set_pinned(self, on):
        return flyout.set_pinned(on) if flyout else False

    def begin_move(self):
        if flyout:
            flyout.begin_move()

    def set_mini(self, on):
        return flyout.set_mini(on) if flyout else False

    def set_mini_hover(self, on):
        return flyout.set_mini_hover(on) if flyout else False

    def sign_in(self):
        threading.Thread(target=sign_in_window, daemon=True, name="signin").start()
        return True

    def open_external(self, url):
        """Open a link in the user's actual browser.

        window.open() inside the flyout spawns a bare WebView2 popup with no
        address bar and no tabs, which shows for a moment and goes again —
        the little window that disappears. The Chrome Web Store and a
        Last.fm approval page both want a real browser.
        """
        import webbrowser
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False


flyout: Flyout | None = None

# Straight to YouTube, not to a Google login form. You press "Sign in"
# yourself, in your own time — the old flow drove the login itself and closed
# the moment it thought it was finished, which was usually too early.
SIGNIN_URL = "https://www.youtube.com/"
# where the cookies we need actually live
COOKIE_STOPS = ("https://music.youtube.com/", "https://www.youtube.com/",
                "https://accounts.google.com/")

SIGNIN_POLL = 3.0          # seconds between "are we signed in yet" checks
SIGNIN_GIVE_UP = 15 * 60   # stop watching after this long


def sign_in_window() -> None:
    """Open YouTube and wait. No clock, no driving the form.

    We can't decrypt somebody else's cookie jar, but we can own the browser
    that makes them — so this opens YouTube in our own WebView2 and simply
    watches for the auth cookies to appear. Sign in whenever you like, take
    as long as you like; the window closes itself once the cookies are real,
    and if you close it first we take whatever is there on the way out.
    """
    from mrs.core import cookies as ck

    win = webview.create_window("Sign in to YouTube", url=SIGNIN_URL,
                                width=980, height=760)

    done = threading.Event()

    def _read_cookies() -> str:
        """Whatever the window currently holds, without navigating anywhere.

        Navigating to collect is what made this feel like YouTube was opening
        by itself, so the watcher only reads the page you're already on. The
        sweep across the other origins happens once, at the end.
        """
        try:
            jars = list(win.get_cookies() or [])
        except Exception:
            return ""
        return ck.from_webview(jars)

    def _sweep() -> str:
        """The full pass, once we know there's something worth collecting."""
        jars, seen = [], set()
        for url in COOKIE_STOPS:
            try:
                win.load_url(url)
                time.sleep(1.8)
                for jar in win.get_cookies() or []:
                    for name in (jar.keys() if hasattr(jar, "keys") else []):
                        tag = ((jar[name].get("domain") or ""), name)
                        if tag in seen:
                            continue
                        seen.add(tag)
                        jars.append(jar)
            except Exception as exc:
                log.debug("no cookies from %s: %s", url, exc)
        return ck.from_webview(jars)

    def finish(reason: str) -> None:
        if done.is_set():
            return
        done.set()
        saved = 0
        try:
            text = _sweep()
            found = [n for n in ck.AUTH_COOKIES if f"	{n}	" in text]
            if found:
                ck.save_master(text)
                saved = max(0, len(text.splitlines()) - 3)
                log.info("signed in — saved %d cookies (%s)", saved,
                         ", ".join(found))
            else:
                log.warning("sign-in finished with no auth cookies (%s)", reason)
        except Exception as exc:
            log.warning("sign-in collect failed: %s", exc)
        finally:
            try:
                win.destroy()
            except Exception:
                pass
            _api(f"/api/cookies/signedin?saved={saved}")

    def watch() -> None:
        """Poll until the auth cookies turn up. Nothing is timed but this."""
        deadline = time.time() + SIGNIN_GIVE_UP
        while not done.is_set() and time.time() < deadline:
            time.sleep(SIGNIN_POLL)
            if done.is_set():
                return
            text = _read_cookies()
            if any(f"	{n}	" in text for n in ck.AUTH_COOKIES):
                log.info("auth cookies appeared — collecting")
                finish("signed in")
                return
        if not done.is_set():
            log.info("sign-in window open %d minutes with no login — leaving it",
                     SIGNIN_GIVE_UP // 60)

    # Closing the window yourself counts as "done" — take what's there.
    try:
        win.events.closing += lambda: finish("window closed")
    except Exception:
        pass

    threading.Thread(target=watch, daemon=True, name="cookies-watch").start()




def _icon_image():
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (109, 139, 255, 255)
    d.ellipse([8, 24, 16, 30], fill=c)
    d.rectangle([15, 8, 17, 26], fill=c)
    d.polygon([(17, 8), (26, 14), (26, 16), (17, 20)], fill=c)
    return img


def _api(path: str) -> None:
    port = int(srv.runtime.get("port") or config.get("port", 7420))
    key = config.get("api_key", "")
    sep = "&" if "?" in path else "?"
    try:
        srv.open_local(srv.local_url(port, f"{path}{sep}key={key}"), timeout=5)
    except Exception:
        pass


def _tray() -> None:
    def show(_i, _it):
        # The window can go without the app going. Offering to show one that
        # was destroyed is a dead menu item on a program that is otherwise
        # working perfectly, so hand over to the browser instead.
        if _window_gone.is_set():
            desktop(_i, _it)
            return
        flyout.show()

    def restart_player(_i, _it):
        _api("/api/restart")

    def find_cookies(_i, _it):
        _api("/api/cookies/find")

    def browser(_i, _it):
        import webbrowser
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            host = s.getsockname()[0]
            s.close()
        except Exception:
            host = "127.0.0.1"
        port = int(srv.runtime.get("port") or config.get("port", 7420))
        webbrowser.open(f"http://{host}:{port}/?key={config.get('api_key','')}")

    def desktop(_i, _it):
        # The flyout is a fixed 400px window on purpose, so the wide layout
        # can't be reached by dragging it — a browser is the only way in.
        import webbrowser
        port = int(srv.runtime.get("port") or config.get("port", 7420))
        webbrowser.open(srv.local_url(
            port, f"/player?key={config.get('api_key', '')}"))

    def quit_(icon, _it):
        _tray_quit.set()          # so the keep-alive loop doesn't rebuild it
        _trace("quit chosen from the tray")
        try:
            from mrs.player import player
            player.stop()
        except Exception:
            pass
        icon.stop()
        try:
            flyout.window.destroy()
        except Exception:
            pass
        os._exit(0)

    menu = Menu(
        MenuItem("Show player", show, default=True),
        MenuItem("Open desktop player", desktop),
        MenuItem("Restart player", restart_player),
        MenuItem("Find cookies", find_cookies),
        Menu.SEPARATOR,
        MenuItem("Open on my phone", browser),
        Menu.SEPARATOR,
        MenuItem("Quit", quit_),
    )
    # Keep trying. Shell_NotifyIcon fails outright if the taskbar isn't ready
    # yet, which is exactly the case when this starts with Windows — and it
    # failed in a daemon thread with nothing caught and nothing logged, so the
    # app ran perfectly with no way to reach it. Explorer restarting takes the
    # icon away the same way, and that wants the same answer: put it back.
    attempt = 0
    while True:
        attempt += 1
        try:
            icon = TrayIcon("Music Request Server", _icon_image(), menu=menu)
            if attempt > 1:
                log.info("tray icon back after %d attempts", attempt)
            icon.run()
            # run() returning without Quit means the shell took it away.
            if _tray_quit.is_set():
                return
            log.warning("tray icon vanished — putting it back")
        except Exception as exc:
            log.warning("tray icon failed (attempt %d): %s", attempt, exc)
        if _tray_quit.is_set():
            return
        # Backs off to half a minute: a logon race clears in seconds, a
        # genuinely broken shell shouldn't be hammered all day.
        time.sleep(min(30.0, 2.0 * attempt))



def _headless_marker():
    from mrs.paths import data_dir
    return data_dir() / "headless.pid"


def _port_busy(port: int) -> bool:
    """Is anything at all holding this port?

    Asked by trying to bind it, not by asking it a question. A server part
    way through starting answers a connection and closes it, which is
    indistinguishable from an empty port to anything that expects a reply —
    and getting that wrong means starting a second copy on a different port.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # No SO_REUSEADDR: the question is "could the server have this",
        # and the server does not set it either.
        try:
            sock.bind(("0.0.0.0", int(port)))
            return False
        except OSError:
            return True


def _port_owner(port: int) -> int:
    """Which process is listening on this port, asked of Windows directly.

    The stand-down handover identifies the other copy by a marker file it
    writes about itself. Every boot failure so far has been a copy that
    wrote no marker and no log — reproduced on demand: the before-sign-in
    task starts a copy in session 0 that binds this port, serves nothing,
    and leaves not one line anywhere. Against that, a marker is no way to
    identify anything. The socket table always knows who has the port.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception as exc:
        log.debug("couldn't read the socket table: %s", exc)
        return 0
    for line in out.splitlines():
        bits = line.split()
        if len(bits) >= 5 and bits[3].upper() == "LISTENING" \
                and bits[1].rsplit(":", 1)[-1] == str(port):
            try:
                return int(bits[-1])
            except ValueError:
                continue
    return 0


def _seize_port(port: int) -> bool:
    """Take the port off a copy that is holding it and answering nothing.

    Being polite here is what has broken every boot since the app learned to
    start before sign-in. The session-0 copy binds the port and wedges; the
    copy that actually has a desktop, speakers and a tray finds the port
    taken, cannot identify the squatter, and exits — so signing in produces
    no player at all, and the only way back is to notice and start it by
    hand. A copy that serves nothing has no claim on the port.
    """
    # End the task before killing anything. Killing its process leaves
    # Windows counting the task as failed, and a task with a restart policy
    # answers a failure by starting the squatter again a minute later.
    try:
        subprocess.run(["schtasks", "/End", "/TN",
                        "MusicRequestServer-BeforeSignIn"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        log.debug("couldn't end the boot task: %s", exc)
    for _ in range(20):
        if not _port_busy(port):
            mark("the boot task ended and let the port go")
            return True
        time.sleep(0.25)

    pid = _port_owner(port)
    if not pid or pid == os.getpid():
        return not _port_busy(port)
    mark(f"pid {pid} is holding {port} and not answering — stopping it")
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        log.warning("couldn't stop pid %s: %s", pid, exc)
        return False
    for _ in range(80):
        if not _port_busy(port):
            return True
        time.sleep(0.25)
    return False


def _standdown_flag():
    """The file that asks a headless copy to bow out.

    Watched rather than signalled: the two copies are in different Windows
    sessions, and a limited scheduled task cannot create the Global event
    that would cross one. A file in a folder both can already write is the
    thing that works everywhere without asking for a privilege.
    """
    from mrs.paths import data_dir
    return data_dir() / "standdown"


def _clear_standdown() -> None:
    try:
        _standdown_flag().unlink(missing_ok=True)
    except Exception:
        pass


def _stand_down_headless() -> bool:
    """Stop a copy that's been serving since before anyone signed in.

    It holds the mutex, the port and the mpv pipes, and it can't play to the
    speakers — nothing in session 0 can, Windows gives it no audio device. So
    when somebody actually signs in, the copy with a desktop takes over. The
    handover is a file naming a pid rather than a shutdown route on the web
    server: this is our own process, on this machine, and adding a way to ask
    an HTTP server to kill itself is a bigger thing to own than a pid file.
    """
    port = config.get("port", 5000)
    marker = _headless_marker()
    try:
        pid = int(marker.read_text(encoding="utf-8").strip())
    except Exception:
        # No marker is not the same as nobody there. The copies that need
        # displacing most are exactly the ones too broken to have written
        # one — so ask the socket table who has the port instead of giving
        # up, which is what used to happen, one line into the handover.
        pid = _port_owner(port)
        if not pid or pid == os.getpid():
            return False
        mark(f"no headless marker; the port is held by pid {pid}")
    if pid == os.getpid():
        return False

    # Ask first. Killing it works and costs something: Windows records the
    # task as terminated (0x41306), and a task with RestartCount set treats
    # that as a failure and starts it again a minute later — so the copy we
    # just got rid of comes back, finds the port taken, exits, and is
    # counted as having failed again. A file it watches for lets it shut
    # down and exit cleanly, which Windows records as a task that finished.
    #
    # A file rather than an event because the two live in different
    # sessions and a limited task cannot create a Global\ object.
    try:
        _standdown_flag().write_text(str(os.getpid()), encoding="utf-8")
        mark(f"asked the headless copy (pid {pid}) to stand down")
    except Exception as exc:
        log.debug("couldn't write the stand-down flag: %s", exc)

    for _ in range(60):                       # fifteen seconds of asking
        if not _port_busy(port):
            mark("the headless copy stood down on its own")
            _clear_standdown()
            try:
                marker.unlink()
            except Exception:
                pass
            return True
        time.sleep(0.25)

    # It didn't go. Take the port anyway — an unattended machine that never
    # gets its speakers back is worse than a task marked as terminated.
    mark(f"headless copy didn't stand down; stopping it (pid {pid})")
    _clear_standdown()
    # End the task properly first. Killing its process leaves Windows
    # thinking the task failed, and a task with RestartCount set answers a
    # failure by starting it again a minute later — so the copy just got
    # rid of comes back and takes the port a second time.
    try:
        subprocess.run(["schtasks", "/End", "/TN",
                        "MusicRequestServer-BeforeSignIn"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(20):
            if not _port_busy(port):
                mark("the task ended and let the port go")
                try:
                    marker.unlink()
                except Exception:
                    pass
                return True
            time.sleep(0.25)
    except Exception as exc:
        log.debug("couldn't end the task: %s", exc)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        log.warning("couldn't stop the headless copy (pid %s): %s", pid, exc)
        return False
    try:
        marker.unlink()
    except Exception:
        pass
    # Wait for it to actually be gone, by asking the port rather than the
    # mutex. Probing with _singleton() *takes* the mutex, so the caller's
    # next check found the handle this process had just made for itself and
    # concluded somebody else had it.
    for _ in range(80):
        if not _port_busy(port):
            break
        time.sleep(0.25)
    mark(f"took over from the headless copy (pid {pid})")
    return not _port_busy(port)


def _watch_serving(port: int) -> None:
    """Never hold the port while serving nothing.

    This is the state behind every report of the program being broken: a
    process alive, the socket bound, and every request accepted and closed
    without an answer. From outside it is indistinguishable from the machine
    being off, and because the port is taken, the next copy to start cannot
    fix it either.

    So the copy that holds the port keeps asking itself the same question
    anyone else would. Three minutes of no answer and it gets out of the
    way — dying is recoverable, because start-at-sign-in and the tray both
    bring it back, and squatting is not.
    """
    missed = 0
    while True:
        time.sleep(30)
        if srv._is_ours(port):
            missed = 0
            continue
        missed += 1
        _trace(f"not answering on {port} ({missed}/6)")
        if missed >= 6:
            mark(f"holding {port} and serving nothing — standing down")
            _trace("exiting so something that works can have the port")
            try:
                from mrs.player import player as _p
                _p.stop()
            except Exception:
                pass
            os._exit(1)


def _serve_with_retry(tries: int = 6, gap: float = 20.0):
    """Start the server, and keep trying if the machine wasn't ready.

    Boot is the hostile case. The task fires the moment Windows will let it,
    which is before the network stack has an address, before DNS answers and
    before half the services this talks to exist. A single attempt that
    lands in that window fails for a reason that has stopped being true
    thirty seconds later — and the old behaviour was to put an error on
    screen and stay broken until somebody noticed.

    Returns (thread, port, ready).
    """
    port = config.get("port", 5000)
    thread = None
    for attempt in range(1, tries + 1):
        if thread is None or not thread.is_alive():
            srv.runtime.pop("error", None)
            thread = srv.run_in_thread()
        for _ in range(60):
            if srv.runtime.get("port"):
                port = srv.runtime["port"]
                break
            time.sleep(0.5)
        if _wait_for_server(port):
            if attempt > 1:
                mark(f"came up on attempt {attempt}, port {port}")
            return thread, port, True
        why = srv.runtime.get("error") or "no reason recorded"
        if attempt >= tries:
            mark(f"gave up after {attempt} attempts: {why}")
            return thread, port, False
        mark(f"attempt {attempt} didn't come up ({why}) — again in {gap:g}s")
        time.sleep(gap)
    return thread, port, False


def _run_headless() -> None:
    """Serve, with no desktop to put anything on.

    This is what runs before anyone signs in. There is no tray icon and no
    window because there is no session to show them in; links work, and the
    computer's own speakers do not, because session 0 has no audio device to
    give mpv. Signing in starts the normal copy, which takes over.
    """
    # A deadline nothing can talk its way out of. The rest of this function
    # already refuses to squat — but only along the paths it reaches, and the
    # copy that actually breaks boot reaches none of them: it binds the port,
    # serves nothing, writes not one line anywhere, and sits there until
    # somebody notices days later. This runs on its own thread and ends the
    # process with os._exit, so it needs nothing else to be working.
    def deadline() -> None:
        end = time.monotonic() + 180.0
        while time.monotonic() < end:
            time.sleep(5.0)
            if srv._is_ours(config.get("port", 5000)):
                return                    # answering; it can look after itself
        mark("headless: three minutes without answering — quitting so the "
             "port is free for a copy that can serve")
        os._exit(3)

    threading.Thread(target=deadline, daemon=True,
                     name="headless-deadline").start()
    mark(f"headless: starting, exe={sys.executable}")
    try:
        from mrs.paths import data_dir
        mark(f"headless: data dir {data_dir()}")
    except Exception as exc:
        mark(f"headless: no data dir — {exc}")
    if _singleton() is None:
        mark("headless: something else already holds the mutex — stopping")
        sys.exit(0)
    try:
        _headless_marker().write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        log.warning("couldn't write the headless marker: %s", exc)
    thread, port, ready = _serve_with_retry()
    if ready:
        mark(f"headless: serving on {port}")
        log.info("running headless on port %s — no desktop, so no tray and "
                 "no sound out of this computer; links play on their own "
                 "devices as usual", port)
    else:
        # Never squat. A copy that holds the port and answers nothing is
        # worse than no copy at all: the desktop copy cannot bind, the
        # links all point at a socket that accepts and closes, and from
        # outside it is indistinguishable from the machine being broken.
        # This is the state that produced "post json doesn't work", "it
        # randomly crashed" and "boot doesn't work" — one wedged process,
        # three symptoms.
        mark("headless: the server never came up — letting go rather than "
             "sitting on the port")
        log.error("headless: server did not come up (%s) — exiting so the "
                  "port is free for a copy that can serve",
                  srv.runtime.get("error") or "no reason recorded")
        try:
            _headless_marker().unlink(missing_ok=True)
        except Exception:
            pass
        try:
            from mrs.player import player as _p
            _p.stop()
        except Exception:
            pass
        sys.exit(1)
    # Somebody signing in means a copy that can reach the speakers is
    # starting. Going quietly, and exiting zero, is what stops Windows
    # counting the handover as the task failing and starting it again.
    _clear_standdown()
    # And keep proving it. A server that stops answering while still
    # holding the socket is the same wedge arriving later — the process is
    # alive, the port is bound, and nothing is served. Checked against our
    # own ping, which is the same question anyone else would ask.
    missed = 0
    try:
        while thread.is_alive():
            if _standdown_flag().exists():
                mark("headless: asked to stand down — shutting down cleanly")
                break
            missed = 0 if srv._is_ours(port) else missed + 1
            if missed >= 5:
                mark("headless: stopped answering for a minute — letting go")
                log.error("headless: holding the port and serving nothing; "
                          "exiting so something else can")
                break
            thread.join(timeout=12.0)
    finally:
        try:
            _headless_marker().unlink()
        except Exception:
            pass
        _clear_standdown()
        try:
            from mrs.player import player as _p
            _p.stop()          # let go of mpv and the pipes before exiting
        except Exception:
            pass
    mark("headless: stopped")
    sys.exit(0)


def _singleton():
    """Named mutex so a second launch can't fight over mpv and the port."""
    try:
        # use_last_error, or the windowed bootloader's stale 183 makes the only
        # instance think it's a duplicate and exit immediately.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, "MusicRequestServer_singleton")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            # Let go of it. CreateMutexW hands back a handle even when it's
            # somebody else's mutex, and keeping that handle keeps the mutex
            # alive after its owner has gone — so having found the headless
            # copy holding it, killed that copy and asked again, we were
            # answered by the reference we ourselves had left behind.
            if h:
                k32.CloseHandle(ctypes.c_void_p(h))
            return None
        return h or True
    except Exception:
        return True


def _wait_for_server(port: int, timeout: int = 60) -> bool:
    """Wait for OUR server — a foreign one on the same port doesn't count."""
    # srv._is_ours asks in both schemes. Asking only in http here is what
    # made "turn HTTPS on" mean "the app never finishes starting" — the
    # server was up and answering the whole time, in TLS, to a question
    # always asked in plaintext.
    end = time.time() + timeout
    while time.time() < end:
        if srv._is_ours(port):
            return True
        time.sleep(0.5)
    return False


def _after_start() -> None:
    if not flyout._visible:
        flyout.hide()
        try:
            h = flyout.hwnd()
            if h:
                U32.ShowWindow(h, 0)
        except Exception:
            pass
    time.sleep(0.4)
    flyout.round_corners()
    flyout.hide_from_taskbar()
    if flyout._visible:
        # a freshly launched window that never gets focus looks like a dead app
        try:
            h = flyout.hwnd()
            if h:
                U32.ShowWindow(h, 5)
                U32.SetForegroundWindow(h)
                flyout._shown_at = time.monotonic()
        except Exception:
            pass
    def guarded(fn):
        # A daemon thread that raises takes its reason with it. These are the
        # window watchers and the tray; losing one silently is how the app
        # ends up running with no way to reach it.
        def go():
            try:
                fn()
            except Exception:
                log.exception("%s stopped", getattr(fn, "__name__", fn))
        return go

    for fn in (flyout.focus_watch, flyout.fullscreen_watch, flyout.hotkey_watch, _tray):
        threading.Thread(target=guarded(fn), daemon=True,
                         name=getattr(fn, "__name__", "watch")).start()


def main() -> None:
    global flyout
    # Check the build rather than the source. Nothing else in the test suite
    # runs inside the frozen bundle, which is where the interesting failures
    # live: a hidden import PyInstaller didn't spot, a template it didn't
    # collect, a path that resolves differently once packed.
    if "--selftest" in sys.argv:
        from mrs.selftest import main as selftest
        sys.exit(selftest())

    # The access rules on their own, for when that's the bit you changed.
    if "--check" in sys.argv:
        from mrs.checks import main as checks
        sys.exit(checks())

    # No desktop to draw on: serve and nothing else.
    if "--headless" in sys.argv:
        _run_headless()
        return

    mark("starting")
    _trace("main() reached")
    # The port, before the mutex, because the mutex cannot see across a
    # session boundary and this is exactly where one is.
    #
    # Windows puts an unprefixed kernel object name in the caller's own
    # session namespace. The before-sign-in task runs in session 0 and the
    # desktop copy in session 1, so each was creating a *different* mutex of
    # the same name, each was satisfied it was the only one, and the copy
    # that had been serving since boot went on holding the port. The desktop
    # copy then moved to a free one — which is every link anybody has been
    # given pointing at the wrong number. A Global\ name would collide
    # properly and a limited task may not create one, so the thing that
    # actually answers across sessions is the port itself.
    port = config.get("port", 5000)
    if _port_busy(port):
        # Busy, not necessarily answering. _is_ours asks the port a question
        # and a copy still starting up accepts the connection and closes it
        # without replying — which reads exactly like "nobody there", so the
        # desktop copy concluded the port was free, failed to bind it, and
        # quietly moved to the next one. Every link anybody has been given
        # carries the port in it, so that is all of them broken at once.
        #
        # Anything holding this port is treated as a copy to be relieved. It
        # is our port; nothing else on this machine should have it.
        # Defer to a copy that is actually serving, and to nothing else.
        # "Leaving it to whatever has it" reads as good manners and is the
        # single line that has broken every boot since this app learned to
        # start before anyone signs in: the session-0 copy binds the port
        # and wedges, and the copy with a desktop, speakers and a tray backs
        # out of its way. Answering our ping is the difference between a
        # colleague and a squatter.
        if srv._is_ours(port):
            mark(f"a working copy is already serving on {port} — leaving it to that one")
            sys.exit(0)
        mark(f"port {port} is held by something that isn't answering — taking it back")
        if _stand_down_headless():
            mark(f"got {port} back")
        elif _port_busy(port) and _seize_port(port):
            mark(f"took {port} from the copy that wasn't answering")
        elif _port_busy(port):
            mark(f"couldn't free {port} — it isn't answering and won't let go")
            sys.exit(0)

    # Taken exactly once. Every call to _singleton() that succeeds creates a
    # handle, so asking twice means the second answer is about the first ask.
    holder = _singleton()
    if holder is None:
        # Same session as another copy — the mutex does catch that one.
        if _stand_down_headless():
            holder = _singleton()
    if holder is None:
        mark("another copy already has the mutex — leaving it to that one")
        sys.exit(0)

    # The same patient startup the headless copy uses. Signing in straight
    # after a cold boot lands in the same unready machine, so this is not a
    # boot-only problem.
    thread, port, ready = _serve_with_retry()
    mark(f"server thread is on port {port}")
    # Slow is not the same as broken. Startup talks to four services and
    # launches two mpv processes, and on a cold machine — or straight after a
    # crash, when the caches are being rebuilt — it can take a good deal
    # longer than a minute. Giving up on it and putting an error on screen,
    # while the thing was still coming up behind the dialog, is most of what
    # "it wouldn't start" has been.
    rounds = 0
    while (not ready and rounds < 4 and thread.is_alive()
           and not srv.runtime.get("error")):
        rounds += 1
        mark(f"still starting after {rounds}m — the thread is alive, waiting")
        ready = _wait_for_server(port)
    if ready:
        mark(f"ready on port {port}")
        _trace(f"serving on {port}")
        threading.Thread(target=_watch_serving, args=(port,), daemon=True,
                         name="serving-watch").start()
    if not ready:
        mark("gave up waiting")
        # Opening a window onto a server that isn't there is how this used to
        # look like the app "just died". Say what's wrong instead — by now
        # startup has already tried to install anything missing, so if we're
        # here it needs a person.
        log.error("server did not come up — see the log")
        # Say what actually happened. Guessing produced "nothing obvious is
        # missing" for a server that had refused to start because another copy
        # was already running on the port, which helps nobody.
        why = srv.runtime.get("error") or ""
        missing = ", ".join(srv.missing_tools())
        if why:
            msg = f"Music Request Server couldn't start.\n\n{why}"
        elif missing:
            msg = (f"Music Request Server couldn't start.\n\n"
                   f"Missing: {missing}\n\nSetup ran but couldn't install "
                   f"{'them' if ',' in missing else 'it'}. Run setup.ps1 next "
                   f"to the app, then try again.")
        else:
            # Put the end of the log in the box. "The log has the detail" is
            # only true when the log has any, and the boots worth reporting
            # are the ones that fell over before they had written a line —
            # which is what you learn from seeing the last few.
            last = tail(8)
            msg = ("Music Request Server couldn't start.\n\n"
                   "Nothing obvious is missing. The last thing it managed:\n\n"
                   f"{last or '(nothing — it stopped before it could log)'}"
                   f"\n\nFull log: {log_path()}")
        _trace(f"giving up before the window: {msg.splitlines()[-1][:120]}")
        try:
            U32.MessageBoxW(None, msg, TITLE, 0x10)   # MB_ICONERROR
        except Exception:
            pass
        os._exit(1)

    sw = U32.GetSystemMetrics(0)
    sh = U32.GetSystemMetrics(1)
    x, y = max(0, sw - Flyout.W - 18), max(0, sh - Flyout.H - 60)

    hidden = "--hidden" in sys.argv
    flyout = Flyout()
    flyout._visible = not hidden
    # First run opens the guide instead of the player. It explains what the
    # three tools are for, what the key is as against a shared link, and which
    # of the optional services are worth having — then hands over. Every step
    # of it is skippable, and it stops appearing once it's been through.
    landing = "player" if config.get("setup_done") else "welcome"
    flyout.window = webview.create_window(
        TITLE,
        # The scheme the server is actually on. Pointed at http while
        # serving TLS, the flyout is a blank window onto a working server.
        url=srv.local_url(port, f"/{landing}?key={config.get('api_key', '')}"),
        js_api=Bridge(), frameless=True, easy_drag=False, on_top=True,
        resizable=False, width=Flyout.W, height=Flyout.H, x=x, y=y,
        # pywebview defaults this to (200, 100), so the 80px idle bar was
        # quietly being served at 100 and the recentring maths was working
        # off a height the window never had.
        min_size=(Flyout.MINI_W, Flyout.MINI_IDLE_H),
        background_color="#0e0f16", hidden=hidden)
    webview.start(_after_start)

    # start() returns when the last window closes, and that used to end the
    # process: player.stop(), os._exit(0), and not a line anywhere saying so.
    #
    # But a window closing is not the same as being asked to quit. A WebView2
    # that falls over, the runtime updating underneath it, a driver reset
    # taking the control with it — any of those destroy the window, and the
    # answer was to stop the music for everyone in the house and leave a log
    # that simply stops mid-song. Afterwards it is indistinguishable from a
    # crash, which is exactly what it has been reported as, twice.
    #
    # The tray exists so this runs without a window. So run without one.
    # start() cannot be called a second time in this process, so the flyout
    # is gone until a restart — but the server, the queue and the tray are
    # all still here, and that is the part anybody is actually using.
    if not _tray_quit.is_set():
        _window_gone.set()
        mark("the player window closed on its own — still serving")
        _trace("window gone; staying up, tray Quit to stop")
        _tray_quit.wait()

    _trace("exiting")
    try:
        from mrs.player import player
        player.stop()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
