"""Playing out of a phone instead of a speaker wired to this machine.

mpv stays in charge. It keeps decoding, keeps the position, keeps driving
crossfade and the queue — both engines just move to the null output, so they
let go of the sound card entirely rather than playing silence into it. The
phone pulls the same file mpv is playing and seeks to mpv's clock, so
everything upstream of the speaker carries on exactly as it did.

Three things make that work:

  Range requests. Safari asks for a few bytes to read the container header,
  then asks for ranges as it goes. Answer with a 200 and the whole file and
  it either refuses to play or gives you a timeline you can't drag.

  Transcoding. 450 of the cached files are .webm holding Opus, which iOS
  won't play in any container. Those get an .m4a made once and kept beside
  them.

  Processing. mpv applies EQ and normalisation as live filters, which a file
  handed to a phone never sees. filter_chain() bakes the same settings into
  the transcode so the phone hears what the speakers would.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

from ..config import config
from ..logging_setup import get
from ..paths import cache_dir, pinned_dir

log = get("cast")

# What Safari on iOS takes as-is.
NATIVE = {".m4a", ".mp3", ".aac", ".mp4", ".m4b", ".wav"}
CONVERT = {".webm", ".opus", ".ogg", ".flac", ".mkv"}
CREATE_NO_WINDOW = 0x08000000

_converting: set[str] = set()
_lock = threading.Lock()


def work_dir() -> Path:
    # A subdirectory of the cache, so the downloader's prune skips it
    # (it only looks at files) but it still gets cleared with the cache.
    p = cache_dir() / "cast"
    p.mkdir(parents=True, exist_ok=True)
    return p


def source_for(video_id: str) -> Path | None:
    """The file the downloader already fetched, whatever extension it got."""
    if not video_id or "/" in video_id or "\\" in video_id or ".." in video_id:
        return None
    for folder in (pinned_dir(), cache_dir()):
        for path in folder.glob(f"{video_id}.*"):
            if path.is_file() and path.stat().st_size > 100_000:
                return path
    return None


def filter_chain() -> str:
    """The processing the PC gets, as an ffmpeg filter string.

    mpv applies EQ and normalisation live, as filters on playback. The phone
    is handed a *file*, so none of that reaches it unless it's baked in when
    the file is made — otherwise casting quietly means listening to the
    un-normalised, un-EQ'd original while the PC is doing it properly.
    """
    from .audio import EQ_PRESETS
    parts = [f"equalizer=f={freq}:width_type=o:width=1.5:g={gain}"
             for freq, gain in EQ_PRESETS.get(config.get("eq", "flat"), [])]
    if config.get("normalize"):
        # the same settings audio.build_chain uses, so both ears agree
        parts.append("dynaudnorm=f=150:g=15:p=0.9:m=15:r=0.9")
    return ",".join(parts)


def _stamp() -> str:
    """Short hash of the current processing, so changing the EQ rebuilds."""
    chain = filter_chain()
    return hashlib.sha1(chain.encode()).hexdigest()[:8] if chain else ""


def _converted(video_id: str) -> Path:
    stamp = _stamp()
    return work_dir() / (f"{video_id}~{stamp}.m4a" if stamp
                         else f"{video_id}.m4a")


def _vid_of(path: Path) -> str:
    """The video id behind a transcode, stamp and all."""
    return path.stem.split("~", 1)[0]


def playable(video_id: str) -> tuple[Path | None, str]:
    """(path, state) where state is ready | needs conversion | converting | missing."""
    src = source_for(video_id)
    if not src:
        return None, "missing"
    # With EQ or normalisation on, even an already-playable file has to be
    # rebuilt — there's no filter chain between the file and the phone.
    if src.suffix.lower() in NATIVE and not filter_chain():
        return src, "ready"
    out = _converted(video_id)
    if out.is_file() and out.stat().st_size > 10_000:
        return out, "ready"
    with _lock:
        if video_id in _converting:
            return None, "converting"
    return None, "needs conversion"


def convert(video_id: str, timeout: int = 300) -> tuple[Path | None, str]:
    """Blocking transcode. Runs about 60x realtime, so a few seconds a track."""
    src = source_for(video_id)
    if not src:
        return None, "missing"
    chain = filter_chain()
    if src.suffix.lower() in NATIVE and not chain:
        return src, "ready"
    with _lock:
        if video_id in _converting:
            return None, "converting"
        _converting.add(video_id)
    try:
        ff = shutil.which("ffmpeg")
        if not ff:
            return None, "no ffmpeg"
        out = _converted(video_id)
        tmp = out.with_suffix(".part.m4a")
        # faststart puts the index at the front, which is what lets the phone
        # seek without pulling the whole file first.
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-vn"]
        if chain:
            cmd += ["-af", chain]
        cmd += ["-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(tmp)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return None, (p.stderr or "ffmpeg failed")[:200]
        tmp.replace(out)
        log.info("transcoded %s for casting%s", video_id,
                 " (processed)" if chain else "")
        return out, "ready"
    except subprocess.TimeoutExpired:
        return None, "transcode timed out"
    finally:
        with _lock:
            _converting.discard(video_id)


def warm(video_id: str) -> None:
    """Get the next track ready in the background, so the gap isn't audible."""
    if not video_id:
        return
    _, state = playable(video_id)
    if state != "needs conversion":
        return
    threading.Thread(target=convert, args=(video_id,), daemon=True,
                     name=f"cast-warm {video_id}").start()


def prune() -> int:
    """Drop transcodes nothing can use any more.

    Two ways that happens: the source got cleaned out from under it, or the
    EQ changed and the processing baked into it is no longer what the PC is
    playing.
    """
    live = _stamp()
    gone = 0
    for path in work_dir().glob("*.m4a"):
        vid = _vid_of(path)
        stale = path.name != (f"{vid}~{live}.m4a" if live else f"{vid}.m4a")
        if stale or not source_for(vid):
            try:
                path.unlink()
                gone += 1
            except OSError:
                pass
    return gone


def stats() -> dict:
    ready = len(list(work_dir().glob("*.m4a")))
    with _lock:
        busy = len(_converting)
    return {"transcoded": ready, "converting": busy,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "processing": filter_chain() or "none"}
