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

_here = os.path.dirname(os.path.abspath(__file__))


def _reexec_if_needed():
    """Relaunch under config python_path if webview isn't importable here."""
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

_config_path = os.path.join(_here, "src", "config.json")


def _load_config():
    with open(_config_path) as f:
        return json.load(f)


# ── server subprocess ───────────────────────────────────────────────

_server_process = None
_running = False


def _run_server():
    global _server_process, _running
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
    if _server_process is not None:
        try:
            _server_process.terminate(); _server_process.wait(timeout=10)
        except Exception:
            pass
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

def _after_start():
    time.sleep(0.4)
    _flyout.round_corners()
    threading.Thread(target=_flyout.focus_watch, daemon=True).start()
    threading.Thread(target=_tray, daemon=True).start()


if __name__ == "__main__":
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
