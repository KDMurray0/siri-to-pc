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
  the transcode so the phone hears what the speakers would — plus, when the
  page says what it is playing out of, tuning for that speaker.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
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


def _speaker(cut: int, harmonics: float, body: float, bite: float,
             level: float) -> str:
    """ffmpeg chain for a small built-in speaker.

    Below `cut` the driver can't move air, but the bass is still in the
    signal eating excursion, so the phone's protection limiter pumps on
    every kick and the whole track comes out quieter. So: cut it, put back
    its harmonics (the speaker can play those, and the ear fills in the
    fundamental), a little body at 280Hz, take the edge off 2.8kHz where
    small speakers shout, then spend the freed headroom on level.
    """
    source = round(cut * 1.45)      # the bass being dropped
    above = round(cut * 1.73)       # keep only the harmonics of it
    return (
        "highpass=f=45:poles=2,asplit=2[m][b];"
        f"[b]lowpass=f={source}:poles=2,lowpass=f={source}:poles=2,"
        "volume=12dB,asoftclip=type=tanh:threshold=0.5,"
        f"highpass=f={above}:poles=2,highpass=f={above}:poles=2,"
        "lowpass=f=900,volume=-6dB[h];"
        f"[m]highpass=f={cut}:poles=2,highpass=f={cut}:poles=2[mh];"
        f"[mh][h]amix=inputs=2:weights=1 {harmonics}:normalize=0,"
        f"equalizer=f=280:width_type=q:width=0.9:g={body},"
        f"equalizer=f=2800:width_type=q:width=1.2:g={bite},"
        "acompressor=threshold=-22dB:ratio=3:attack=10:release=220:"
        "makeup=4dB:knee=6,"
        f"volume={level}dB,"
        "alimiter=limit=0.75:attack=8:release=120:level=false"
    )


# Per kind of device. Headphones and anything unknown get nothing: this cuts
# real bass, which on AirPods is just worse. The page picks one from what it
# is running on and the listener can switch it off.
TUNES: dict[str, dict] = {
    "iphone": {"label": "iPhone speaker", "chain": _speaker(110, 0.6, 2, -2.5, 6)},
    "phone": {"label": "Phone speaker", "chain": _speaker(130, 0.7, 2, -3, 6)},
    "ipad": {"label": "iPad speakers", "chain": _speaker(75, 0.4, 1, -1.5, 5)},
}


def tune_name(raw: str | None) -> str:
    """A speaker preset, or aeq-<id> for a headphone profile already on disk.

    Only on disk: the stream must never wait on GitHub, so the page fetches
    the profile first (/api/autoeq/profile) and asks for it after.
    """
    raw = (raw or "").strip().lower()
    if raw in TUNES:
        return raw
    if raw.startswith("aeq-") and re.fullmatch(r"aeq-[0-9a-f]{12}", raw):
        from . import autoeq
        return raw if autoeq.cached_chain(raw[4:]) else ""
    return ""


def _tune_chain(tune: str) -> str:
    if tune.startswith("aeq-"):
        from . import autoeq
        return autoeq.cached_chain(tune[4:])
    return TUNES[tune]["chain"]


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
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", video_id or "unknown")[:100]
    for folder in (pinned_dir(), cache_dir()):
        for path in folder.glob(f"{sid}.*"):
            if (path.is_file() and path.stat().st_size > 0
                    and not path.name.endswith((".part", ".ytdl", ".complete"))):
                return path
    return None


def filter_chain(tune: str = "") -> str:
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
    tune = tune_name(tune)
    if tune:
        # Last: the speaker is the last thing the sound goes through.
        parts.append(_tune_chain(tune))
    return ",".join(p for p in parts if p)


def _stamp(tune: str = "") -> str:
    """Short hash of the processing, so changing the EQ rebuilds."""
    chain = filter_chain(tune)
    return hashlib.sha1(chain.encode()).hexdigest()[:8] if chain else ""


def _converted(video_id: str, tune: str = "") -> Path:
    stamp = _stamp(tune)
    return work_dir() / (f"{video_id}~{stamp}.m4a" if stamp
                         else f"{video_id}.m4a")


def _vid_of(path: Path) -> str:
    """The video id behind a transcode, stamp and all."""
    return path.stem.split("~", 1)[0]


