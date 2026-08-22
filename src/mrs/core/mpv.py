"""Talking to mpv over a Windows named pipe.

Don't "clean up" these bits, they're all load-bearing:
  - open the pipe with FILE_FLAG_OVERLAPPED, sync handles deadlock
  - one handle for read+write, mpv replies on the same connection
  - send real JSON true/false, mpv rejects 1/0 for flags like pause
  - winerror 232/109/233/6/2 just means mpv went away
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future
from ctypes import wintypes

from ..logging_setup import get

log = get("mpv")

_K32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
CREATE_NO_WINDOW = 0x08000000

# errno values that all mean "the pipe is gone"
_PIPE_DEAD = {232, 109, 233, 6, 2}

# A suffix keeps a second instance (or a test run alongside the real app) from
# fighting over the same pipe — and from killing the other's mpv as a "stray".
_SUFFIX = os.environ.get("MRS_PIPE_SUFFIX", "")
PIPE_MAIN = rf"\\.\pipe\mpvsocket{_SUFFIX}"
PIPE_ALT = rf"\\.\pipe\mpvsocket{_SUFFIX}2"


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


_K32.CreateFileW.restype = ctypes.c_void_p
_K32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p]
_K32.CreateEventW.restype = wintypes.HANDLE
_K32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_K32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_K32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]


def is_pipe_dead(exc: Exception) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in _PIPE_DEAD


def _open_pipe(name: str):
    h = _K32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None,
                         OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if h == INVALID_HANDLE or not h:
        raise OSError(ctypes.get_last_error(), f"cannot open {name}")
    return h


def _write(handle, payload: bytes) -> None:
    ev = _K32.CreateEventW(None, True, False, None)
    ov = _OVERLAPPED(); ov.hEvent = ev
    written = wintypes.DWORD(0)
    try:
        ok = _K32.WriteFile(handle, payload, len(payload), ctypes.byref(written),
                            ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                raise OSError(err, "WriteFile failed")
            if _K32.WaitForSingleObject(ev, 4000) != 0:
                raise OSError(258, "WriteFile timed out")
            if not _K32.GetOverlappedResult(handle, ctypes.byref(ov),
                                            ctypes.byref(written), False):
                raise OSError(ctypes.get_last_error(), "WriteFile result failed")
    finally:
        _K32.CloseHandle(ev)


def kill_stray_mpv() -> bool:
    """Kill mpv instances belonging to THIS instance (matched by pipe name)."""
    marker = f"mpvsocket{_SUFFIX}" if _SUFFIX else "mpvsocket"
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='mpv.exe'\" | "
          f"Where-Object {{ $_.CommandLine -match '{marker}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue; 'killed' }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return "killed" in (r.stdout or "")
    except Exception:
        return False


class MpvClient:
    """One mpv process plus its IPC connection."""

    def __init__(self, pipe_name: str = PIPE_MAIN, *, primary: bool = True) -> None:
        self.pipe_name = pipe_name
        self.primary = primary
        self.proc: subprocess.Popen | None = None
        self._handle = None
        self._read_event = None
        self._reader: threading.Thread | None = None
        # kept alive: the kernel writes into these during a pending read
        self._ov = _OVERLAPPED()
        self._chunk = ctypes.create_string_buffer(65536)
        self._gen = 0
        self._req_id = 0
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.broken = False
        self._event_handlers: list = []
        self.mpv_path = shutil.which("mpv") or "mpv"

    # -- lifecycle --
    def spawn(self, volume: int = 70, extra_args: list[str] | None = None) -> None:
        self._gen += 1
        gen = self._gen
        args = [self.mpv_path, "--no-video", "--idle=yes", "--no-terminal",
                "--volume-max=150", f"--volume={int(volume)}",
                f"--input-ipc-server={self.pipe_name}"]
        if self.primary:
            args += ["--media-controls=yes", "--input-media-keys=yes"]
        else:
            args += ["--no-config", "--media-controls=no"]
        args += (extra_args or [])
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     creationflags=CREATE_NO_WINDOW)
        log.info("mpv launched (pid=%s, %s)", self.proc.pid, self.pipe_name)

        handle = None
        for _ in range(24):
            time.sleep(0.4)
            try:
                handle = _open_pipe(self.pipe_name)
                break
            except Exception:
                handle = None
        if not handle:
            raise RuntimeError(f"mpv IPC pipe {self.pipe_name} never appeared")

        if self._read_event:
            try:
                _K32.CloseHandle(self._read_event)
            except Exception:
                pass
        self._handle = handle
        self._read_event = _K32.CreateEventW(None, True, False, None)
        self.broken = False
        self._reader = threading.Thread(target=self._read_loop, args=(gen,),
                                        daemon=True)
        self._reader.start()
        log.info("connected to %s", self.pipe_name)

    def close(self) -> None:
        """Shut down cleanly: cancel the pending read and let the reader thread
        actually exit BEFORE the handle goes away."""
        self._stop.set()
        h, self._handle = self._handle, None
        if h:
            try:
                _K32.CancelIoEx(ctypes.c_void_p(h), None)
            except Exception:
                pass
        reader = self._reader
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        if h:
            try:
                _K32.CloseHandle(ctypes.c_void_p(h))
            except Exception:
                pass
        if self._read_event:
            try:
                _K32.CloseHandle(self._read_event)
            except Exception:
                pass
            self._read_event = None
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and not self.broken)

    # -- reading --
    def on_event(self, fn) -> None:
        self._event_handlers.append(fn)

    def _read_loop(self, gen: int) -> None:
        buf = b""
        size = 65536
        chunk = self._chunk
        ov = self._ov
        while not self._stop.is_set() and gen == self._gen:
            handle = self._handle
            if not handle:
                return
            ov.hEvent = self._read_event
            _K32.ResetEvent(self._read_event)
            read = wintypes.DWORD(0)
            ok = _K32.ReadFile(handle, chunk, size, ctypes.byref(read),
                               ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_IO_PENDING:
                    if _K32.WaitForSingleObject(self._read_event, 1000) != 0:
                        continue
                    if not _K32.GetOverlappedResult(handle, ctypes.byref(ov),
                                                    ctypes.byref(read), False):
                        self._mark_broken(); return
                elif err in _PIPE_DEAD:
                    self._mark_broken(); return
                else:
                    continue
            n = read.value
            if not n:
                continue
            buf += chunk.raw[:n]
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return
        rid = msg.get("request_id")
        if rid is not None:
            with self._lock:
                fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if msg.get("event"):
            for fn in list(self._event_handlers):
                try:
                    fn(msg)
                except Exception:
                    pass

    def _mark_broken(self) -> None:
        self._handle = None
        if not self.broken:
            self.broken = True
            log.warning("%s IPC lost", self.pipe_name)
        with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()

    # -- writing --
    @staticmethod
    def _coerce(v):
        # bools must stay bools; mpv rejects ints for flag properties
        if isinstance(v, bool):
            return v
        return v

    def command(self, *args, timeout: float = 4.0, wait: bool = True):
        """Send a command; return mpv's reply data (or None)."""
        if not self._handle:
            return None
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            fut: Future = Future()
            if wait:
                self._pending[rid] = fut
        payload = json.dumps({
            "command": [self._coerce(a) for a in args],
            "request_id": rid,
        }).encode() + b"\n"
        try:
            _write(self._handle, payload)
        except Exception as exc:
            with self._lock:
                self._pending.pop(rid, None)
            if is_pipe_dead(exc):
                self._mark_broken()
                return None
            log.debug("write failed: %s", exc)
            return None
        if not wait:
            return None
        try:
            msg = fut.result(timeout=timeout)
        except Exception:
            with self._lock:
                self._pending.pop(rid, None)
            return None
        if msg.get("error") not in (None, "success"):
            log.debug("mpv error for %s: %s", args, msg.get("error"))
            return None
        return msg.get("data")

    def fire(self, *args) -> None:
        """Send without waiting for a reply."""
        self.command(*args, wait=False)

    def get(self, prop: str, default=None):
        val = self.command("get_property", prop)
        return default if val is None else val

    def get_many(self, props: list[str]) -> dict:
        return {p: self.command("get_property", p) for p in props}

    def set(self, prop: str, value) -> None:
        self.command("set_property", prop, value, wait=False)
