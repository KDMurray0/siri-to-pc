"""A song, shared.

A link anybody can open. In a browser, every person who opens it gets a
listening session of their own -- the song, then the radio from it -- that
ends when they leave: their page checks in, and when it stops, the session
goes. Pasted into a chat app it's a card with the song in it; Discord plays
the video (the cover, with the song) right there in the chat.

A share opens the full player with a private, device-only queue. Its
credential and profile live only in memory and end with the client heartbeat.
"""

from __future__ import annotations

import json
import hashlib
import http.client
import ipaddress
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

from ..logging_setup import get
from ..paths import cache_dir, data_dir, write_atomic, write_atomic_bytes

log = get("shares")

KEEP_DAYS = 30
MAX_SHARES = 2000
VISIT_GONE = 90.0          # seconds without a check-in before a visit ends
MAX_VISITS = 16            # listening at once through shared links
JOINS_PER_IP = 8           # per ten minutes
EMBED_SIDE = 720
CARD_WIDTH, CARD_HEIGHT = 1200, 280
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
                # An early share can precede artwork/metadata resolution.
                # Reusing its id must not preserve that empty cover forever.
                for field in ("title", "artist", "art"):
                    if track.get(field):
                        row[field] = str(track[field])[:500 if field == "art" else 200]
                if duration > 0:
                    row["duration"] = duration
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

_visits: dict[str, dict] = {}          # token -> {sid, pid, full_player, beat}
_joins: dict[str, list[float]] = {}
_vlock = threading.Lock()
_join_lock = threading.Lock()


def _live_visits() -> int:
    from .session import sessions
    with _vlock:
        for token in [t for t, v in _visits.items() if not sessions.find(v["pid"])]:
            _visits.pop(token, None)
        return len(_visits)


def join(sid: str, ip: str, *, full_player: bool = False):
    # Admission and registration are one operation: simultaneous clicks may
    # not each see the last free slot and all start download workers.
    with _join_lock:
        return _join(sid, ip, full_player=full_player)


def _join(sid: str, ip: str, *, full_player: bool = False):
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
    with _vlock:
        _visits[token] = {"sid": sid, "pid": pid, "full_player": full_player,
                          "beat": time.monotonic()}
    try:
        room = sessions.for_pass(pid, "Shared song", "phone",
                                 Profile(pid, "Shared song", permanent=False, tracking=False))
        room.queue.play_now([Track(video_id=row["video_id"], title=row.get("title", ""),
                                   artist=row.get("artist", ""), art=row.get("art", ""),
                                   duration=row.get("duration", 0), origin="share")])
    except Exception:
        leave(sid, token)
        raise
    return token, room


def player_row(credential: str, *, heartbeat: bool = False) -> dict | None:
    """An in-memory player credential. Expired visits can never reopen a room."""
    from .session import sessions
    if not credential.startswith("share."):
        return None
    token = credential[6:]
    with _vlock:
        visit = _visits.get(token)
        if not visit or not visit.get("full_player"):
            return None
        stale = time.monotonic() - visit["beat"] > VISIT_GONE
        if heartbeat and not stale:
            visit["beat"] = time.monotonic()
        info = dict(visit)
    room = sessions.find(info["pid"])
    if stale or room is None:
        leave(info["sid"], token)
        return None
    return {"id": room.id, "name": "Private listen", "scope": "phone",
            "expires": 1, "ephemeral": True, "tracking": False,
            "share_id": info["sid"]}


def player_alive(pid: str) -> bool:
    with _vlock:
        return any(v["pid"] == pid and
                   (not v.get("full_player") or time.monotonic() - v["beat"] <= VISIT_GONE)
                   for v in _visits.values())


def forget_visit(pid: str) -> None:
    """Called by the session reaper, including when the owner closes a room."""
    with _vlock:
        for token in [t for t, v in _visits.items() if v["pid"] == pid]:
            _visits.pop(token, None)


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
    # Do not re-publish covers or videos cached by the older unrestricted
    # artwork fetcher. New media is built only through the safe image path.
    d = cache_dir() / "share-embed-v4"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha256(video_id.encode()).hexdigest()[:24] + ".mp4")


def media_key(row: dict) -> str:
    return json.dumps([row.get(k, "") for k in
                       ("video_id", "title", "artist", "art", "duration")])


def card_file(row: dict):
    return embed_file(media_key(row)).with_suffix(".jpg")


def card(row: dict):
    from .share_card import render
    out = card_file(row)
    if not out.exists() or time.time() - out.stat().st_mtime > 300:
        render(row, _cover(row), out, CARD_WIDTH, CARD_HEIGHT)
    return out


