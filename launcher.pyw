"""Music Request Server - System Tray Launcher + Web Player Flyout.

Double-click this file (.pyw) to run the server from a system-tray icon plus a
borderless, rounded, always-on-top web-based music player (served by the app at
/player and shown in a pywebview window). It behaves like a Windows flyout —
click away to hide, click the tray icon to bring it back.

The player UI needs pywebview + pystray, which live in the interpreter named by
config "python_path"; if the .pyw was opened by a different Python this file
re-execs itself under the right one.
"""

import json
import os
import sys

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Frozen: server runs in-process; config/data sit next to the .exe.
    _here = os.path.dirname(sys.executable)
    # No console in a windowed .exe -> send prints to a log, not a None stdout.
    try:
        _logf = open(os.path.join(_here, "server.log"), "a", buffering=1,
                     encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = _logf
    except Exception:
        pass
else:
    _here = os.path.dirname(os.path.abspath(__file__))


def _reexec_if_needed():
    """Relaunch under config python_path if webview isn't importable here."""
    if FROZEN:
        return False
    try:
        import webview  # noqa: F401
        return False
    except Exception:
        pass
    try:
        cfg = json.load(open(os.path.join(_here, "src", "config.json")))
        py = cfg.get("python_path") or ""
        pyw = py.replace("python.exe", "pythonw.exe")
        if os.path.exists(pyw):
            py = pyw
        if py and os.path.abspath(py) != os.path.abspath(sys.executable):
            import subprocess
            subprocess.Popen([py, os.path.abspath(__file__)])
            return True
    except Exception:
        pass
    return False


if _reexec_if_needed():
    sys.exit(0)

import ctypes
from ctypes import wintypes
import socket
import subprocess
import threading
import time
from urllib import request as urlrequest

import webview
from pystray import Icon as TrayIcon
from pystray import Menu, MenuItem
from PIL import Image, ImageDraw

if FROZEN:
    import paths as _paths
    _paths.migrate_legacy_data()          # move old next-to-exe data to stable dir
    _config_path = _paths.config_path()   # %LOCALAPPDATA%\MusicRequestServer
else:
    _config_path = os.path.join(_here, "src", "config.json")


def _load_config():
    if not os.path.exists(_config_path) and FROZEN:
        # First run of the .exe with no config yet: generate one (fresh key).
        try:
            import auth
            auth.ensure_config()
        except Exception:
            pass
    with open(_config_path) as f:
        return json.load(f)


def _kill_mpv():
    # Kill our mpv (matches the pipe name). subprocess.terminate() on the server
    # skips its atexit cleanup, so mpv would otherwise be orphaned on quit.
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='mpv.exe'\" | "
              "Where-Object { $_.CommandLine -match 'mpvsocket' } | ForEach-Object "
              "{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000)
    except Exception:
        pass


# ── server: in-process thread when frozen, subprocess from source ────

_server_process = None
_server_app = None       # the imported app module (frozen path only)
_running = False
_port_override = None    # set when the configured port is taken by something else


def _run_server():
    global _server_process, _server_app, _running
    if FROZEN:
        _running = True
        try:
            import app as server_app          # bundled server modules
            _server_app = server_app
            server_app.startup()
            cfg = _load_config()
            server_app.app.run(host=cfg.get("host", "0.0.0.0"),
                               port=_port_override or cfg.get("port", 5000),
                               threaded=True, debug=False, use_reloader=False)
        except SystemExit:
            pass                                # startup() aborts on missing mpv
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            _running = False
        return
    py = _load_config().get("python_path") or sys.executable
    try:
        _server_process = subprocess.Popen(
            [py, os.path.join(_here, "src", "app.py")],
            cwd=os.path.join(_here, "src"), creationflags=0x08000000)
        _running = True
        _server_process.wait()
    except Exception:
        pass
    finally:
        _running = False


def _start_server():
    if not _running:
        threading.Thread(target=_run_server, daemon=True).start()


def _stop_server():
    global _server_process, _running
    if FROZEN:
        try:
            if _server_app is not None:
                _server_app.player_manager.stop()
        except Exception:
            pass
    elif _server_process is not None:
        try:
            _server_process.terminate(); _server_process.wait(timeout=10)
        except Exception:
            pass
    _kill_mpv()          # ensure no mpv is left running after we quit
    _running = False


def _is_our_server(port, timeout=1.5):
    """True only if OUR server answers on *port* (/api/ping is unauthenticated).

    A stale instance or an unrelated app holding the port would otherwise get
    the player window pointed at it — which just renders a bare 404.
    """
    try:
        with urlrequest.urlopen(f"http://127.0.0.1:{port}/api/ping",
                                timeout=timeout) as r:
            return b'"status"' in r.read(120)
    except Exception:
        return False


def _port_is_free(port):
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try: s.close()
        except Exception: pass


def _pick_port(preferred):
    """Use the configured port unless a FOREIGN server holds it.

    Windows lets a 127.0.0.1 bind win over our 0.0.0.0 bind, so a squatter
    silently steals every request the flyout makes. Step aside to a free port.
    """
    if _port_is_free(preferred) or _is_our_server(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 21):
        if _port_is_free(p):
            print(f"Port {preferred} is held by another server — using {p} instead.")
            return p
    return preferred


def _wait_for_server(port, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if _is_our_server(port, timeout=1):
            return True
        time.sleep(0.6)
    return False


def _lan():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53)); a = s.getsockname()[0]; s.close(); return a
    except Exception:
        return "localhost"


