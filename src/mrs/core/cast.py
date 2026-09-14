"""Playing out of a phone instead of a speaker wired to this machine.

mpv stays in charge. It keeps decoding, keeps the position, keeps driving
crossfade and the queue — both engines just move to the null output, so they
let go of the sound card entirely rather than playing silence into it. The
phone pulls the same file mpv is playing and seeks to mpv's clock, so
everything upstream of the speaker carries on exactly as it did.

Four things make that work:

  Range requests. Safari asks for a few bytes to read the container header,
  then asks for ranges as it goes. Answer with a 200 and the whole file and
  it either refuses to play or gives you a timeline you can't drag.

  Format. The page says which of AAC, WebM Opus, Ogg Opus and MP4 Opus its
  browser plays (FORMATS). A browser that plays YouTube's Opus gets the
  download itself, or the same packets in a container it takes; the rest get
  an AAC encode made once and kept.

  Processing. mpv applies EQ and normalisation as live filters, which a file
  handed to a phone never sees. filter_chain() bakes the same settings into
  the file so the phone hears what the speakers would — plus, when the page
  says what it is playing out of, tuning for that speaker or headphone.

  Holding. A url keeps the file it was first answered with, so a transcode
  landing mid-song can't swap the bytes under a range request.
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

# What every phone and browser takes as-is.
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


# What the page says its browser plays. "aac" plays on every phone and
# browser there is, so it's the default and the fallback; the Opus ones are
# only asked for when the browser has said "probably" to that exact type.
FORMATS: dict[str, dict] = {
    "aac": {"ext": ".m4a", "encode": ["-c:a", "aac", "-b:a", "256k"],
            "mux": ["-movflags", "+faststart"]},
    "webm": {"ext": ".webm", "encode": ["-c:a", "libopus", "-b:a", "160k"], "mux": []},
    "ogg": {"ext": ".ogg", "encode": ["-c:a", "libopus", "-b:a", "160k"], "mux": []},
    "mp4opus": {"ext": ".mp4", "encode": ["-c:a", "libopus", "-b:a", "160k"],
                "mux": ["-movflags", "+faststart"]},
}
_OUT_EXTS = {f["ext"] for f in FORMATS.values()}
_codecs: dict[tuple[str, float], str] = {}


def fmt_name(raw: str | None) -> str:
    raw = (raw or "").strip().lower()
    return raw if raw in FORMATS else "aac"


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


def _codec(src: Path) -> str:
    """The audio codec inside a download. Asked of ffprobe once per file."""
    key = (str(src), src.stat().st_mtime)
    if key in _codecs:
        return _codecs[key]
    guess = "opus" if src.suffix.lower() in (".webm", ".opus", ".ogg") else ""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            p = subprocess.run([probe, "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                str(src)], capture_output=True, text=True, timeout=15,
                               creationflags=CREATE_NO_WINDOW)
            lines = (p.stdout or "").strip().splitlines()
            if p.returncode == 0 and lines:
                guess = lines[0].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    _codecs[key] = guess
    return guess


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


def _as_is(src: Path, chain: str, fmt: str) -> bool:
    """Whether the download itself can go to this browser, untouched.

    AAC and MP3 play everywhere. An Opus .webm is what YouTube sends, and a
    browser that plays WebM Opus gets exactly those bytes: no second lossy
    encode on top of the first, and no seconds of transcoding before the
    first note.
    """
    if chain:
        return False
    if src.suffix.lower() in NATIVE:
        return True
    return fmt == "webm" and src.suffix.lower() == ".webm" and _codec(src) == "opus"


def _stamp(tune: str = "", fmt: str = "aac") -> str:
    """Short hash of the processing and format, so changing either rebuilds."""
    chain = filter_chain(tune)
    key = chain if fmt_name(fmt) == "aac" else f"{chain}|{fmt_name(fmt)}"
    return hashlib.sha1(key.encode()).hexdigest()[:8] if key else ""


def _converted(video_id: str, tune: str = "", fmt: str = "aac") -> Path:
    stamp = _stamp(tune, fmt)
    ext = FORMATS[fmt_name(fmt)]["ext"]
    return work_dir() / (f"{video_id}~{stamp}{ext}" if stamp
                         else f"{video_id}{ext}")


def _vid_of(path: Path) -> str:
    """The video id behind a transcode, stamp and all."""
    return path.stem.split("~", 1)[0]


def _job(video_id: str, tune: str, fmt: str = "aac") -> str:
    return f"{video_id}~{_stamp(tune, fmt)}{FORMATS[fmt_name(fmt)]['ext']}"


def playable(video_id: str, tune: str = "",
             fmt: str = "aac") -> tuple[Path | None, str]:
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
    if _as_is(src, filter_chain(tune), fmt_name(fmt)):
        return src, "ready"
    out = _converted(video_id, tune, fmt)
    if out.is_file() and out.stat().st_size > 10_000:
        return out, "ready"
    with _lock:
        if _job(video_id, tune, fmt) in _converting:
            return None, "converting"
    return None, "needs conversion"


def convert(video_id: str, tune: str = "", fmt: str = "aac",
            timeout: int = 300) -> tuple[Path | None, str]:
    """Blocking. A remux is near instant; an encode ~60x realtime, tuned ~90x."""
    src = source_for(video_id)
    if not src:
        return None, "missing"
    fmt = fmt_name(fmt)
    chain = filter_chain(tune)
    if _as_is(src, chain, fmt):
        return src, "ready"
    job = _job(video_id, tune, fmt)
    with _lock:
        if job in _converting:
            return None, "converting"
        _converting.add(job)
    try:
        ff = shutil.which("ffmpeg")
        if not ff:
            return None, "no ffmpeg"
        spec = FORMATS[fmt]
        out = _converted(video_id, tune, fmt)
        tmp = out.with_suffix(".part" + spec["ext"])
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-vn"]
        # Opus already, into another container that holds Opus, untouched:
        # copy the packets. Anything else is encoded.
        remux = not chain and fmt != "aac" and _codec(src) == "opus"
        if chain:
            cmd += ["-af", chain]
        cmd += ["-c:a", "copy"] if remux else spec["encode"]
        # faststart puts the index at the front, which is what lets the phone
        # seek without pulling the whole file first.
        cmd += spec["mux"] + [str(tmp)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return None, (p.stderr or "ffmpeg failed")[:200]
        tmp.replace(out)
        log.info("%s %s for casting as %s%s", "remuxed" if remux else "transcoded",
                 video_id, fmt,
                 f" (tuned: {tune_name(tune)})" if tune_name(tune)
                 else " (processed)" if chain else "")
        return out, "ready"
    except subprocess.TimeoutExpired:
        return None, "transcode timed out"
    finally:
        with _lock:
            _converting.discard(job)


def warm(video_id: str, tune: str = "", fmt: str = "aac") -> None:
    """Get the next track ready in the background, so the gap isn't audible."""
    if not video_id:
        return
    _, state = playable(video_id, tune, fmt)
    if state != "needs conversion":
        return
    threading.Thread(target=convert, args=(video_id, tune, fmt), daemon=True,
                     name=f"cast-warm {video_id}").start()


