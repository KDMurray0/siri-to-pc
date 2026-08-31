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
import threading
import time
from ctypes import wintypes
from urllib import request as urlrequest
from urllib.parse import urlparse

import webview
from PIL import Image, ImageDraw
from pystray import Icon as TrayIcon
from pystray import Menu, MenuItem

from mrs.config import config
from mrs.logging_setup import get, log_path, mark, tail
from mrs import server as srv

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
    port = config.get("port", 5000)
    key = config.get("api_key", "")
    sep = "&" if "?" in path else "?"
    try:
        urlrequest.urlopen(f"http://127.0.0.1:{port}{path}{sep}key={key}", timeout=5)
    except Exception:
        pass


def _tray() -> None:
    def show(_i, _it):
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
        webbrowser.open(f"http://{host}:{config.get('port',5000)}/?key={config.get('api_key','')}")

    def desktop(_i, _it):
        # The flyout is a fixed 400px window on purpose, so the wide layout
        # can't be reached by dragging it — a browser is the only way in.
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{config.get('port',5000)}"
                        f"/player?key={config.get('api_key','')}")

    def quit_(icon, _it):
        _tray_quit.set()          # so the keep-alive loop doesn't rebuild it
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



def _singleton():
    """Named mutex so a second launch can't fight over mpv and the port."""
    try:
        # use_last_error, or the windowed bootloader's stale 183 makes the only
        # instance think it's a duplicate and exit immediately.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k32.CreateMutexW(None, False, "MusicRequestServer_singleton")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return h or True
    except Exception:
        return True


def _wait_for_server(port: int, timeout: int = 60) -> bool:
    """Wait for OUR server — a foreign one on the same port doesn't count."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urlrequest.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
                if b"music-request-server" in r.read(200):
                    return True
        except Exception:
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

    mark("starting")
    if _singleton() is None:
        mark("another copy already has the mutex — leaving it to that one")
        sys.exit(0)

    thread = srv.run_in_thread()
    # The server may move to a free port if something else holds the configured
    # one, so ask it where it actually landed.
    port = config.get("port", 5000)
    for _ in range(60):
        if srv.runtime.get("port"):
            port = srv.runtime["port"]
            break
        time.sleep(0.5)
    mark(f"server thread is on port {port}")
    # Slow is not the same as broken. Startup talks to four services and
    # launches two mpv processes, and on a cold machine — or straight after a
    # crash, when the caches are being rebuilt — it can take a good deal
    # longer than a minute. Giving up on it and putting an error on screen,
    # while the thing was still coming up behind the dialog, is most of what
    # "it wouldn't start" has been.
    ready = _wait_for_server(port)
    rounds = 0
    while (not ready and rounds < 4 and thread.is_alive()
           and not srv.runtime.get("error")):
        rounds += 1
        mark(f"still starting after {rounds}m — the thread is alive, waiting")
        ready = _wait_for_server(port)
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
        url=f"http://127.0.0.1:{port}/{landing}?key={config.get('api_key','')}",
        js_api=Bridge(), frameless=True, easy_drag=False, on_top=True,
        resizable=False, width=Flyout.W, height=Flyout.H, x=x, y=y,
        # pywebview defaults this to (200, 100), so the 80px idle bar was
        # quietly being served at 100 and the recentring maths was working
        # off a height the window never had.
        min_size=(Flyout.MINI_W, Flyout.MINI_IDLE_H),
        background_color="#0e0f16", hidden=hidden)
    webview.start(_after_start)
    try:
        from mrs.player import player
        player.stop()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