# ── flyout window controller ────────────────────────────────────────

class Flyout:
    W, H = 400, 640

    def __init__(self, config):
        self.config = config
        self.window = None
        self._visible = True
        self._shown_at = time.monotonic()
        self._hwnd = None
        self._pinned = False          # popped out: stays put, never auto-hides
        self._moving = False          # native drag loop active
        self._fs_active = False       # a fullscreen app currently owns our monitor
        self._click_through = False   # overlay mode: cursor passes through
        self._force_interactive = False  # hotkey override during fullscreen
        self._mini = False            # shrunk to the mini player
        self._resize_gen = 0          # cancels an in-flight animated resize

    # JS bridge
    def on_blur(self):
        if self._pinned:
            return
        if not os.environ.get("MRS_NO_AUTOHIDE"):
            self.hide()

    def set_pinned(self, on):
        # Pop-out: keep it visible, force it topmost, stop auto-hiding on blur.
        self._pinned = bool(on)
        if self._fs_active and self._pinned:
            return self._pinned      # a game owns the screen — stay out of its way
        try:
            if self._pinned and self.window:
                self.window.show()
                self._visible = True
            hwnd = self._find_hwnd()
            if hwnd:
                HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
                SWP = 0x0001 | 0x0002 | 0x0040   # NOSIZE | NOMOVE | SHOWWINDOW
                ctypes.windll.user32.SetWindowPos(
                    hwnd, HWND_TOPMOST if self._pinned else HWND_NOTOPMOST,
                    0, 0, 0, 0, SWP)
        except Exception:
            pass
        return self._pinned

    def begin_move(self):
        # Native drag: follow the cursor while the left button is held. Reliable
        # even where WebView2 ignores -webkit-app-region:drag.
        if self._moving:
            return
        self._moving = True
        threading.Thread(target=self._move_loop, daemon=True).start()

    def _move_loop(self):
        try:
            u = ctypes.windll.user32
            hwnd = self._find_hwnd()
            if not hwnd:
                return
            pt = wintypes.POINT(); u.GetCursorPos(ctypes.byref(pt))
            r = wintypes.RECT(); u.GetWindowRect(hwnd, ctypes.byref(r))
            ox, oy = pt.x - r.left, pt.y - r.top
            while u.GetAsyncKeyState(0x01) & 0x8000:      # VK_LBUTTON held
                u.GetCursorPos(ctypes.byref(pt))
                u.SetWindowPos(hwnd, 0, pt.x - ox, pt.y - oy, 0, 0, 0x0001 | 0x0004)  # NOSIZE|NOZORDER
                time.sleep(0.008)
        except Exception:
            pass
        finally:
            self._moving = False

    MINI_W = 344            # constant mini width (idle + hover share it)
    MINI_IDLE_H = 80        # idle: art + name/artist
    MINI_HOVER_H = 108      # hover: + transport row + vertical volume

    def _win_size(self):
        try:
            hwnd = self._find_hwnd()
            if hwnd:
                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                return r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return Flyout.W, Flyout.H

    def _animate_resize(self, tw, th, dur=0.16, steps=11):
        """Smoothly step the window from its current size to (tw, th). A newer
        call cancels an in-flight one via the generation counter."""
        self._resize_gen += 1
        gen = self._resize_gen
        cw, ch = self._win_size()

        def run():
            for i in range(1, steps + 1):
                if gen != self._resize_gen or not self.window:
                    return
                f = i / steps
                try:
                    self.window.resize(int(cw + (tw - cw) * f),
                                       int(ch + (th - ch) * f))
                except Exception:
                    return
                time.sleep(dur / steps)
        threading.Thread(target=run, daemon=True).start()

    def set_mini(self, on):
        # Enter/leave mini mode (animated). Enter at hover height (pointer is
        # over it); set_mini_hover collapses to the idle card when you leave.
        self._mini = bool(on)
        if on:
            self._animate_resize(Flyout.MINI_W, Flyout.MINI_HOVER_H)
        else:
            self._animate_resize(Flyout.W, Flyout.H, dur=0.20)
        return bool(on)

    def set_mini_hover(self, on):
        # While mini: grow to the card on hover, shrink to the idle card off.
        # Width never changes — only the height animates.
        if not self._mini:
            return False
        self._animate_resize(Flyout.MINI_W,
                             Flyout.MINI_HOVER_H if on else Flyout.MINI_IDLE_H)
        return bool(on)

    def _find_hwnd(self):
        if self._hwnd:
            return self._hwnd
        hwnd = ctypes.windll.user32.FindWindowW(None, "Music Request")
        self._hwnd = hwnd or None
        return self._hwnd

    def round_corners(self):
        try:
            hwnd = self._find_hwnd()
            if hwnd:
                val = ctypes.c_int(2)  # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def hide_from_taskbar(self):
        # Make it a tool window: no taskbar button, no alt-tab entry — a flyout.
        try:
            hwnd = self._find_hwnd()
            if not hwnd:
                return
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
            u = ctypes.windll.user32
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                             (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            if self._visible:            # hide/show so the taskbar drops the button
                u.ShowWindow(hwnd, 0)
                u.ShowWindow(hwnd, 5)
                u.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def show(self):
        try:
            self.window.show()
            self._visible = True
            self._shown_at = time.monotonic()
            hwnd = self._find_hwnd()
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def hide(self):
        try:
            self.window.hide()
        except Exception:
            pass
        self._visible = False

    def focus_watch(self):
        """Hide when another window becomes the OS foreground."""
        while True:
            time.sleep(0.25)
            try:
                if self._pinned or self._fs_active or os.environ.get("MRS_NO_AUTOHIDE"):
                    continue                       # overlay stays put during games
                if self._visible and (time.monotonic() - self._shown_at) > 0.6:
                    hwnd = self._find_hwnd()
                    if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                        self.hide()
            except Exception:
                pass

    def _foreground_is_fullscreen(self):
        """True when a real app (a game) covers the whole monitor in the
        foreground — so we should get out of its way even when pinned."""
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        hwnd = self._find_hwnd()
        if hwnd and fg == ctypes.c_void_p(hwnd).value:
            return False                      # that's us
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(fg, buf, 256)
        if buf.value in ("WorkerW", "Progman", "Shell_TrayWnd",
                         "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"):
            return False                      # desktop / shell / task view
        r = wintypes.RECT()
        u.GetWindowRect(fg, ctypes.byref(r))

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mon = u.MonitorFromWindow(fg, 2)      # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        m = mi.rcMonitor
        covers = (r.left <= m.left and r.top <= m.top
                  and r.right >= m.right and r.bottom >= m.bottom)
        if not covers:
            return False
        # Only get out of the way if the game is on the SAME monitor as us — a
        # fullscreen app on the OTHER screen shouldn't touch the player.
        if hwnd:
            pmon = u.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)
            if pmon and mon and pmon != mon:
                return False
        return True

    def _set_topmost(self, on):
        try:
            hwnd = self._find_hwnd()
            if hwnd:
                SWP = 0x0001 | 0x0002 | 0x0010    # NOSIZE | NOMOVE | NOACTIVATE
                ctypes.windll.user32.SetWindowPos(
                    hwnd, -1 if on else -2, 0, 0, 0, 0, SWP)
        except Exception:
            pass

    def _set_click_through(self, on):
        """Discord-overlay behaviour: the window stays visible + on top but the
        cursor passes straight through it (WS_EX_TRANSPARENT), so it can't grab
        the mouse over a game. LAYERED+opaque alpha keeps it fully drawn."""
        if on == self._click_through:
            return
        try:
            hwnd = self._find_hwnd()
            if not hwnd:
                return
            u = ctypes.windll.user32
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, LWA_ALPHA = -20, 0x80000, 0x20, 2
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
                u.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)  # stay opaque
                self._set_topmost(True)          # float over the game like an overlay
            else:
                u.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
            self._click_through = on
        except Exception:
            pass

    def fullscreen_watch(self):
        """While a fullscreen app owns OUR monitor, turn the player into a
        click-through overlay (visible, on top, but the mouse passes through) —
        like the Discord overlay. Ctrl+Alt+M toggles interactivity back on."""
        while True:
            time.sleep(0.7)
            try:
                fs = self._foreground_is_fullscreen()
                if fs and not self._fs_active:
                    self._fs_active = True
                    if not self._force_interactive:
                        self._set_click_through(True)
                elif fs and self._fs_active and self._click_through:
                    # Keep re-asserting topmost — a borderless game re-raises
                    # itself, and we have to keep floating above it (Discord does
                    # the same). (True exclusive-fullscreen can't be overlaid by
                    # any normal window; nothing to do there.)
                    self._set_topmost(True)
                elif not fs and self._fs_active:
                    self._fs_active = False
                    self._force_interactive = False
                    self._set_click_through(False)
                    if self._pinned:
                        self.set_pinned(True)
                    else:
                        self._set_topmost(False)
            except Exception:
                pass

    def toggle_interactive(self):
        """Hotkey: while a game is fullscreen, flip the overlay between
        click-through and interactive so you can actually use it, then back."""
        if not self._fs_active:
            return
        self._force_interactive = not self._force_interactive
        self._set_click_through(not self._force_interactive)
        if self._force_interactive:
            try:
                ctypes.windll.user32.SetForegroundWindow(
                    ctypes.c_void_p(self._find_hwnd()))
            except Exception:
                pass

    def hotkey_watch(self):
        """Poll Ctrl+Alt+M (no message loop needed) to toggle overlay interactivity."""
        u = ctypes.windll.user32
        prev = False
        while True:
            time.sleep(0.05)
            try:
                down = ((u.GetAsyncKeyState(0x11) & 0x8000) and   # Ctrl
                        (u.GetAsyncKeyState(0x12) & 0x8000) and   # Alt
                        (u.GetAsyncKeyState(0x4D) & 0x8000))      # M
                if down and not prev:
                    self.toggle_interactive()
                prev = bool(down)
            except Exception:
                pass


