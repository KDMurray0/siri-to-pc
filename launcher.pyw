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
import socket
import subprocess
import threading
import time
from urllib import request as urlrequest

import webview
from pystray import Icon as TrayIcon
from pystray import Menu, MenuItem
from PIL import Image, ImageDraw

_config_path = os.path.join(_here, "config.json") if FROZEN \
    else os.path.join(_here, "src", "config.json")


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
                               port=cfg.get("port", 5000),
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


def _wait_for_server(port, timeout=40):
    url = f"http://127.0.0.1:{port}/api/ping"
    end = time.time() + timeout
    while time.time() < end:
        try:
            urlrequest.urlopen(url, timeout=1)
            return True
        except Exception:
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

    # JS bridge
    def on_blur(self):
        if not os.environ.get("MRS_NO_AUTOHIDE"):
            self.hide()

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
                if os.environ.get("MRS_NO_AUTOHIDE"):
                    continue
                if self._visible and (time.monotonic() - self._shown_at) > 0.6:
                    hwnd = self._find_hwnd()
                    if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                        self.hide()
            except Exception:
                pass


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
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "MusicRequestServer_singleton")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return h or True
    except Exception:
        return True                       # never block startup on a mutex error


def _after_start():
    time.sleep(0.4)
    _flyout.round_corners()
    _flyout.hide_from_taskbar()
    threading.Thread(target=_flyout.focus_watch, daemon=True).start()
    threading.Thread(target=_tray, daemon=True).start()


if __name__ == "__main__":
    _singleton = _acquire_singleton()
    if _singleton is None:
        sys.exit(0)                      # another instance is already running

    config = _load_config()
    port = config.get("port", 5000)
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
        js_api=_flyout,
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        width=Flyout.W, height=Flyout.H, x=x, y=y,
        background_color="#0e0f16", hidden=hidden,
    )
    webview.start(_after_start)
    # webview.start returns when the window is closed
    _stop_server()
    os._exit(0)
