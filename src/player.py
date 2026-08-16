r"""Music playback control via mpv IPC over a Windows named pipe.

One persistent mpv process is started at server startup. All commands are
sent as JSON over \\.\pipe\mpvsocket using a background reader thread
that dispatches replies (by request_id) and unsolicited events.

A SINGLE handle is opened with GENERIC_READ | GENERIC_WRITE for both
directions, since mpv replies on the same pipe connection the command
arrived on (a second handle would be a separate connection and never
receive the replies).

The handle is opened with FILE_FLAG_OVERLAPPED (asynchronous I/O). This is
essential: with a *synchronous* handle, Windows serialises operations per
handle, so the reader thread's blocking ReadFile (which waits until mpv
sends data) would block every WriteFile behind it. Since mpv sends nothing
until it receives a command, that deadlocks — no command can ever be sent.
Overlapped I/O lets the concurrent read and write proceed independently.
"""

import atexit
import ctypes
import glob
import json
import os
import re
import shutil
import tempfile
import subprocess
import threading
import time
from concurrent.futures import Future
from ctypes import wintypes

from paths import data_dir


# --------------------------------------------------------------------------
# Windows API helpers (ctypes) - used to open named pipes.
# --------------------------------------------------------------------------

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_ALL = 0x00000003  # FILE_SHARE_READ | FILE_SHARE_WRITE
OPEN_EXISTING    = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_IO_PENDING = 997
ERROR_IO_INCOMPLETE = 996
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT  = 0x00000102
INFINITE = 0xFFFFFFFF


class _OVERLAPPED(ctypes.Structure):
    """Win32 OVERLAPPED structure for asynchronous I/O."""
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


_KERNEL32.CreateFileW.restype = wintypes.HANDLE
_KERNEL32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p,   # LPSECURITY_ATTRIBUTES (pass None)
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]

_KERNEL32.ReadFile.restype = wintypes.BOOL
_KERNEL32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
]

_KERNEL32.WriteFile.restype = wintypes.BOOL
_KERNEL32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
]

_KERNEL32.CloseHandle.restype = wintypes.BOOL
_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]

_KERNEL32.CreateEventW.restype = wintypes.HANDLE
_KERNEL32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
]

_KERNEL32.GetOverlappedResult.restype = wintypes.BOOL
_KERNEL32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL
]

_KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
_KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

_KERNEL32.CancelIoEx.restype = wintypes.BOOL
_KERNEL32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]

_KERNEL32.ResetEvent.restype = wintypes.BOOL
_KERNEL32.ResetEvent.argtypes = [wintypes.HANDLE]


# Writes may be issued from several threads (command threads and the reader
# thread's fallback path). Serialise them so their byte streams never
# interleave on the byte-mode pipe.
_write_lock = threading.Lock()


def _open_pipe(name):
    """Open an existing named pipe for overlapped (async) read+write."""
    handle = _KERNEL32.CreateFileW(
        name, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_ALL,
        None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None
    )
    if handle == INVALID_HANDLE_VALUE or handle == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _write(handle, data_bytes):
    """Write bytes to the pipe using overlapped I/O, waiting for completion.

    Overlapped WriteFile does not serialise behind the reader thread's
    pending overlapped ReadFile, so this never deadlocks.
    """
    length = len(data_bytes)
    buf = ctypes.create_string_buffer(data_bytes, length)

    with _write_lock:
        event = _KERNEL32.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ol = _OVERLAPPED()
            ol.hEvent = event
            bytes_written = wintypes.DWORD(0)
            ok = _KERNEL32.WriteFile(handle, buf, length,
                                     ctypes.byref(bytes_written),
                                     ctypes.byref(ol))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    raise ctypes.WinError(err)
                # Wait for the async write to finish.
                _KERNEL32.WaitForSingleObject(event, INFINITE)
                ok = _KERNEL32.GetOverlappedResult(
                    handle, ctypes.byref(ol),
                    ctypes.byref(bytes_written), True
                )
                if not ok:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _KERNEL32.CloseHandle(event)


def _kill_stray_mpv():
    # Kill orphaned mpv from a prior crash/force-close (matches our pipe name
    # only). Returns True if any were killed.
    try:
        ps = ("$p=Get-CimInstance Win32_Process -Filter \"Name='mpv.exe'\" | "
              "Where-Object { $_.CommandLine -match 'mpvsocket' }; "
              "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }; ($p | Measure-Object).Count")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             timeout=12, capture_output=True, text=True,
                             creationflags=0x08000000)
        return (out.stdout or "").strip() not in ("", "0")
    except Exception:
        return False


# --------------------------------------------------------------------------
# PlayerManager
# --------------------------------------------------------------------------