# ── JS bridge ────────────────────────────────────────────────────────
# A tiny api object with NO reference to the window, so pywebview never walks
# the WebView2 COM object (that caused the recursion-depth spam in the log).

class _Bridge:
    def on_blur(self):
        if _flyout:
            _flyout.on_blur()

    def set_pinned(self, on):
        return _flyout.set_pinned(on) if _flyout else False

    def begin_move(self):
        if _flyout:
            _flyout.begin_move()

    def set_mini(self, on):
        return _flyout.set_mini(on) if _flyout else False

    def set_mini_hover(self, on):
        return _flyout.set_mini_hover(on) if _flyout else False


# ── tray ─────────────────────────────────────────────────────────────

def _make_icon():
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img); c = (109, 139, 255, 255)
    d.ellipse([8, 24, 16, 30], fill=c); d.rectangle([15, 8, 17, 26], fill=c)
    d.polygon([(17, 8), (26, 14), (26, 16), (17, 20)], fill=c)
    return img


_flyout = None


def _tray():
    def show(icon, item): _flyout.show()
    def browser(icon, item):
        import webbrowser
        c = _load_config()
        webbrowser.open(f"http://{_lan()}:{c.get('port',5000)}/?key={c.get('api_key','')}")
    def quit_(icon, item):
        _stop_server(); icon.stop()
        try:
            _flyout.window.destroy()
        except Exception:
            pass
        os._exit(0)
    menu = Menu(
        MenuItem("Show Player", show, default=True),
        MenuItem("Open in Browser (phone)", browser),
        Menu.SEPARATOR,
        MenuItem("Quit", quit_),
    )
    TrayIcon("Music Request Server", _make_icon(), menu=menu).run()


