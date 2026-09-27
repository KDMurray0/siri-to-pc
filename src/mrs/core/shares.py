"""A song, shared.

A link anybody can open. In a browser, every person who opens it gets a
listening session of their own -- the song, then the radio from it -- that
ends when they leave: their page checks in, and when it stops, the session
goes. Pasted into a chat app it's a card with the song in it; Discord plays
the video (the cover, with the song) right there in the chat.

A share is never a way into anything else: no account, no search, no
queue but its own, and it expires.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request

from ..logging_setup import get
from ..paths import cache_dir, data_dir, write_atomic

log = get("shares")

KEEP_DAYS = 30
MAX_SHARES = 2000
VISIT_GONE = 90.0          # seconds without a check-in before a visit ends
MAX_VISITS = 16            # listening at once through shared links
JOINS_PER_IP = 8           # per ten minutes
EMBED_SIDE = 720
_lock = threading.Lock()
_CREATE_NO_WINDOW = 0x08000000


class Busy(Exception):
    """Too many people, or one person too often."""


def _path():
    return data_dir() / "shares.json"


def _read() -> dict:
    try:
        got = json.loads(_path().read_text("utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(rows: dict) -> None:
    write_atomic(_path(), json.dumps(rows))


def create(track: dict, by: str = "") -> dict:
    """Share one song. The same song shared again by the same person is the same link."""
    vid = str(track.get("video_id") or "").strip()
    if not vid or len(vid) > 40:
        raise ValueError("no song to share")
    try:
        duration = int(round(float(track.get("duration") or 0)))
    except (TypeError, ValueError):
        duration = 0
    now = int(time.time())
    with _lock:
        rows = {k: v for k, v in _read().items() if v.get("expires", 0) > now}
        for sid, row in rows.items():
            if row["video_id"] == vid and row.get("by") == by:
                row["expires"] = now + KEEP_DAYS * 86400
                _write(rows)
                return dict(row, id=sid)
        if len(rows) >= MAX_SHARES:
            for sid in sorted(rows, key=lambda k: rows[k].get("created", 0))[:len(rows) - MAX_SHARES + 1]:
                rows.pop(sid, None)
        sid = secrets.token_urlsafe(8)
        rows[sid] = {"video_id": vid, "title": str(track.get("title") or "")[:200],
                     "artist": str(track.get("artist") or "")[:200],
                     "art": str(track.get("art") or "")[:500],
                     "duration": max(0, duration), "by": by[:40],
                     "created": now, "expires": now + KEEP_DAYS * 86400}
        _write(rows)
        return dict(rows[sid], id=sid)


def get(sid: str) -> dict | None:
    row = _read().get(sid or "")
    if not row or row.get("expires", 0) < time.time():
        return None
    return dict(row, id=sid)


def forget_by(who: str) -> int:
    """Every share somebody made. For when their account goes."""
    with _lock:
        rows = _read()
        mine = [k for k, v in rows.items() if v.get("by") == who]
        for k in mine:
            rows.pop(k, None)
        if mine:
            _write(rows)
        return len(mine)


# ── one session per person who opens it ──────────────────────────────

_visits: dict[str, dict] = {}          # token -> {sid, pid, ip}
_joins: dict[str, list[float]] = {}
_vlock = threading.Lock()


def _live_visits() -> int:
    from .session import sessions
    with _vlock:
        for token in [t for t, v in _visits.items() if not sessions.find(v["pid"])]:
            _visits.pop(token, None)
        return len(_visits)


def join(sid: str, ip: str):
    """A fresh session for whoever just opened the link: the song, then its radio."""
    from ..models import Track
    from .profile import Profile
    from .session import sessions
    row = get(sid)
    if not row:
        raise LookupError(sid)
    now = time.time()
    with _vlock:
        seen = [t for t in _joins.get(ip, []) if now - t < 600]
        if len(seen) >= JOINS_PER_IP:
            raise Busy("That link has been opened a lot from here -- try again in a few minutes")
        _joins[ip] = seen + [now]
        if len(_joins) > 1024:
            for k in [k for k, v in _joins.items() if now - v[-1] > 600]:
                _joins.pop(k, None)
    if _live_visits() >= MAX_VISITS:
        raise Busy("Too many people are listening through shared links right now")
    token = secrets.token_urlsafe(18)
    pid = "v-" + secrets.token_hex(6)
    room = sessions.for_pass(pid, "Shared song", "phone",
                             Profile(pid, "Shared song", permanent=False))
    room.queue.play_now([Track(video_id=row["video_id"], title=row.get("title", ""),
                               artist=row.get("artist", ""), art=row.get("art", ""),
                               duration=row.get("duration", 0), origin="share")])
    with _vlock:
        _visits[token] = {"sid": sid, "pid": pid, "ip": ip}
    log.info("someone opened a shared song (%d listening)", len(_visits))
    return token, room


def visit(sid: str, token: str):
    """Their session, if it's still going. Asking is a check-in."""
    from .session import sessions
    with _vlock:
        v = _visits.get(token or "")
    if not v or v["sid"] != sid:
        return None
    room = sessions.find(v["pid"])
    if room is None:
        with _vlock:
            _visits.pop(token, None)
        return None
    room.touch()
    return room