def playable(video_id: str, tune: str = "") -> tuple[Path | None, str]:
    """(path, state): ready | arriving | needs conversion | converting | missing.

    Downloads are private until yt-dlp publishes the completed final name.
    There is intentionally no growing-file state here: a byte prefix is not
    a valid progressive media container merely because a guessed total exists.
    """
    from .downloader import arriving
    coming = arriving(video_id)
    src = source_for(video_id)
    if not src:
        # Known to be on its way, just not far enough along to have a path.
        return None, "arriving" if coming else "missing"
    if coming:
        return None, "arriving"
    # With EQ or normalisation on, even an already-playable file has to be
    # rebuilt — there's no filter chain between the file and the phone.
    if src.suffix.lower() in NATIVE and not filter_chain(tune):
        return src, "ready"
    out = _converted(video_id, tune)
    if out.is_file() and out.stat().st_size > 10_000:
        return out, "ready"
    with _lock:
        if _job(video_id, tune) in _converting:
            return None, "converting"
    return None, "needs conversion"


def _job(video_id: str, tune: str) -> str:
    return f"{video_id}~{_stamp(tune)}"


def convert(video_id: str, tune: str = "",
            timeout: int = 300) -> tuple[Path | None, str]:
    """Blocking transcode. ~350x realtime plain, ~90x tuned."""
    src = source_for(video_id)
    if not src:
        return None, "missing"
    chain = filter_chain(tune)
    if src.suffix.lower() in NATIVE and not chain:
        return src, "ready"
    job = _job(video_id, tune)
    with _lock:
        if job in _converting:
            return None, "converting"
        _converting.add(job)
    try:
        ff = shutil.which("ffmpeg")
        if not ff:
            return None, "no ffmpeg"
        out = _converted(video_id, tune)
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
                 f" (tuned: {tune_name(tune)})" if tune_name(tune)
                 else " (processed)" if chain else "")
        return out, "ready"
    except subprocess.TimeoutExpired:
        return None, "transcode timed out"
    finally:
        with _lock:
            _converting.discard(job)


def warm(video_id: str, tune: str = "") -> None:
    """Get the next track ready in the background, so the gap isn't audible."""
    if not video_id:
        return
    _, state = playable(video_id, tune)
    if state != "needs conversion":
        return
    threading.Thread(target=convert, args=(video_id, tune), daemon=True,
                     name=f"cast-warm {video_id}").start()


# Which file a stream url is being answered with. Safari reads one url as
# dozens of range requests; if the tuned file lands halfway through, the
# later ranges would come from a different file and the audio is garbage.
# So once a url has been answered, it keeps its file while it's in use.
_held: dict[str, tuple[Path, float]] = {}
_HOLD = 1800.0


def serve(video_id: str, tune: str = "") -> tuple[Path | None, str]:
    """What the stream route hands out, without ever making the phone wait
    for tuning: untuned now, tuned from the next time it's asked for."""
    tune = tune_name(tune)
    key = _job(video_id, tune)
    now = time.monotonic()
    with _lock:
        held = _held.get(key)
        if held and now - held[1] < _HOLD and held[0].is_file():
            _held[key] = (held[0], now)
            return held[0], "ready"
    path, state = playable(video_id, tune)
    if state != "ready" and state not in ("arriving", "missing") and tune:
        plain, plain_state = playable(video_id)
        if plain_state == "ready" and plain:
            warm(video_id, tune)
            path, state = plain, "ready"
    if state in ("needs conversion", "converting"):
        path, state = convert(video_id, tune)
    if state == "ready" and path:
        with _lock:
            for k in [k for k, (_, at) in _held.items() if now - at >= _HOLD]:
                del _held[k]
            _held[key] = (Path(path), now)
    return path, state


def prune() -> int:
    """Drop transcodes nothing can use any more.

    Two ways that happens: the source got cleaned out from under it, or the
    EQ changed and the processing baked into it is no longer what the PC is
    playing.
    """
    from . import autoeq
    live = {_stamp(t) for t in ("", *TUNES, *autoeq.cached_tunes())}
    with _lock:
        busy = {Path(p).name for p, _ in _held.values()}
    gone = 0
    for path in work_dir().glob("*.m4a"):
        vid = _vid_of(path)
        stamp = path.stem.split("~", 1)[1] if "~" in path.stem else ""
        stale = stamp not in live
        if path.name in busy:
            continue
        if path.name.endswith(".part.m4a"):
            # Mid-transcode, unless it's been sitting there an hour.
            stale = time.time() - path.stat().st_mtime > 3600
            if not stale:
                continue
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
            "processing": filter_chain() or "none",
            "tunes": {k: v["label"] for k, v in TUNES.items()}}