# Which file a stream url is being answered with. Safari reads one url as
# dozens of range requests; if the tuned file lands halfway through, the
# later ranges would come from a different file and the audio is garbage.
# So once a url has been answered, it keeps its file while it's in use.
_held: dict[str, tuple[Path, float]] = {}
_HOLD = 1800.0


def serve(video_id: str, tune: str = "",
          fmt: str = "aac") -> tuple[Path | None, str]:
    """What the stream route hands out, without ever making the phone wait
    for tuning: untuned now, tuned from the next time it's asked for."""
    tune, fmt = tune_name(tune), fmt_name(fmt)
    key = _job(video_id, tune, fmt)
    now = time.monotonic()
    with _lock:
        held = _held.get(key)
        if held and now - held[1] < _HOLD and held[0].is_file():
            _held[key] = (held[0], now)
            return held[0], "ready"
    path, state = playable(video_id, tune, fmt)
    if state != "ready" and state not in ("arriving", "missing") and tune:
        plain, plain_state = playable(video_id, "", fmt)
        if plain_state == "ready" and plain:
            warm(video_id, tune, fmt)
            path, state = plain, "ready"
    if state in ("needs conversion", "converting"):
        path, state = convert(video_id, tune, fmt)
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
    live = {_stamp(t, f) for t in ("", *TUNES, *autoeq.cached_tunes())
            for f in FORMATS}
    with _lock:
        busy = {Path(p).name for p, _ in _held.values()}
    gone = 0
    for path in work_dir().iterdir():
        if not path.is_file() or path.suffix.lower() not in _OUT_EXTS:
            continue
        vid = _vid_of(path)
        stamp = path.stem.split("~", 1)[1] if "~" in path.stem else ""
        stale = stamp not in live
        if path.name in busy:
            continue
        if path.stem.endswith(".part"):
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
    ready = sum(1 for p in work_dir().iterdir() if p.suffix.lower() in _OUT_EXTS
                and not p.stem.endswith(".part"))
    with _lock:
        busy = len(_converting)
    return {"transcoded": ready, "converting": busy,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "processing": filter_chain() or "none",
            "tunes": {k: v["label"] for k, v in TUNES.items()},
            "formats": list(FORMATS)}