def cover_file(video_id: str):
    """The first-party cover used by chat crawlers and the generated preview."""
    return embed_file(video_id).with_suffix(".jpg")


_ART_HOST = re.compile(
    r"(?:i\d*\.ytimg\.com|yt\d+\.ggpht\.com|lh\d+\.googleusercontent\.com|"
    r"i\.scdn\.co|[a-z0-9-]+\.mzstatic\.com|(?:e-)?cdns-images\.dzcdn\.net)\Z")
_COVER_BYTES = 4_000_000


def _art_url(art: str) -> str:
    """Only catalogue image CDNs, never an arbitrary URL supplied by a guest."""
    if not isinstance(art, str) or any(ord(c) < 32 for c in art) or "\\" in art:
        return ""
    try:
        url = urlsplit(art)
        if (url.scheme != "https" or url.username or url.password or url.fragment
                or url.port not in (None, 443) or not _ART_HOST.fullmatch(url.hostname or "")):
            return ""
    except ValueError:
        return ""
    return art


class _ArtworkConnection(http.client.HTTPSConnection):
    """Connect to the checked address, with TLS still verifying the CDN name.

    Looking up an address to validate it and then letting an HTTP library
    resolve it again creates a DNS-rebinding gap. Pin this connection to one
    of the addresses just checked, and do not use environment proxy settings.
    """

    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global
                                for row in addresses):
            raise ValueError("Artwork must resolve to a public address")
        failure = None
        deadline = time.monotonic() + self.timeout
        for family, kind, proto, _, target in addresses[:4]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw = socket.socket(family, kind, proto)
            try:
                raw.settimeout(remaining)
                raw.connect(target)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as exc:
                failure = exc
                raw.close()
        raise OSError("Could not reach the artwork CDN") from failure


def _read_cover_url(art: str) -> bytes:
    if not _art_url(art):
        raise ValueError("Unsupported artwork host")
    url = urlsplit(art)
    conn = _ArtworkConnection(url.hostname, timeout=5)
    try:
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        conn.request("GET", path, headers={"User-Agent": "MusicRequestServer/4 share"})
        response = conn.getresponse()
        # CDNs return the image directly. Never follow a redirect into a LAN
        # service, another scheme, or an unreviewed hostname.
        if response.status != 200:
            raise ValueError("Artwork CDN did not return an image")
        body = response.read(_COVER_BYTES + 1)
        if len(body) > _COVER_BYTES:
            raise ValueError("Artwork is too large")
        return body
    finally:
        conn.close()


def _cover(row: dict):
    """The cover, big enough for a chat card. None if there isn't one."""
    vid = str(row.get("video_id") or "")
    out = cover_file(vid)
    # A cover we already have is served as-is: it was fetched through the
    # safe path, and its identity is the song's, not the ID's shape.
    if out.exists() and out.stat().st_size > 2000:
        return out
    art = _art_url(str(row.get("art") or ""))
    if not art:
        # Only a real video ID may seed a fallback URL; anything else has
        # no artwork rather than a guess.
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return None
        art = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    url = re.sub(r"=w\d+-h\d+", f"=w{EMBED_SIDE}-h{EMBED_SIDE}", art)
    try:
        data = _read_cover_url(url)
        # Normalize format and reject HTML/error bodies before caching them.
        import io
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > 20_000_000:
                raise ValueError("Artwork dimensions are too large")
            im = ImageOps.fit(im.convert("RGB"), (EMBED_SIDE, EMBED_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
        write_atomic_bytes(out, buf.getvalue())
        return out
    except Exception as exc:
        log.info("no cover for a shared song: %s", exc)
        return None


def make_embed(row: dict):
    """The cover and the song as one small video. Blocking; returns the path or None."""
    vid = row["video_id"]
    out = embed_file(media_key(row))
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
            cover = card(row)
            picture = ["-loop", "1", "-framerate", "1", "-i", str(cover)]
            tmp = out.with_name(out.stem + ".part.mp4")
            cmd = [ff, "-y", "-loglevel", "error", *picture, "-i", str(src),
                   "-map", "0:v:0", "-map", "1:a:0",
                   "-vf", "format=yuv420p",
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
    rows = [r for r in _read().values() if r.get("expires", 0) > time.time()]
    keep = {embed_file(media_key(r)).stem for r in rows}
    keep.update(cover_file(r["video_id"]).stem for r in rows)
    gone = 0
    d = cache_dir() / "share-embed-v4"
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