# ── main ─────────────────────────────────────────────────────────────

def _acquire_singleton():
    # Named mutex: stops a second launch spawning a rival server/mpv.
    # Returns a handle to keep alive, or None if already running.
    try:
        # use_last_error so get_last_error() reflects THIS CreateMutexW call, not
        # a stale value the (windowed) PyInstaller bootloader left behind — that
        # stale 183 made the sole instance think it was a duplicate and exit.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k32.CreateMutexW(None, False, "MusicRequestServer_singleton")
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            return None
        return h or True
    except Exception:
        return True                       # never block startup on a mutex error


def _after_start():
    # Boot / --hidden: squash any flash — force the window hidden right away.
    if not _flyout._visible:
        _flyout.hide()
        try:
            hwnd = _flyout._find_hwnd()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
        except Exception:
            pass
    time.sleep(0.4)
    _flyout.round_corners()
    _flyout.hide_from_taskbar()
    threading.Thread(target=_flyout.focus_watch, daemon=True).start()
    threading.Thread(target=_flyout.fullscreen_watch, daemon=True).start()
    threading.Thread(target=_flyout.hotkey_watch, daemon=True).start()
    threading.Thread(target=_tray, daemon=True).start()


if __name__ == "__main__":
    _singleton = _acquire_singleton()
    if _singleton is None:
        sys.exit(0)                      # another instance is already running

    config = _load_config()
    port = _pick_port(config.get("port", 5000))
    _port_override = port if port != config.get("port", 5000) else None
    key = config.get("api_key", "")

    _start_server()
    _wait_for_server(port)

    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    x, y = sw - Flyout.W - 18, sh - Flyout.H - 60

    hidden = "--hidden" in sys.argv   # start minimised to tray (boot launch)
    _flyout = Flyout(config)
    _flyout._visible = not hidden
    _flyout.window = webview.create_window(
        "Music Request",
        url=f"http://127.0.0.1:{port}/player?key={key}",
        js_api=_Bridge(),
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        width=Flyout.W, height=Flyout.H, x=x, y=y,
        background_color="#0e0f16", hidden=hidden,
    )
    webview.start(_after_start)
    # webview.start returns when the window is closed
    _stop_server()
    os._exit(0)
