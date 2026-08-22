"""Real spectrum data for the meter.

mpv won't give us audio samples, so ffmpeg analyses the file after it
downloads: 7 bands, ~25 a second, cached next to the audio. The UI plays it
back against the position.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..logging_setup import get
from ..paths import cache_dir

log = get("spectrum")

CREATE_NO_WINDOW = 0x08000000
FPS = 25                       # frames we keep, after downsampling ffmpeg's rate
BANDS = [60, 160, 400, 1000, 2500, 6000, 11000]
_RMS = re.compile(r"RMS_level=(-?\d+(?:\.\d+)?)")
_FLOOR, _CEIL = -70.0, -12.0   # dB range mapped onto 0..255

_lock = threading.Lock()
_running: set[str] = set()


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def spec_path(video_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", video_id or "x")[:100]
    return cache_dir() / f"{safe}.spec.json"


def load(video_id: str) -> dict | None:
    p = spec_path(video_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _band_filters(tmp: Path) -> str:
    """One ffmpeg graph: split the audio and measure every band at once."""
    parts = [f"[0:a]asplit={len(BANDS)}" + "".join(f"[b{i}]" for i in range(len(BANDS)))]
    for i, freq in enumerate(BANDS):
        out = (tmp / f"band{i}.txt").as_posix().replace(":", "\\:")
        parts.append(
            f"[b{i}]bandpass=f={freq}:width_type=o:w=1.5,"
            f"astats=metadata=1:reset=1,"
            f"ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file='{out}'"
            f"[o{i}]")
    return ";".join(parts)


def _read_band(path: Path) -> list[float]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [float(m) for m in _RMS.findall(text)]


def analyse(path: str, video_id: str) -> dict | None:
    """Measure one file. Returns the spectrum, or None if it can't be done."""
    if not have_ffmpeg() or not video_id:
        return None
    existing = load(video_id)
    if existing:
        return existing
    with _lock:
        if video_id in _running:
            return None
        _running.add(video_id)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            graph = _band_filters(tmp)
            args = ["ffmpeg", "-v", "error", "-i", path,
                    "-filter_complex", graph]
            for i in range(len(BANDS)):
                args += ["-map", f"[o{i}]", "-f", "null", "-"]
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=180, creationflags=CREATE_NO_WINDOW)
            bands = [_read_band(tmp / f"band{i}.txt") for i in range(len(BANDS))]
        if not any(bands):
            log.debug("no spectrum data (%s)", (proc.stderr or "")[:120])
            return None

        length = min(len(b) for b in bands if b)
        if length < 4:
            return None
        # ffmpeg reports ~50/sec; thin it down to FPS.
        native_fps = 50.0
        step = max(1, int(round(native_fps / FPS)))

        # Normalise per band against this track. A loud master would otherwise
        # peg every bar at the top and a quiet one would barely move.
        peaks = []
        for b in bands:
            vals = sorted(v for v in b if v > -90)
            peaks.append(vals[int(len(vals) * 0.98)] if vals else _CEIL)
        floors = []
        for b in bands:
            vals = sorted(v for v in b if v > -90)
            floors.append(vals[int(len(vals) * 0.10)] if vals else _FLOOR)

        frames = []
        for i in range(0, length, step):
            row = []
            for bi, b in enumerate(bands):
                db = b[i] if i < len(b) else _FLOOR
                lo = min(floors[bi], peaks[bi] - 6)      # keep a sane span
                span = max(6.0, peaks[bi] - lo)
                unit = (db - lo) / span
                row.append(max(0, min(255, int(unit * 255))))
            frames.append(row)

        data = {"fps": native_fps / step, "bands": len(BANDS),
                "frames": frames, "v": 1}
        try:
            spec_path(video_id).write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        log.info("spectrum ready for %s (%d frames)", video_id, len(frames))
        return data
    except Exception as exc:
        log.debug("spectrum failed: %s", exc)
        return None
    finally:
        with _lock:
            _running.discard(video_id)


def analyse_async(path: str, video_id: str) -> None:
    if not have_ffmpeg() or load(video_id):
        return
    threading.Thread(target=analyse, args=(path, video_id), daemon=True,
                     name="spectrum").start()
