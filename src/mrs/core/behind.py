"""What's behind the mini player, so its glass can bend the real thing.

A web page can't see past its own window. So while the glass mini player is
on screen, the app takes the patch of desktop under the window -- the window
itself is left out of capture, so what comes back is what's behind it -- and
returns it through the desktop window's private JS bridge. The page lays it under
the glass and the glass does the rest: bent at the rim, frosted, tinted.

Only ever the client rectangle of our own window, never an HTTP endpoint,
and only while the visible mini player is asking. Side effect, said
plainly: while it runs, the mini player doesn't show up in screenshots or
screen shares.
"""

from __future__ import annotations

import ctypes
import io
import sys
import threading
from ctypes import wintypes

from ..logging_setup import get

log = get("behind")

_state = {"hwnd": None, "visible": None, "excluded": None, "enabled": False}
_lock = threading.RLock()


def use(hwnd_of, visible=None) -> None:
    """The desktop app says which window is the mini player."""
    with _lock:
        set_enabled(False)
        _state["hwnd"] = hwnd_of
        _state["visible"] = visible


def available() -> bool:
    return (sys.platform == "win32" and sys.getwindowsversion().build >= 19041
            and _state["hwnd"] is not None)


if sys.platform == "win32":
    _U32, _G32 = ctypes.windll.user32, ctypes.windll.gdi32
    _U32.GetDC.restype = ctypes.c_void_p
    _U32.GetDC.argtypes = [ctypes.c_void_p]
    _U32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _U32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    _U32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    _U32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _U32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _G32.CreateCompatibleDC.restype = ctypes.c_void_p
    _G32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _G32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _G32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _G32.SelectObject.restype = ctypes.c_void_p
    _G32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _G32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _G32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    _G32.DeleteObject.argtypes = [ctypes.c_void_p]
    _G32.DeleteDC.argtypes = [ctypes.c_void_p]


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _hwnd():
    got = _state["hwnd"]
    try:
        return got() if callable(got) else got
    except Exception:
        return None


def set_enabled(on: bool) -> bool:
    """Exclude exactly our window; never capture if Windows refused exclusion."""
    with _lock:
        _state["enabled"] = False
        old = _state["excluded"]
        h = _hwnd() if on and available() else None
        if old and old != h:
            if not _U32.SetWindowDisplayAffinity(ctypes.c_void_p(old), 0):
                log.debug("could not restore display affinity")
                return False  # Keep the handle so the next stop can retry.
            _state["excluded"] = None
        if not h:
            return False
        if old == h:
            _state["enabled"] = True
            return True
        if not _U32.SetWindowDisplayAffinity(ctypes.c_void_p(h), 0x11):
            return False
        _state["excluded"] = h
        _state["enabled"] = True
        return True


def _grab(x: int, y: int, w: int, h: int) -> bytes:
    scr = _U32.GetDC(None)
    mem = bmp = old = None
    try:
        if not scr:
            raise OSError("GetDC failed")
        mem = _G32.CreateCompatibleDC(scr)
        if not mem:
            raise OSError("CreateCompatibleDC failed")
        bmp = _G32.CreateCompatibleBitmap(scr, w, h)
        if not bmp:
            raise OSError("CreateCompatibleBitmap failed")
        old = _G32.SelectObject(mem, bmp)
        if not old or old == ctypes.c_void_p(-1).value:
            old = None
            raise OSError("SelectObject failed")
        if not _G32.BitBlt(mem, 0, 0, w, h, scr, x, y, 0x40CC0020):  # SRCCOPY | CAPTUREBLT
            raise OSError("BitBlt failed")
        # GetDIBits requires the bitmap to be deselected from the DC.
        _G32.SelectObject(mem, old)
        old = None
        bih = _BIH(ctypes.sizeof(_BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if _G32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0) != h:
            raise OSError("GetDIBits failed")
        return buf.raw
    finally:
        if old:
            _G32.SelectObject(mem, old)
        if bmp:
            _G32.DeleteObject(bmp)
        if mem:
            _G32.DeleteDC(mem)
        if scr:
            _U32.ReleaseDC(None, scr)


def frame(quality: int = 72) -> bytes | None:
    """One JPEG of what's under the window right now, or None if there's nothing to show."""
    with _lock:
        try:
            return _frame(quality)
        except Exception as exc:
            log.debug("background capture: %s", exc)
            return None


def _frame(quality: int) -> bytes | None:
    if not available() or not _state["enabled"] or not _state["excluded"]:
        return None
    vis = _state["visible"]
    try:
        if callable(vis) and not vis():
            return None
    except Exception:
        return None
    h = _hwnd()
    if not h or h != _state["excluded"] or not _U32.IsWindowVisible(ctypes.c_void_p(h)):
        return None
    r = wintypes.RECT()
    origin = wintypes.POINT()
    if (not _U32.GetClientRect(ctypes.c_void_p(h), ctypes.byref(r))
            or not _U32.ClientToScreen(ctypes.c_void_p(h), ctypes.byref(origin))):
        return None
    w, ht = r.right - r.left, r.bottom - r.top
    if w <= 0 or ht <= 0 or w > 2048 or ht > 768:
        return None
    from PIL import Image
    raw = _grab(origin.x, origin.y, w, ht)
    im = Image.frombuffer("RGBA", (w, ht), raw, "raw", "BGRA", 0, 1).convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality)
    return out.getvalue()
