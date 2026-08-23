"""The visualiser's numbers, measured off the song itself.

Loopback hears the whole machine, so Discord moved the bars. This reads the
file mpv is playing: ffmpeg decodes to 8kHz mono, a Goertzel bank measures
seven bands at 20fps, cached to disk. Under half a second a track, once.

The page indexes the result by playback position, so it costs nothing to draw.
"""

from __future__ import annotations

import array
import math
import queue
import subprocess
import threading
from pathlib import Path

from ..logging_setup import get
from ..paths import data_dir

log = get("spectrum")

CREATE_NO_WINDOW = 0x08000000
RATE = 8000            # plenty for a seven-band meter
FPS = 20
WINDOW = 256
BANDS = [60, 160, 400, 1000, 2500, 6000, 7800]
SILENCE = 1e-9


def _dir() -> Path:
    p = data_dir() / "spectra"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_file(path: str) -> Path:
    return _dir() / (Path(path).stem + ".bin")


def _decode(path: str) -> array.array:
    """Whole track as 8 kHz mono samples."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
         "-ac", "1", "-ar", str(RATE), "-"],
        capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW).stdout
    samples = array.array("h")
    samples.frombytes(out[:len(out) // 2 * 2])
    return samples


def _measure(samples: array.array) -> list[list[float]]:
    coef = [2.0 * math.cos(2.0 * math.pi * (f * WINDOW / RATE) / WINDOW)
            for f in BANDS]
    hop = RATE // FPS
    scale = 1.0 / (32768.0 * WINDOW)
    frames: list[list[float]] = []
    for start in range(0, max(0, len(samples) - WINDOW), hop):
        chunk = samples[start:start + WINDOW]
        row = []
        for c in coef:
            s1 = s2 = 0.0
            for x in chunk:
                s0 = x + c * s1 - s2
                s2 = s1
                s1 = s0
            row.append(math.sqrt(max(0.0, s1 * s1 + s2 * s2 - c * s1 * s2)) * scale)
        frames.append(row)
    return frames


def _normalise(frames: list[list[float]]) -> bytes:
    """Per-band, against that band's loud moments — quiet songs still fill out."""
    if not frames:
        return b""
    out = bytearray()
    peaks = []
    for b in range(len(BANDS)):
        col = sorted(f[b] for f in frames)
        # near the top rather than the max, so one clap doesn't flatten the
        # rest — but high enough that the loud half isn't all pegged at 255
        peaks.append(max(col[min(len(col) - 1, int(len(col) * 0.99))], SILENCE))
    for f in frames:
        for b, v in enumerate(f):
            out.append(min(255, int(255 * min(1.0, v / peaks[b]) ** 0.6)))
    return bytes(out)


def analyse(path: str) -> bytes:
    """Envelope for a file, cached on disk."""
    cache = _cache_file(path)
    try:
        if cache.is_file() and cache.stat().st_size:
            return cache.read_bytes()
    except Exception:
        pass
    if not path or not Path(path).is_file():
        return b""
    try:
        data = _normalise(_measure(_decode(path)))
    except Exception as exc:
        log.debug("couldn't analyse %s: %s", Path(path).name, exc)
        return b""
    if data:
        try:
            cache.write_bytes(data)
        except Exception:
            pass
        log.info("analysed %s (%d frames)", Path(path).name, len(data) // len(BANDS))
    return data


def cached(path: str) -> bytes | None:
    try:
        f = _cache_file(path)
        return f.read_bytes() if f.is_file() else None
    except Exception:
        return None


_queued: set[str] = set()
_lock = threading.Lock()
_jobs: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None


def _run() -> None:
    global _worker
    done = 0
    while True:
        try:
            path = _jobs.get(timeout=30)
        except queue.Empty:
            break
        try:
            analyse(path)
            done += 1
        except Exception:
            pass
        finally:
            with _lock:
                _queued.discard(path)
        if done % 25 == 0:
            prune()          # the cache is small, but not infinite
    with _lock:
        _worker = None


def ensure(path: str) -> None:
    """Analyse in the background so it's ready before the track plays.

    One worker, not one thread per track. Measured on its own the analysis
    takes under half a second, but it's pure Python and several at once — two
    download workers finishing together while a track plays — turn that into
    eight seconds of everything fighting over the interpreter.
    """
    if not path or cached(path) is not None:
        return
    global _worker
    with _lock:
        if path in _queued:
            return
        _queued.add(path)
        _jobs.put(path)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="spectrum")
            _worker.start()


def prune(keep: int = 400) -> None:
    """Don't let the cache grow forever."""
    try:
        files = sorted(_dir().glob("*.bin"), key=lambda f: f.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink(missing_ok=True)
    except Exception:
        pass