def leave(sid: str, token: str) -> bool:
    from .session import sessions
    with _vlock:
        v = _visits.get(token or "")
        if not v or v["sid"] != sid:
            return False
        _visits.pop(token, None)
    return sessions.close(v["pid"], "left")


def is_visit(pid: str) -> bool:
    return str(pid).startswith("v-")


# ── the video a chat app plays ───────────────────────────────────────

_making: set[str] = set()
_mlock = threading.Lock()
_one_at_a_time = threading.Semaphore(1)


def embed_file(video_id: str):
    d = cache_dir() / "share-embed"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{video_id}.mp4"


def _cover(row: dict):
    """The cover, big enough for a chat card. None if there isn't one."""
    art = str(row.get("art") or "")
    if not art.startswith("http"):
        return None
    import re
    url = re.sub(r"=w\d+-h\d+", f"=w{EMBED_SIDE}-h{EMBED_SIDE}", art)
    out = embed_file(row["video_id"]).with_suffix(".jpg")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicRequestServer/4 share"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out.write_bytes(r.read(4_000_000))
        return out
    except Exception as exc:
        log.info("no cover for a shared song: %s", exc)
        return None


def make_embed(row: dict):
    """The cover and the song as one small video. Blocking; returns the path or None."""
    vid = row["video_id"]
    out = embed_file(vid)
    if out.exists() and out.stat().st_size > 2000:
        return out
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    with _mlock:
        if vid in _making:
            return None
        _making.add(vid)
    try:
        with _one_at_a_time:
            from ..models import Track
            from .downloader import downloader
            src = downloader.cached(vid) or downloader.fetch(
                Track(video_id=vid, title=row.get("title", ""), artist=row.get("artist", ""),
                      origin="share"))
            if not src:
                return None
            cover = _cover(row)
            picture = (["-loop", "1", "-framerate", "1", "-i", str(cover)] if cover else
                       ["-f", "lavfi", "-i", f"color=c=0x141418:s={EMBED_SIDE}x{EMBED_SIDE}:r=1"])
            tmp = out.with_name(out.stem + ".part.mp4")
            side = EMBED_SIDE
            cmd = [ff, "-y", "-loglevel", "error", *picture, "-i", str(src),
                   "-map", "0:v:0", "-map", "1:a:0",
                   "-vf", f"scale={side}:{side}:force_original_aspect_ratio=increase,"
                          f"crop={side}:{side},format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-r", "1",
                   "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(tmp)]
            done = subprocess.run(cmd, capture_output=True, timeout=300,
                                  creationflags=_CREATE_NO_WINDOW)
            if done.returncode or not tmp.exists():
                log.warning("couldn't make a shared song's video: %s",
                            done.stderr.decode("utf-8", "replace")[-300:])
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
            return out
    except Exception as exc:
        log.warning("couldn't make a shared song's video: %s", exc)
        return None
    finally:
        with _mlock:
            _making.discard(vid)


def make_embed_soon(row: dict) -> None:
    threading.Thread(target=make_embed, args=(row,), daemon=True, name="share-embed").start()


def prune_embeds() -> int:
    """Videos for songs nobody is sharing any more."""
    keep = {r["video_id"] for r in _read().values() if r.get("expires", 0) > time.time()}
    gone = 0
    d = cache_dir() / "share-embed"
    if not d.is_dir():
        return 0
    for f in d.iterdir():
        if f.stem.split(".")[0] not in keep:
            try:
                f.unlink()
                gone += 1
            except OSError:
                pass
    return gone
