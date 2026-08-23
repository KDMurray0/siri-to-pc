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

import webview
from PIL import Image, ImageDraw
from pystray import Icon as TrayIcon
from pystray import Menu, MenuItem

from mrs.config import config
from mrs.logging_setup import get
from mrs import server as srv

log = get("launcher")

U32 = ctypes.windll.user32
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

    # -- sizing (centre-anchored) --
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

    def _resize_centered(self, tw: int, th: int, dur: float = 0.16, steps: int = 10) -> None:
        """Grow/shrink around the window's centre, clamped to the screen."""
        self._resize_gen += 1
        gen = self._resize_gen
        h = self.hwnd()
        if not h:
            return
        r = self._rect()
        cw, ch = r.right - r.left, r.bottom - r.top
        cx, cy = r.left + cw / 2, r.top + ch / 2

        def run() -> None:
            for i in range(1, steps + 1):
                if gen != self._resize_gen:
                    return
                f = i / steps
                w = int(cw + (tw - cw) * f)
                ht = int(ch + (th - ch) * f)
                x, y = self._clamp(int(cx - w / 2), int(cy - ht / 2), w, ht)
                try:
                    U32.SetWindowPos(h, 0, x, y, w, ht, SWP_NOZORDER | SWP_NOACTIVATE)
                except Exception:
                    return
                time.sleep(dur / steps)

        threading.Thread(target=run, daemon=True).start()

    def set_mini(self, on) -> bool:
        self._mini = bool(on)
        if on:
            self._resize_centered(Flyout.MINI_W, Flyout.MINI_HOVER_H)
        else:
            self._resize_centered(Flyout.W, Flyout.H, dur=0.2)
        return bool(on)

    def set_mini_hover(self, on) -> bool:
        if not self._mini:
            return False
        self._resize_centered(Flyout.MINI_W,
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


flyout: Flyout | None = None

SIGNIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmusic.youtube.com%2F"
# where the cookies we need actually live
COOKIE_STOPS = ("https://music.youtube.com/", "https://www.youtube.com/",
                "https://accounts.google.com/")


def sign_in_window() -> None:
    """A real Google login in our own WebView2, then take the cookies.

    This is the whole reason the Chrome route was a dead end: we can't decrypt
    someone else's cookie jar, but we can own the browser that made them.
    """
    from mrs.core import cookies as ck

    win = webview.create_window("Sign in to YouTube", url=SIGNIN_URL,
                                width=520, height=680, on_top=True)

    def collect() -> None:
        jars, seen = [], set()
        for url in COOKIE_STOPS:
            try:
                win.load_url(url)
                time.sleep(3.0)          # let the navigation settle
                for jar in win.get_cookies() or []:
                    for name in (jar.keys() if hasattr(jar, "keys") else []):
                        morsel = jar[name]
                        tag = (morsel.get("domain") or "", name)
                        if tag in seen:
                            continue
                        seen.add(tag)
                        jars.append(jar)
            except Exception as exc:
                log.debug("no cookies from %s: %s", url, exc)
        text = ck.from_webview(jars)
        found = [n for n in ck.AUTH_COOKIES if f"	{n}	" in text]
        if found:
            ck.save_master(text)
            log.info("signed in — saved %d cookies (%s)",
                     len(text.splitlines()) - 3, ", ".join(found))
        else:
            log.warning("sign-in window had no auth cookies — not saved")
        try:
            win.destroy()
        except Exception:
            pass
        _api("/api/cookies/find")

    def on_loaded() -> None:
        # once Google hands us off to YouTube, the login is done
        try:
            url = win.get_current_url() or ""
        except Exception:
            return
        if "music.youtube.com" in url or "//www.youtube.com" in url:
            win.events.loaded -= on_loaded
            threading.Thread(target=collect, daemon=True, name="cookies-grab").start()

    win.events.loaded += on_loaded




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

    def quit_(icon, _it):
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
        MenuItem("Restart player", restart_player),
        MenuItem("Find cookies", find_cookies),
        Menu.SEPARATOR,
        MenuItem("Open on my phone", browser),
        Menu.SEPARATOR,
        MenuItem("Quit", quit_),
    )
    TrayIcon("Music Request Server", _icon_image(), menu=menu).run()


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
    for fn in (flyout.focus_watch, flyout.fullscreen_watch, flyout.hotkey_watch, _tray):
        threading.Thread(target=fn, daemon=True).start()


def main() -> None:
    global flyout
    if _singleton() is None:
        sys.exit(0)

    srv.run_in_thread()
    # The server may move to a free port if something else holds the configured
    # one, so ask it where it actually landed.
    port = config.get("port", 5000)
    for _ in range(60):
        if srv.runtime.get("port"):
            port = srv.runtime["port"]
            break
        time.sleep(0.5)
    if not _wait_for_server(port):
        log.error("server did not come up — see the log")

    sw = U32.GetSystemMetrics(0)
    sh = U32.GetSystemMetrics(1)
    x, y = max(0, sw - Flyout.W - 18), max(0, sh - Flyout.H - 60)

    hidden = "--hidden" in sys.argv
    flyout = Flyout()
    flyout._visible = not hidden
    flyout.window = webview.create_window(
        TITLE,
        url=f"http://127.0.0.1:{port}/player?key={config.get('api_key','')}",
        js_api=Bridge(), frameless=True, easy_drag=False, on_top=True,
        resizable=False, width=Flyout.W, height=Flyout.H, x=x, y=y,
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