class PlayerManager:
    """Manages a single persistent mpv process via IPC."""

    def __init__(self):
        self._pipe_handle = None
        self._lock = threading.Lock()
        self._pending = {}  # request_id -> Future
        self._mpv_process = None
        self._ready = threading.Event()
        self._reader_stop = threading.Event()
        self._reader_thread = None
        self._read_event = None
        # Secondary mpv: plays the outgoing track's tail during a true crossfade
        self._mpv2_process = None
        self._pipe2_handle = None
        self._crossfade_until = 0.0       # monotonic; suppresses re-trigger
        # Download-then-play state
        self._ytdlp_path = None
        self._ytdlp_opts = []          # shared yt-dlp CLI options (cookies, etc.)
        self._cache_dir = None
        self._download_lock = threading.Lock()  # serialise yt-dlp (avoid rate limits)
        self._append_seq = 0           # generation counter to cancel stale prefetch
        # Track metadata: local file path -> {video_id, title, artist, album}
        self._track_meta = {}
        self._meta_lock = threading.Lock()
        # Auto-queue (Spotify-style radio)
        self._autoqueue_enabled = False
        self._autoqueue_provider = None   # callable(seed_video_id, exclude_ids) -> [track dicts]
        self._autoqueue_threshold = 2     # emergency floor: refill now if this few remain
        self._autoqueue_batch = 4         # songs added per refill
        self._autoqueue_target = 15       # base lookahead (grows while you skip)
        self._aq_min_interval = 8.0       # refill delay when near the end
        self._aq_max_interval = 60.0      # refill delay when well-buffered
        self._skip_times = []             # recent skip timestamps (accelerate refills)
        self._last_refill_ts = 0.0
        self._autoqueue_lock = threading.Lock()
        self._played_ids = []             # ordered history of video_ids (dedup radio)
        self._monitor_thread = None
        self._autoqueue_hold = False      # album/artist: no radio until exhausted
        self._seed_artists = []           # bias next radio batch to these bands
        # Audio / persistent settings
        self._state_file = os.path.join(data_dir(), "player_state.json")
        self._settings = {
            "volume": 70, "eq": "flat", "normalize": False,
            "crossfade": 0, "repeat": "off",
        }
        self._like_provider = None        # callable(video_id, exclude) -> [tracks]
        self._sleep_timer = None          # threading.Timer
        self._sleep_deadline = None       # monotonic ts when sleep fires
        self._announce_enabled = True     # speak the song when you request one
        self._tts_voice = "en-US-AriaNeural"   # edge-tts neural voice
        self._ducking = False             # true while announce lowers the volume
        # Liked songs (drives taste-based recommendations)
        self._liked_file = os.path.join(data_dir(), "liked_songs.json")
        self._liked = []                  # [{video_id,title,artist,album,thumbnail,ts}]
        self._liked_ids = set()
        self._liked_lock = threading.Lock()
        # Play history + skip stats (preference engine / smart shuffle)
        self._stats_file = os.path.join(data_dir(), "play_stats.json")
        self._history = []                # recent listened-to ids (no-repeat + context)
        self._history_size = 100          # tunable via config
        self._song_stats = {}             # video_id -> [plays, skips]
        self._artist_stats = {}           # artist(lower) -> [plays, skips]
        self._stats_lock = threading.Lock()
        # Tunable smart-shuffle weights (overridable from config)
        self._weights = {
            "liked_boost": 2.0, "playthrough": 0.5, "skip_penalty": 0.8,
            "song_play": 0.4, "jitter": 1.0, "liked_seed_prob": 0.35,
            "context_songs": 3, "same_artist_boost": 3.0,
        }
        # currently-watched track (to detect skip vs play-through)
        self._watch_id = None
        self._watch_meta = {}
        self._watch_pos = 0
        self._watch_dur = 0
        self._stats_dirty = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, config=None):
        """Launch mpv, open the named pipe (single R/W handle), start reader.

        *config* (optional) may carry ``cookies_from_browser`` and
        ``cookies_file`` keys. YouTube now gates most streams behind a
        "confirm you're not a bot" check, so yt-dlp needs cookies from a
        logged-in session to resolve audio. These are forwarded to yt-dlp
        via mpv's ``--ytdl-raw-options`` hook.
        """
        config = config or {}
        self._load_state()          # persisted volume / EQ / normalize / repeat
        self._history_size = int(config.get("history_size", 100) or 100)
        self._announce_enabled = bool(config.get("announce", True))
        self._tts_voice = (config.get("tts_voice") or "en-US-AriaNeural").strip()
        for k in self._weights:
            if config.get(f"queue_{k}") is not None:
                self._weights[k] = type(self._weights[k])(config[f"queue_{k}"])
        self._load_liked()          # persistent taste profile
        self._load_stats()          # play history + skip stats
        # Kill orphaned mpv from a force-close, then let Windows release the pipe
        # names before the new mpv binds them (avoids a restart pipe race).
        if _kill_stray_mpv():
            time.sleep(0.6)

        mpv_path = shutil.which("mpv")
        if not mpv_path:
            raise RuntimeError(
                "mpv not found on PATH. Install with: winget install mpv"
            )

        # mpv only ever plays LOCAL files (each track is downloaded first), so
        # it needs no ytdl/cookie options. media-controls registers it with the
        # Windows media overlay + hardware media keys; it reads the title/artist
        # from each file's embedded tags.
        cmd = [
            mpv_path,
            "--no-video", "--idle=yes", "--no-terminal",
            "--volume-max=150",                       # allow boosting above 100
            f"--volume={int(self._settings.get('volume', 70))}",
            "--media-controls=yes", "--input-media-keys=yes",
            r"--input-ipc-server=\\.\pipe\mpvsocket",
        ]

        # Options used to DOWNLOAD each track with yt-dlp: cookies (to pass the
        # bot check), a JS runtime (Node — solves YouTube's signature challenge)
        # and the player client (tv gives a fetchable progressive stream).
        cookies_browser = (config.get("cookies_from_browser") or "").strip()
        cookies_file = (config.get("cookies_file") or "").strip()
        if cookies_file and not os.path.isfile(cookies_file):
            print(f"WARNING: cookies_file '{cookies_file}' not found — ignoring.")
            cookies_file = ""
        if not cookies_browser and not cookies_file:
            print("WARNING: no cookies configured — YouTube may block downloads.")
        js_runtime = (config.get("js_runtime") or "").strip()
        player_client = (config.get("player_client") or "").strip()
        self._ytdlp_path = shutil.which("yt-dlp")
        opts = []
        if cookies_browser: opts += ["--cookies-from-browser", cookies_browser]
        if cookies_file:    opts += ["--cookies", cookies_file]
        if js_runtime:      opts += ["--js-runtimes", js_runtime]
        if player_client:   opts += ["--extractor-args", f"youtube:player_client={player_client}"]
        for extra in config.get("ytdl_raw_options", []) or []:
            extra = str(extra).strip()
            if not extra:
                continue
            if "=" in extra:
                k, v = extra.split("=", 1)
                opts += [f"--{k}", v]
            else:
                opts.append(f"--{extra}")
        self._ytdlp_opts = opts
        self._cache_dir = os.path.join(tempfile.gettempdir(), "mrs_audio_cache")
        os.makedirs(self._cache_dir, exist_ok=True)
        if not self._ytdlp_path:
            print("WARNING: yt-dlp not found on PATH; playback will fail.")

        self._mpv_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=0x08000000,   # CREATE_NO_WINDOW (mpv resolves to mpv.com)
        )
        print(f"mpv launched (pid={self._mpv_process.pid})")

        pipe_name = r"\\.\pipe\mpvsocket"

        # Wait for the pipe to appear - try each 0.5s
        pipe_handle = None
        for _ in range(20):
            time.sleep(0.5)
            try:
                pipe_handle = _open_pipe(pipe_name)
                print("Connected to mpv IPC pipe (single R/W handle).")
                break
            except Exception:
                pipe_handle = None

        if not pipe_handle:
            raise RuntimeError("mpv IPC pipe did not appear within 10 s")

        self._pipe_handle = pipe_handle
        # Manual-reset event dedicated to the reader thread's overlapped reads.
        self._read_event = _KERNEL32.CreateEventW(None, True, False, None)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # ── Secondary mpv (true-crossfade tail player) ──
        # Plays the outgoing track's final seconds, fading out, while the
        # primary jumps to the next track fading in. media-controls off so it
        # never creates a competing Windows media session.
        try:
            cmd2 = [mpv_path, "--no-video", "--idle=yes", "--no-terminal",
                    "--no-config", "--media-controls=no", "--volume=100",
                    r"--input-ipc-server=\\.\pipe\mpvsocket2"]
            self._mpv2_process = subprocess.Popen(
                cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=0x08000000)   # CREATE_NO_WINDOW
            for _ in range(20):
                time.sleep(0.3)
                try:
                    self._pipe2_handle = _open_pipe(r"\\.\pipe\mpvsocket2")
                    break
                except Exception:
                    self._pipe2_handle = None
            if self._pipe2_handle:
                print("Crossfade engine ready (secondary mpv connected).")
        except Exception:
            self._pipe2_handle = None

        # Auto-queue config + monitor thread
        self._autoqueue_enabled = bool(config.get("auto_queue", True))
        self._autoqueue_batch = int(config.get("auto_queue_batch", 4) or 4)
        self._autoqueue_threshold = int(config.get("auto_queue_threshold", 2) or 2)
        self._autoqueue_target = int(config.get("auto_queue_target", 15) or 15)
        self._aq_min_interval = float(config.get("auto_queue_min_interval", 8) or 8)
        self._aq_max_interval = float(config.get("auto_queue_max_interval", 60) or 60)
        self._monitor_thread = threading.Thread(target=self._autoqueue_loop, daemon=True)
        self._monitor_thread.start()
        if self._autoqueue_enabled:
            print(f"Auto-queue enabled (keep ~{self._autoqueue_target}+ ahead, "
                  f"+{self._autoqueue_batch} per refill, "
                  f"{self._aq_min_interval:.0f}-{self._aq_max_interval:.0f}s pacing)")

        # Restore persisted volume / EQ / normalization / repeat.
        self.apply_saved_settings()

        self._ready.set()
        atexit.register(self.stop)

    def stop(self):
        """Terminate mpv and close the pipe."""
        try:
            self._save_stats()
        except Exception:
            pass
        self._reader_stop.set()
        # Cancel any pending overlapped read so the reader thread wakes up.
        if self._pipe_handle:
            try: _KERNEL32.CancelIoEx(self._pipe_handle, None)
            except Exception: pass
        if self._read_event:
            try: _KERNEL32.CloseHandle(self._read_event)
            except Exception: pass
            self._read_event = None
        if self._pipe_handle:
            try: _KERNEL32.CloseHandle(self._pipe_handle)
            except Exception: pass
            self._pipe_handle = None
        if self._pipe2_handle:
            try: _KERNEL32.CloseHandle(self._pipe2_handle)
            except Exception: pass
            self._pipe2_handle = None
        for proc in (self._mpv_process, self._mpv2_process):
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        self._mpv_process = None
        self._mpv2_process = None

    # ------------------------------------------------------------------
    # IPC reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """Background thread: continuously read from the pipe via overlapped ReadFile.

        Uses overlapped I/O with a dedicated event so this blocking read does
        not serialise with (and thereby block) writes on the same handle.
        Partial lines are buffered across reads until a newline is seen.
        """
        buf_size = 65536
        buf = ctypes.create_string_buffer(buf_size)
        partial = b""

        while not self._reader_stop.is_set():
            try:
                _KERNEL32.ResetEvent(self._read_event)
                ol = _OVERLAPPED()
                ol.hEvent = self._read_event
                bytes_read = wintypes.DWORD(0)
                ok = _KERNEL32.ReadFile(self._pipe_handle, buf, buf_size,
                                        ctypes.byref(bytes_read),
                                        ctypes.byref(ol))
                if not ok:
                    err = ctypes.get_last_error()
                    if err != ERROR_IO_PENDING:
                        break  # pipe closed or fatal error
                    # Wait for data, checking the stop flag periodically.
                    while not self._reader_stop.is_set():
                        rc = _KERNEL32.WaitForSingleObject(self._read_event, 250)
                        if rc == WAIT_OBJECT_0:
                            break
                        if rc != WAIT_TIMEOUT:
                            return
                    if self._reader_stop.is_set():
                        _KERNEL32.CancelIoEx(self._pipe_handle, ctypes.byref(ol))
                        break
                    if not _KERNEL32.GetOverlappedResult(
                        self._pipe_handle, ctypes.byref(ol),
                        ctypes.byref(bytes_read), True
                    ):
                        break

                if bytes_read.value == 0:
                    continue

                partial += buf.raw[:bytes_read.value]
                # Process complete lines only; keep any trailing partial line.
                *lines, partial = partial.split(b"\n")
                for raw_line in lines:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(msg, dict):
                        req_id = msg.get("request_id")
                        if os.environ.get("MRS_IPC_DEBUG"):
                            print(f"[rdr] msg={msg}", flush=True)
                        if req_id is not None:
                            with self._lock:
                                future = self._pending.pop(req_id, None)
                            if os.environ.get("MRS_IPC_DEBUG"):
                                print(f"[rdr] dispatch id={req_id} matched={future is not None}", flush=True)
                            if future and not future.done():
                                error = msg.get("error")
                                if error and error != "success":
                                    future.set_exception(RuntimeError(error))
                                else:
                                    future.set_result(msg.get("data"))
            except Exception:
                break

    # ------------------------------------------------------------------
    # Command helper
    # ------------------------------------------------------------------

    _next_id = 0

    @staticmethod
    def _convert_arg(a):
        """Pass a Python value through to mpv's JSON IPC unchanged.

        Booleans MUST stay booleans: mpv's flag properties (e.g. ``pause``)
        reject an integer value with "error accessing property" and require a
        real JSON ``true``/``false``.
        """
        return a

    def _cmd(self, command, *args):
        """Send a single mpv IPC command (positional format) and return the data field.

        mpv's IPC protocol is a positional format:
          {"command": [command, arg1, arg2, ...], "request_id": N}
        Reply:
          {"error": "success", "data": <result>, "request_id": N}
        """
        converted = [self._convert_arg(a) for a in args]

        with self._lock:
            req_id = self._next_id
            self._next_id += 1

        future = Future()
        with self._lock:
            self._pending[req_id] = future

        msg = {
            "command": [command] + converted,
            "request_id": req_id,
        }
        payload = json.dumps(msg) + "\n"
        if os.environ.get("MRS_IPC_DEBUG"):
            print(f"[cmd] send id={req_id} {command} {converted}", flush=True)
        try:
            _write(self._pipe_handle, payload.encode("utf-8"))
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise

        try:
            r = future.result(timeout=3)
            if os.environ.get("MRS_IPC_DEBUG"):
                print(f"[cmd] recv id={req_id} {command} -> {r!r}", flush=True)
            return r
        except Exception as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            if os.environ.get("MRS_IPC_DEBUG"):
                print(f"[cmd] TIMEOUT id={req_id} {command} pending={sorted(self._pending)}", flush=True)
            raise TimeoutError(f"mpv IPC command '{command}' timed out") from exc

    def _get_properties(self, names):
        """Read multiple mpv properties in one IPC exchange.

        Returns a dict mapping each property name to its value.  This is far
        faster than calling ``_cmd("get_property", ...)`` for each name
        individually (each call has up to a 3-second timeout).
        """
        futures = {}
        with self._lock:
            for name in names:
                req_id = self._next_id
                self._next_id += 1
                future = Future()
                self._pending[req_id] = future
                futures[name] = (req_id, future)

        # Build and send each command
        for name, (req_id, future) in futures.items():
            msg = {
                "command": ["get_property", name],
                "request_id": req_id,
            }
            payload = json.dumps(msg) + "\n"
            try:
                _write(self._pipe_handle, payload.encode("utf-8"))
            except Exception:
                pass

        result = {}
        for name, (req_id, future) in futures.items():
            try:
                result[name] = future.result(timeout=2)
            except Exception as exc:
                result[name] = None
        return result


    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    # ---- download-then-play helpers ----------------------------------

    @staticmethod
    def _safe_id(video_id):
        """Filesystem-safe cache key (YouTube ids pass through unchanged)."""
        return re.sub(r'[^A-Za-z0-9_-]', "_", str(video_id))[:80]

    def _cached_path(self, video_id):
        """Return an existing, non-empty cached audio file for *video_id*."""
        vid = self._safe_id(video_id)
        for f in glob.glob(os.path.join(self._cache_dir, f"{vid}.*")):
            if (not f.endswith(".part") and os.path.isfile(f)
                    and os.path.getsize(f) > 0):
                return f
        return None

    def _download_track(self, video_id, meta=None):
        """Download a track's audio to the cache dir; return local path or None.

        Downloads from the track's ``url`` (SoundCloud/Bandcamp/etc.) when the
        metadata carries one, otherwise from YouTube. yt-dlp fetches reliably,
        avoiding the HTTP 403 ffmpeg hits on direct YouTube stream URLs. *meta*
        (title/artist/album) is embedded so Windows SMTC and the UI show it.
        """
        if not self._ytdlp_path or not video_id:
            return None
        cached = self._cached_path(video_id)
        if cached:
            self._embed_metadata(cached, meta)
            self._store_meta(cached, video_id, meta)
            return cached
        url = (meta or {}).get("url") or f"https://www.youtube.com/watch?v={video_id}"
        out_tmpl = os.path.join(self._cache_dir, f"{self._safe_id(video_id)}.%(ext)s")
        cmd = ([self._ytdlp_path, "-f", "bestaudio/best", "--no-playlist",
                "--no-part", "-o", out_tmpl] + self._ytdlp_opts + ["--", url])
        try:
            with self._download_lock:
                subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=180, creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
        except Exception:
            return None
        path = self._cached_path(video_id)
        if path:
            self._embed_metadata(path, meta)
            self._store_meta(path, video_id, meta)
        return path

    def _store_meta(self, path, video_id, meta):
        """Remember a track's metadata keyed by its local file path."""
        meta = meta or {}
        with self._meta_lock:
            self._track_meta[path] = {
                "video_id": video_id,
                "title": meta.get("title") or "",
                "artist": meta.get("artist") or "",
                "album": meta.get("album") or "",
                "art": meta.get("thumbnail") or "",
            }

    @staticmethod
    def _embed_metadata(path, meta):
        """Write title/artist/album tags so mpv's media-title (and thus the
        Windows SMTC overlay) shows the real song rather than the file name."""
        if not meta:
            return
        title = meta.get("title") or ""
        artist = meta.get("artist") or ""
        album = meta.get("album") or ""
        if not (title or artist):
            return
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".m4a", ".mp4", ".aac"):
                from mutagen.mp4 import MP4
                f = MP4(path)
                if title:  f["\xa9nam"] = [title]
                if artist: f["\xa9ART"] = [artist]
                if album:  f["\xa9alb"] = [album]
                f.save()
            elif ext in (".opus", ".ogg", ".webm"):
                from mutagen.oggopus import OggOpus
                f = OggOpus(path)
                if title:  f["title"] = [title]
                if artist: f["artist"] = [artist]
                if album:  f["album"] = [album]
                f.save()
            else:
                from mutagen import File as MFile
                f = MFile(path, easy=True)
                if f is not None:
                    if title:  f["title"] = [title]
                    if artist: f["artist"] = [artist]
                    if album:  f["album"] = [album]
                    f.save()
        except Exception:
            pass  # metadata is a nicety; never fail playback over it

    def _download_with_fallbacks(self, track, fallbacks):
        """Download *track* (dict), trying each fallback id if it fails."""
        path = self._download_track(track["video_id"], track)
        if path:
            return path
        for fb in fallbacks:
            path = self._download_track(fb, track)
            if path:
                return path
        return None

    # ── True crossfade via the secondary mpv ──────────────────────────

    def _secondary(self, *cmd):
        """Fire a command at the secondary (tail) mpv, no reply expected."""
        if not self._pipe2_handle:
            return
        try:
            _write(self._pipe2_handle, (json.dumps({"command": list(cmd)}) + "\n").encode())
        except Exception:
            pass

    def _ramp_secondary_out(self, vol, cf):
        """Fade the secondary mpv (the outgoing tail) from *vol* to 0 over cf s."""
        steps = max(4, int(cf * 8))
        for i in range(steps):
            if self._reader_stop.is_set():
                break
            self._secondary("set_property", "volume", int(vol * (1 - (i + 1) / steps)))
            time.sleep(cf / steps)
        self._secondary("stop")

    def _start_tail(self, out_path, out_pos, cf):
        # Secondary mpv plays the outgoing tail, fading out. Seek must wait for
        # the file to load, else it lands on nothing (this broke crossfade).
        vol = int(self._settings.get("volume", 70))
        self._crossfade_until = time.monotonic() + cf + 1.0
        self._secondary("loadfile", out_path, "replace")

        def _tail():
            time.sleep(0.18)                                  # let the file load
            self._secondary("set_property", "volume", vol)
            self._secondary("seek", out_pos, "absolute")
            self._secondary("set_property", "af", f"afade=t=out:st=0:d={cf}")
            self._ramp_secondary_out(vol, cf)

        threading.Thread(target=_tail, daemon=True).start()

    def _crossfade_replace(self, path):
        """Load *path* on the primary. When crossfade is on and something is
        already playing, the primary jumps to the new song (fading in via the
        audio filter) while the secondary plays the outgoing tail fading out —
        a real overlapping crossfade."""
        cf = self._settings.get("crossfade") or 0
        props = {}
        try:
            props = self._get_properties(["path", "time-pos", "idle-active"])
        except Exception:
            pass
        if cf <= 0 or props.get("idle-active") or not self._pipe2_handle or not props.get("path"):
            self._cmd("loadfile", path, "replace")
            return
        self._start_tail(props.get("path"), props.get("time-pos") or 0, cf)
        self._cmd("loadfile", path, "replace")   # new track fades in (afade)

    def _maybe_crossfade(self):
        """Auto-advance crossfade: when the current track is within *cf* seconds
        of the end and the next track is ready, start the tail on the secondary
        and advance the primary early so the two overlap."""
        cf = self._settings.get("crossfade") or 0
        if cf <= 0 or not self._pipe2_handle or time.monotonic() < self._crossfade_until:
            return
        try:
            props = self._get_properties(
                ["path", "time-pos", "duration", "playlist-pos", "playlist-count", "pause"])
        except Exception:
            return
        pos, dur = props.get("time-pos"), props.get("duration")
        ppos, pcount = props.get("playlist-pos"), props.get("playlist-count") or 0
        if props.get("pause") or pos is None or not dur or ppos is None:
            return
        if ppos + 1 >= pcount or pos < dur - cf:
            return
        try:
            pl = self._cmd("get_property", "playlist") or []
            next_ready = ppos + 1 < len(pl) and pl[ppos + 1].get("filename")
        except Exception:
            next_ready = False
        if not next_ready or not props.get("path"):
            return
        self._start_tail(props.get("path"), pos, cf)  # outgoing tail fades out
        self._cmd("playlist-next")                    # new track fades in

    def _prefetch_append(self, tracks, seq):
        """Background: download each remaining track (dict) and append it.

        *seq* guards against a newer play() superseding this prefetch run.
        """
        for tr in tracks:
            if self._reader_stop.is_set() or seq != self._append_seq:
                return
            path = self._download_track(tr["video_id"], tr)
            if not path or seq != self._append_seq:
                if seq != self._append_seq:
                    return
                continue
            try:
                self._cmd("loadfile", path, "append")
                self._remember_played(tr["video_id"])
            except Exception:
                pass

    def _remember_played(self, video_id):
        """Track recently-queued ids so auto-queue radio doesn't repeat them."""
        with self._autoqueue_lock:
            if video_id in self._played_ids:
                self._played_ids.remove(video_id)
            self._played_ids.append(video_id)
            if len(self._played_ids) > 200:
                self._played_ids = self._played_ids[-200:]

    def play(self, plan):
        """Play a search-plan dict.

        *plan* is produced by ``search.resolve`` and has keys:
            tracks  - list of dicts with at least ``video_id``
            fallbacks - list of alternate video_ids for the first track only
            shuffle - bool
            mode    - "play" | "queue" | "next"
        """
        tracks = [t for t in plan.get("tracks", []) if t.get("video_id")]
        if not tracks:
            return {"status": "error", "message": "No tracks in plan"}

        mode = plan.get("mode", "play")
        shuffle = plan.get("shuffle", False)
        fallbacks = list(plan.get("fallbacks", []))
        kind = plan.get("kind")

        if shuffle:
            import random
            tracks = list(tracks)
            random.shuffle(tracks)

        # Album/artist: hold radio until the pure queue runs out.
        if mode == "play":
            self._autoqueue_hold = kind in ("album", "artist") and len(tracks) > 1

        # New generation: cancels any in-flight prefetch from a prior request.
        with self._lock:
            self._append_seq += 1
            seq = self._append_seq

        if mode == "play":
            first = self._download_with_fallbacks(tracks[0], fallbacks)
            if not first:
                return {"status": "error",
                        "message": "Could not download the first track from YouTube"}
            self._cmd("playlist-clear")
            self._crossfade_replace(first)   # fade out current, swap, fade in
            self._cmd("set_property", "pause", False)  # a request always plays
            self._remember_played(tracks[0]["video_id"])
            rest = tracks[1:]
        elif mode == "next":
            first = self._download_with_fallbacks(tracks[0], fallbacks)
            if not first:
                return {"status": "error",
                        "message": "Could not download the track from YouTube"}
            current = self._current_playlist_pos()
            total_before = self._playlist_count()
            self._cmd("loadfile", first, "append")
            self._cmd("playlist-move", total_before, current + 1)
            self._remember_played(tracks[0]["video_id"])
            rest = tracks[1:]
        else:  # queue
            rest = tracks

        if rest:
            threading.Thread(target=self._prefetch_append,
                             args=(rest, seq), daemon=True).start()

        return {
            "status": "ok",
            "tracks": len(tracks),
            "mode": mode,
            "shuffle": shuffle,
        }

    # ------------------------------------------------------------------
    # Auto-queue (Spotify-style radio)
    # ------------------------------------------------------------------

    def set_autoqueue_provider(self, provider):
        """Register a callable(seed_video_id, exclude_ids) -> [track dicts].

        Called when the queue is about to run dry to fetch related songs.
        """
        self._autoqueue_provider = provider

    def set_autoqueue_enabled(self, enabled):
        self._autoqueue_enabled = bool(enabled)

    def _persist_volume_if_changed(self):
        # Save the volume whenever it changes (slider, media keys, mixer) so it
        # survives a restart. Skip while announce is ducking it.
        if self._ducking:
            return
        try:
            v = self._get_properties(["volume"]).get("volume")
        except Exception:
            return
        if v is None:
            return
        v = int(round(v))
        if abs(v - int(self._settings.get("volume", 70))) >= 1:
            self._settings["volume"] = v
            self._save_state()

    def _current_video_id(self):
        """video_id of the currently-playing file, via its cached metadata."""
        props = self._get_properties(["path"])
        path = props.get("path")
        if not path:
            return None
        with self._meta_lock:
            meta = self._track_meta.get(path)
        return meta.get("video_id") if meta else None

    def _current_artist(self):
        """Lower-cased primary artist of the currently-playing file, if known."""
        props = self._get_properties(["path"])
        path = props.get("path")
        if not path:
            return None
        with self._meta_lock:
            meta = self._track_meta.get(path)
        if not meta:
            return None
        return (meta.get("artist") or "").split(",")[0].strip().lower() or None

    def _autoqueue_loop(self):
        """Background: keep the queue full when auto-queue is on.

        When the playlist is within ``_autoqueue_threshold`` tracks of the end
        and something is playing, fetch a batch of related songs (radio seeded
        by the current track) and append them.
        """
        while not self._reader_stop.is_set():
            time.sleep(1)
            # Always track plays vs skips (feeds the preference engine) and run
            # the auto-advance crossfade check.
            self._watch_track()
            self._maybe_crossfade()
            self._persist_volume_if_changed()
            if not self._autoqueue_enabled or not self._autoqueue_provider:
                continue
            try:
                props = self._get_properties(
                    ["playlist-count", "playlist-pos", "idle-active"])
                if props.get("idle-active"):
                    continue
                count = props.get("playlist-count") or 0
                pos = props.get("playlist-pos")
                if pos is None or pos < 0:
                    continue
                remaining = count - pos - 1
                # Hold: no radio until the pure album/artist queue is exhausted.
                if self._autoqueue_hold:
                    if remaining > 0:
                        continue
                    self._autoqueue_hold = False
                now = time.monotonic()
                recent_skips = sum(1 for t in self._skip_times if now - t < 60)
                # Queue grows dynamically — the more you're skipping, the deeper
                # the buffer we keep ahead of you.
                target = self._autoqueue_target + min(12, recent_skips * 3)
                if remaining >= target:
                    continue
                # Refill delay scales with how much buffer is left: short near the
                # end, long when well-stocked, shorter still while skipping. Never
                # let it actually run dry.
                frac = remaining / float(max(1, target))
                interval = self._aq_min_interval + \
                    (self._aq_max_interval - self._aq_min_interval) * frac
                if recent_skips >= 2:
                    interval /= recent_skips
                if remaining > self._autoqueue_threshold and \
                        (now - self._last_refill_ts) < interval:
                    continue
                self._last_refill_ts = now

                if not self._current_video_id():
                    continue
                with self._autoqueue_lock:
                    exclude = list(self._played_ids)
                with self._stats_lock:
                    exclude += self._history        # don't re-queue recent plays
                # Seed from the recent context (several non-skipped songs),
                # ranked by taste with recent plays dropped — "smart shuffle".
                new_tracks = self._gather_candidates(exclude)
                appended = 0
                for tr in new_tracks:
                    if self._reader_stop.is_set():
                        break
                    path = self._download_track(tr["video_id"], tr)
                    if path:
                        self._cmd("loadfile", path, "append")
                        self._remember_played(tr["video_id"])
                        appended += 1
                    if appended >= self._autoqueue_batch:
                        break
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Queue introspection
    # ------------------------------------------------------------------

    def get_queue(self):
        """Return the current mpv playlist mapped to known track metadata.

        Each entry: {position, current, title, artist, album, video_id}.
        """
        try:
            raw = self._cmd("get_property", "playlist") or []
        except Exception:
            return []
        queue = []
        for i, entry in enumerate(raw):
            path = entry.get("filename", "")
            with self._meta_lock:
                meta = self._track_meta.get(path, {})
            queue.append({
                "position": i,
                "current": bool(entry.get("current") or entry.get("playing")),
                "title": meta.get("title") or entry.get("title") or "",
                "artist": meta.get("artist") or "",
                "album": meta.get("album") or "",
                "video_id": meta.get("video_id") or "",
                "art": meta.get("art") or "",
            })
        return queue

    # ------------------------------------------------------------------
    # Persistent settings + audio engine
    # ------------------------------------------------------------------

    # EQ presets as (frequency Hz, gain dB) bands, applied via ffmpeg equalizer.
    EQ_PRESETS = {
        "flat": [],
        "bass": [(60, 6), (150, 3)],
        "bass boost": [(45, 9), (110, 5)],
        "treble": [(8000, 5), (12000, 4)],
        "vocal": [(200, -2), (1000, 3), (3000, 4)],
        "rock": [(60, 4), (250, -1), (1000, 2), (4000, 3), (8000, 4)],
        "pop": [(100, 2), (1000, 3), (8000, 2)],
        "jazz": [(100, 3), (500, 1), (5000, 2), (10000, 2)],
        "classical": [(60, 3), (400, 1), (12000, 3)],
        "electronic": [(50, 6), (200, 2), (6000, 3), (12000, 4)],
    }

    def _load_state(self):
        try:
            with open(self._state_file, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._settings.update({k: data[k] for k in self._settings if k in data})
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self._state_file, "w") as f:
                json.dump(self._settings, f)
        except Exception:
            pass

    def _build_af_chain(self):
        """Assemble the mpv audio-filter chain from EQ + normalize + fade."""
        parts = []
        cross = self._settings.get("crossfade") or 0
        if cross:
            parts.append(f"afade=t=in:st=0:d={cross}")
        for freq, gain in self.EQ_PRESETS.get(self._settings.get("eq", "flat"), []):
            parts.append(f"equalizer=f={freq}:width_type=o:width=1.5:g={gain}")
        if self._settings.get("normalize"):
            parts.append("dynaudnorm=g=7:f=250:p=0.6")
        return ",".join(parts)

    def _apply_audio(self):
        try:
            self._cmd("set_property", "af", self._build_af_chain())
        except Exception:
            pass

    def apply_saved_settings(self):
        """Apply persisted volume, EQ, normalization and repeat to mpv."""
        try:
            self._cmd("set_property", "volume", int(self._settings.get("volume", 70)))
        except Exception:
            pass
        self._apply_audio()
        self._apply_repeat()

    def set_audio(self, eq=None, normalize=None, crossfade=None):
        if eq is not None and eq in self.EQ_PRESETS:
            self._settings["eq"] = eq
        if normalize is not None:
            self._settings["normalize"] = bool(normalize)
        if crossfade is not None:
            self._settings["crossfade"] = max(0, min(12, int(crossfade)))
        self._apply_audio()
        self._save_state()
        return dict(self._settings)

    def _apply_repeat(self):
        mode = self._settings.get("repeat", "off")
        try:
            if mode == "one":
                self._cmd("set_property", "loop-file", "inf")
                self._cmd("set_property", "loop-playlist", "no")
            elif mode == "all":
                self._cmd("set_property", "loop-file", "no")
                self._cmd("set_property", "loop-playlist", "inf")
            else:
                self._cmd("set_property", "loop-file", "no")
                self._cmd("set_property", "loop-playlist", "no")
        except Exception:
            pass

    def cycle_repeat(self):
        order = ["off", "all", "one"]
        cur = self._settings.get("repeat", "off")
        self._settings["repeat"] = order[(order.index(cur) + 1) % 3] if cur in order else "all"
        self._apply_repeat()
        self._save_state()
        return self._settings["repeat"]

    def shuffle_now(self):
        try:
            self._cmd("playlist-shuffle")
        except Exception:
            pass
        return {"ok": True}

    def seek(self, pos):
        try:
            self._cmd("seek", float(pos), "absolute")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def announce(self, text):
        # Speak the song (requested tracks only), ducking the music. edge-tts
        # neural voice, falling back to the offline System.Speech voice.
        if not self._announce_enabled or not text:
            return

        def _run():
            vol = int(self._settings.get("volume", 70))
            self._ducking = True          # don't let the monitor persist the duck
            try:
                self._cmd("set_property", "volume", max(8, int(vol * 0.25)))
            except Exception:
                pass
            if not self._speak_neural(text):
                self._speak_desktop(text)
            try:
                self._cmd("set_property", "volume", vol)
            except Exception:
                pass
            self._ducking = False

        threading.Thread(target=_run, daemon=True).start()

    def _speak_neural(self, text):
        """Speak via edge-tts neural voice. Returns True on success."""
        try:
            import asyncio
            import edge_tts
        except Exception:
            return False
        mp3 = os.path.join(tempfile.gettempdir(), f"mrs_tts_{os.getpid()}.mp3")
        try:
            async def _gen():
                await edge_tts.Communicate(text, self._tts_voice).save(mp3)
            asyncio.run(_gen())
            if not os.path.isfile(mp3) or os.path.getsize(mp3) < 256:
                return False
            mpv_path = shutil.which("mpv")
            if not mpv_path:
                return False
            subprocess.run(
                [mpv_path, "--no-video", "--really-quiet", "--no-terminal", mp3],
                timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
            return True
        except Exception:
            return False
        finally:
            try:
                os.remove(mp3)
            except Exception:
                pass

    def _speak_desktop(self, text):
        """Offline fallback: the built-in Windows System.Speech voice."""
        try:
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "$s.Speak([Console]::In.ReadToEnd())")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           input=text, text=True, timeout=25,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=0x08000000)
        except Exception:
            pass

    def adjust_volume(self, delta):
        """Nudge the volume by *delta* (e.g. "turn it up"). Returns the message."""
        try:
            cur = self._get_properties(["volume"]).get("volume")
            cur = int(cur if cur is not None else self._settings.get("volume", 70))
        except Exception:
            cur = int(self._settings.get("volume", 70))
        vol = max(0, min(150, cur + int(delta)))
        self._cmd("set_property", "volume", vol)
        self._settings["volume"] = vol
        self._save_state()
        return {"action": "volume", "ok": True, "value": vol,
                "message": f"Volume {vol}"}

    def queue_similar(self, count=5):
        """Queue songs similar to the current track right after it (no 'like')."""
        vid = self._current_video_id()
        if not vid or not self._like_provider:
            return {"ok": False, "message": "Nothing playing"}
        with self._autoqueue_lock:
            exclude = list(self._played_ids)
        with self._stats_lock:
            exclude += self._history
        related = self._rank_candidates(self._like_provider(vid, exclude) or [])[:count]
        current = self._current_playlist_pos()
        total = self._playlist_count()
        added = 0
        for tr in related:
            path = self._download_track(tr["video_id"], tr)
            if path:
                self._cmd("loadfile", path, "append")
                self._cmd("playlist-move", total + added, current + 1 + added)
                self._remember_played(tr["video_id"])
                added += 1
        return {"ok": True, "added": added,
                "message": f"Queued {added} more like this"}

    # -- sleep timer --
    def set_sleep_timer(self, minutes):
        """Pause playback after *minutes* (0/None cancels)."""
        if self._sleep_timer:
            self._sleep_timer.cancel()
            self._sleep_timer = None
            self._sleep_deadline = None
        minutes = int(minutes or 0)
        if minutes <= 0:
            return {"sleep_minutes": 0}

        def _fire():
            try:
                self._cmd("set_property", "pause", True)
            except Exception:
                pass
            self._sleep_timer = None
            self._sleep_deadline = None

        self._sleep_timer = threading.Timer(minutes * 60, _fire)
        self._sleep_timer.daemon = True
        self._sleep_timer.start()
        self._sleep_deadline = time.monotonic() + minutes * 60
        return {"sleep_minutes": minutes}

    def sleep_remaining(self):
        if not self._sleep_deadline:
            return 0
        return max(0, int(self._sleep_deadline - time.monotonic()))

    # -- likes: persistent taste profile that drives recommendations --
    def set_like_provider(self, provider):
        self._like_provider = provider

    def _load_liked(self):
        try:
            with open(self._liked_file) as f:
                data = json.load(f)
            if isinstance(data, list):
                self._liked = data
                self._liked_ids = {t.get("video_id") for t in data if t.get("video_id")}
        except Exception:
            pass

    def _save_liked(self):
        try:
            with open(self._liked_file, "w") as f:
                json.dump(self._liked[-500:], f)
        except Exception:
            pass

    def is_liked(self, video_id):
        with self._liked_lock:
            return video_id in self._liked_ids

    def get_liked(self):
        with self._liked_lock:
            return list(reversed(self._liked))   # newest first

    def like_current(self):
        """Toggle 'like' for the current track.

        Liking records the song to the persistent taste profile and queues a
        few similar songs right after it. Un-liking removes it.
        """
        vid = self._current_video_id()
        if not vid:
            return {"ok": False, "message": "Nothing playing"}
        props = self._get_properties(["path"])
        with self._meta_lock:
            meta = dict(self._track_meta.get(props.get("path"), {}))

        with self._liked_lock:
            already = vid in self._liked_ids
            if already:
                self._liked_ids.discard(vid)
                self._liked = [t for t in self._liked if t.get("video_id") != vid]
                self._save_liked()
                return {"ok": True, "liked": False, "message": "Removed from liked"}
            self._liked_ids.add(vid)
            self._liked.append({
                "video_id": vid, "title": meta.get("title", ""),
                "artist": meta.get("artist", ""), "album": meta.get("album", ""),
                "thumbnail": meta.get("art", ""), "ts": time.time(),
            })
            self._save_liked()

        # Queue a few similar songs right after the current one.
        added = 0
        if self._like_provider:
            with self._autoqueue_lock:
                exclude = list(self._played_ids)
            related = (self._like_provider(vid, exclude) or [])[:3]
            current = self._current_playlist_pos()
            total = self._playlist_count()
            for tr in related:
                path = self._download_track(tr["video_id"], tr)
                if path:
                    self._cmd("loadfile", path, "append")
                    self._cmd("playlist-move", total + added, current + 1 + added)
                    self._remember_played(tr["video_id"])
                    added += 1
        return {"ok": True, "liked": True, "added": added,
                "message": f"Liked — queued {added} similar"}

    def _liked_seed(self):
        """A random liked video_id, for blending taste into the auto-queue."""
        with self._liked_lock:
            if not self._liked:
                return None
            import random
            return random.choice(self._liked).get("video_id")

    # -- play history + skip tracking (preference engine / smart shuffle) --
    def _load_stats(self):
        try:
            with open(self._stats_file) as f:
                d = json.load(f)
            self._history = d.get("history", [])[-self._history_size:]
            self._song_stats = d.get("songs", {})
            self._artist_stats = d.get("artists", {})
        except Exception:
            pass

    def _save_stats(self):
        try:
            with open(self._stats_file, "w") as f:
                json.dump({"history": self._history[-self._history_size:],
                           "songs": self._song_stats, "artists": self._artist_stats}, f)
        except Exception:
            pass

    def _record_play(self, video_id, meta, completed):
        """Record that a track finished (completed) or was skipped."""
        if not video_id:
            return
        artist = (meta.get("artist") or "").split(",")[0].strip().lower()
        with self._stats_lock:
            s = self._song_stats.setdefault(video_id, [0, 0])
            s[0 if completed else 1] += 1
            if artist:
                a = self._artist_stats.setdefault(artist, [0, 0])
                a[0 if completed else 1] += 1
            # The no-repeat history + queue context only track songs you
            # actually listened to (>=30%). Skipped songs are ignored here, so
            # they can come back later (the skip only counts against them in the
            # taste ranking, not as a hard "already played").
            if completed:
                if video_id in self._history:
                    self._history.remove(video_id)
                self._history.append(video_id)
                self._history = self._history[-self._history_size:]
            self._stats_dirty += 1
            dirty = self._stats_dirty
        if dirty % 3 == 0:
            self._save_stats()

    def _watch_track(self):
        """Detect track changes and log each as a play-through or a skip.

        A track counts as 'played' if it advanced past ~55% of its length
        before changing, otherwise it's a 'skip'. Runs from the monitor loop.
        """
        try:
            props = self._get_properties(["path", "time-pos", "duration", "idle-active"])
        except Exception:
            return
        path = props.get("path")
        cur_id = None
        cur_meta = {}
        if path:
            with self._meta_lock:
                cur_meta = dict(self._track_meta.get(path, {}))
            cur_id = cur_meta.get("video_id")

        if cur_id != self._watch_id:
            if self._watch_id and self._watch_dur:
                ratio = (self._watch_pos or 0) / self._watch_dur
                # A "skip" only counts against a song if you bailed before ~30%.
                completed = ratio >= 0.30
                if not completed:
                    now = time.monotonic()
                    self._skip_times = [t for t in self._skip_times if now - t < 90][-19:]
                    self._skip_times.append(now)
                self._record_play(self._watch_id, self._watch_meta, completed=completed)
            self._watch_id = cur_id
            self._watch_meta = cur_meta
            self._watch_pos = 0
            self._watch_dur = 0
        if not props.get("idle-active"):
            self._watch_pos = props.get("time-pos") or self._watch_pos
            self._watch_dur = props.get("duration") or self._watch_dur

    def preferred_artists(self):
        """Artists the user demonstrably likes: liked-song artists plus artists
        whose songs are played through far more than skipped."""
        pref = set()
        with self._liked_lock:
            for t in self._liked:
                a = (t.get("artist") or "").split(",")[0].strip().lower()
                if a:
                    pref.add(a)
        with self._stats_lock:
            for a, (plays, skips) in self._artist_stats.items():
                if plays >= 2 and plays >= skips * 2:
                    pref.add(a)
        return pref

    def _rank_candidates(self, tracks):
        """Filter recently-played tracks out and reorder by taste.

        Drops anything in the recent-play history (so the endless queue and
        smart shuffle don't repeat), then scores by liked/played-through vs
        skipped, with a little randomness to keep it fresh.
        """
        import random
        with self._stats_lock:
            recent = set(self._history)
            song_stats = dict(self._song_stats)
            artist_stats = dict(self._artist_stats)
        with self._liked_lock:
            liked_artists = {(t.get("artist") or "").split(",")[0].strip().lower()
                             for t in self._liked}

        try:
            from search import is_derivative
        except Exception:
            is_derivative = lambda t: False
        w = self._weights
        seed_artists = set(self._seed_artists or [])
        scored = []
        seen = set()
        for t in tracks:
            vid = t.get("video_id")
            if not vid or vid in recent or vid in seen:
                continue
            seen.add(vid)
            artist = (t.get("artist") or "").split(",")[0].strip().lower()
            score = random.random() * w["jitter"]
            if artist and artist in seed_artists:
                score += w["same_artist_boost"]       # same band > same genre
            if artist and artist in liked_artists:
                score += w["liked_boost"]
            if is_derivative(t.get("title")):
                continue                              # queue clean, popular versions only
            ap = artist_stats.get(artist)
            if ap:
                score += w["playthrough"] * ap[0] - w["skip_penalty"] * ap[1]
            ss = song_stats.get(vid)
            if ss:
                score += w["song_play"] * ss[0] - w["skip_penalty"] * ss[1]
            scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored]

    def _gather_candidates(self, exclude):
        """Seed the next batch from the recent listening context — several
        songs you played through, not just the current one — so the queue
        keeps the overall vibe going instead of narrowing to the last song.
        """
        import random
        cur = self._current_video_id()
        seeds = [cur] if cur else []
        with self._stats_lock:
            kept = [v for v in reversed(self._history) if v and v != cur]
        seeds += kept[:max(0, int(self._weights["context_songs"]) - 1)]
        if random.random() < self._weights["liked_seed_prob"]:
            ls = self._liked_seed()
            if ls:
                seeds.append(ls)
        uniq = []
        for s in seeds:
            if s and s not in uniq:
                uniq.append(s)
        # Bias next batch to the current bands (same artist > same genre).
        with self._meta_lock:
            id2artist = {m.get("video_id"): (m.get("artist") or "").split(",")[0].strip().lower()
                         for m in self._track_meta.values() if m.get("video_id")}
        seed_artists = []
        cur_artist = self._current_artist()
        if cur_artist:
            seed_artists.append(cur_artist)
        for v in uniq:
            a = id2artist.get(v)
            if a and a not in seed_artists:
                seed_artists.append(a)
        self._seed_artists = seed_artists
        candidates = []
        for s in uniq[:4]:
            candidates += self._autoqueue_provider(s, exclude) or []
        return self._rank_candidates(candidates)

    # -- export/download the current track to the user's Music folder --
    def export_current(self, dest_dir=None):
        props = self._get_properties(["path"])
        path = props.get("path")
        if not path or not os.path.isfile(path):
            return {"ok": False, "message": "No current track file"}
        with self._meta_lock:
            meta = self._track_meta.get(path, {})
        dest_dir = dest_dir or os.path.join(os.path.expanduser("~"), "Music", "MusicRequest")
        os.makedirs(dest_dir, exist_ok=True)
        artist = (meta.get("artist") or "").strip()
        title = (meta.get("title") or os.path.basename(path)).strip()
        safe = re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}" if artist else title)
        ext = os.path.splitext(path)[1]
        dest = os.path.join(dest_dir, safe + ext)
        try:
            shutil.copyfile(path, dest)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "path": dest, "message": f"Saved to {dest}"}

    def set_announce(self, enabled):
        self._announce_enabled = bool(enabled)
        return self._announce_enabled

    def get_settings(self):
        s = dict(self._settings)
        s["sleep_remaining"] = self.sleep_remaining()
        s["eq_presets"] = list(self.EQ_PRESETS.keys())
        s["announce"] = self._announce_enabled
        return s

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def control(self, action, value=None):
        """Send a transport control command.

        Returns a dict with a speakable ``message`` field.
        """
        if action in ("pause", "stop"):
            self._cmd("set_property", "pause", True)
            return {"action": action, "ok": True, "message": "Paused"}

        elif action == "resume":
            self._cmd("set_property", "pause", False)
            return {"action": action, "ok": True, "message": "Resumed"}

        elif action == "playpause":
            self._cmd("cycle", "pause")
            return {"action": action, "ok": True, "message": "Toggled play/pause"}

        elif action == "next":
            self._cmd("playlist-next")
            return {"action": action, "ok": True, "message": "Skipped to next track"}

        elif action == "previous":
            self._cmd("playlist-prev")
            return {"action": action, "ok": True, "message": "Previous track"}

        elif action == "volume":
            vol = max(0, min(150, int(value or 50)))
            self._cmd("set_property", "volume", vol)
            self._settings["volume"] = vol
            self._save_state()
            return {"action": action, "ok": True, "message": f"Volume set to {vol}"}

        elif action in ("shuffle", "shuffle_toggle"):
            self.shuffle_now()
            return {"action": action, "ok": True, "message": "Queue shuffled"}

        elif action == "repeat":
            mode = self.cycle_repeat()
            return {"action": action, "ok": True, "repeat": mode,
                    "message": f"Repeat {mode}"}

        elif action == "like":
            return {"action": action, **self.like_current()}

        return {"action": action, "ok": False, "message": f"Unknown action: {action}"}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self):
        """Return current player state mapped to the legacy status shape.

        Reads all properties in a single batch call (~2 seconds total) rather
        than seven separate IPC round-trips (up to 21 seconds worst case).
        """
        try:
            props = self._get_properties([
                "pause", "idle-active", "media-title", "path", "volume",
                "time-pos", "duration", "playlist-pos", "playlist-count",
            ])

            paused = props.get("pause")
            idle = props.get("idle-active")
            title = props.get("media-title")
            path = props.get("path")
            pos = props.get("time-pos")
            dur = props.get("duration")
            playlist_pos = props.get("playlist-pos")
            playlist_count = props.get("playlist-count")

            if paused:
                state = "paused"
            elif idle:
                state = "stopped"
            else:
                state = "playing"

            with self._meta_lock:
                meta = self._track_meta.get(path, {}) if path else {}

            track_info = None
            if title and not idle:
                track_info = {
                    "name": meta.get("title") or title,
                    "artist": meta.get("artist", ""),
                    "album": meta.get("album", ""),
                    "video_id": meta.get("video_id", ""),
                    "art": meta.get("art", ""),
                    "liked": self.is_liked(meta.get("video_id", "")),
                    "position": pos or 0,
                    "duration": dur or 0,
                }

            return {
                "state": state,
                "paused": bool(paused),
                "volume": props.get("volume"),
                "track": track_info,
                "playlist_position": playlist_pos,
                "playlist_count": playlist_count,
                "auto_queue": self._autoqueue_enabled,
                "repeat": self._settings.get("repeat", "off"),
                "eq": self._settings.get("eq", "flat"),
                "normalize": bool(self._settings.get("normalize")),
                "crossfade": self._settings.get("crossfade", 0),
                "sleep_remaining": self.sleep_remaining(),
            }
        except Exception as exc:
            return {"state": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_playlist_pos(self):
        try:
            result = self._get_properties(["playlist-pos"])
            return int(result.get("playlist-pos") or 0)
        except Exception:
            return 0

    def _playlist_count(self):
        try:
            result = self._get_properties(["playlist-count"])
            return int(result.get("playlist-count") or 0)
        except Exception:
            return 0