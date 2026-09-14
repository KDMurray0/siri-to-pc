"""The name of the Windows audio output, as Windows says it.

Asked through Core Audio directly: IMMDeviceEnumerator, the same call mpv's
WASAPI output makes to pick its device. Starting PortAudio for this takes a
quarter of a second and enumerates every host API; this takes about two
milliseconds, so it can be asked every few seconds to notice a switch.

A Bluetooth headphone's name is its model — "Headphones (WH-1000XM4)" — which
is what makes matching a correction profile to it possible at all. A wired
jack just says "Headphones (High Definition Audio Device)".
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

_E_RENDER, _E_MULTIMEDIA = 0, 1
_CLSCTX_ALL = 23
_STGM_READ = 0
_VT_LPWSTR = 31


class _GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        g = cls()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


_local = threading.local()


def _ref(x) -> ctypes.c_void_p:
    return ctypes.c_void_p(ctypes.addressof(x))


def _call(obj, index: int, *args, restype=ctypes.HRESULT):
    """Method `index` of a COM object's vtable."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *[type(a) for a in args])(vtbl[index])
    return fn(obj, *args)


def _release(obj) -> None:
    if obj:
        _call(obj, 2, restype=wintypes.ULONG)


def _enumerator():
    """One per thread, created once. COM objects don't cross threads."""
    got = getattr(_local, "enum", None)
    if got:
        return got
    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitializeEx(None, 0)          # multithreaded
    except OSError:
        pass                                   # already initialised another way: fine
    enum = ctypes.c_void_p()
    ole32.CoCreateInstance(
        ctypes.byref(_GUID.of("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None,
        _CLSCTX_ALL, ctypes.byref(_GUID.of("{A95664D2-9614-4F35-A746-DE8DB63617E6}")),
        ctypes.byref(enum))
    _local.enum = enum
    return enum


def _name_of(device) -> str:
    store = ctypes.c_void_p()
    _call(device, 4, wintypes.DWORD(_STGM_READ), _ref(store))
    try:
        key = _PROPERTYKEY(_GUID.of("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)
        var = (ctypes.c_ubyte * 32)()
        _call(store, 5, _ref(key), _ref(var))
        try:
            vt = int.from_bytes(bytes(var[0:2]), "little")
            if vt != _VT_LPWSTR:
                return ""
            ptr = ctypes.c_void_p.from_buffer(var, 8).value
            return ctypes.wstring_at(ptr) if ptr else ""
        finally:
            ctypes.oledll.ole32.PropVariantClear(_ref(var))
    finally:
        _release(store)


def _id_of(device) -> str:
    raw = ctypes.c_wchar_p()
    _call(device, 5, _ref(raw))
    try:
        return raw.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(raw)


def default_output() -> dict:
    """{"id", "name"} of the default multimedia output, or empty strings."""
    if sys.platform != "win32":
        return {"id": "", "name": ""}
    try:
        device = ctypes.c_void_p()
        _call(_enumerator(), 4, ctypes.c_int(_E_RENDER), ctypes.c_int(_E_MULTIMEDIA),
              _ref(device))
        try:
            return {"id": _id_of(device), "name": _name_of(device)}
        finally:
            _release(device)
    except OSError:
        return {"id": "", "name": ""}


def output_named(device_id: str) -> str:
    """The friendly name for an endpoint id, e.g. mpv's wasapi/{…}."""
    if sys.platform != "win32" or not device_id:
        return ""
    try:
        device = ctypes.c_void_p()
        _call(_enumerator(), 5, ctypes.c_wchar_p(device_id), _ref(device))
        try:
            return _name_of(device)
        finally:
            _release(device)
    except OSError:
        return ""
