"""Public playlist snapshots. Importing copies tracks, never permissions or paths."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from ..paths import data_dir, write_atomic

_lock = threading.RLock()
KEEP_DAYS = 30
MAX_SHARES = 250
MAX_SHARES_PER_OWNER = 20
MAX_TRACKS = 1000


def _path():
    return data_dir() / "playlist-shares.json"


def _read():
    try:
        rows = json.loads(_path().read_text("utf-8"))
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def create(name, tracks, by):
    if not tracks:
        raise ValueError("Add some songs before sharing this playlist")
    if len(tracks) > MAX_TRACKS:
        raise ValueError("A shared playlist can contain up to 1,000 songs")
    clean = []
    for track in tracks:
        if (track.source in ("local", "radio") or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", track.video_id or "")):
            raise ValueError("This playlist contains local-only songs or live radio; those cannot be copied by link")
        art = track.art if urlsplit(track.art or "").scheme == "https" else ""
        clean.append({"video_id": track.video_id, "title": track.title[:300],
                      "artist": track.artist[:300], "album": track.album[:300],
                      "art": art[:1000], "duration": max(0, min(86400, int(track.duration or 0)))})
    payload = {"name": name[:120], "tracks": clean}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    now = int(time.time())
    with _lock:
        rows = {k: v for k, v in _read().items() if v.get("expires", 0) > now}
        sid = next((k for k, v in rows.items() if v.get("digest") == digest and v.get("by") == by), None)
        if sid is None:
            own = [k for k, v in rows.items() if v.get("by") == by]
            if len(own) >= MAX_SHARES_PER_OWNER:
                rows.pop(min(own, key=lambda k: rows[k].get("created", 0)))
            if len(rows) >= MAX_SHARES:
                rows.pop(min(rows, key=lambda k: rows[k].get("created", 0)))
            sid = secrets.token_urlsafe(12)
        rows[sid] = {**payload, "digest": digest, "by": by, "created": now,
                     "expires": now + KEEP_DAYS * 86400}
        write_atomic(_path(), json.dumps(rows))
    return {**payload, "id": sid}


def get(sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16}", sid or ""):
        return None
    with _lock:
        row = _read().get(sid)
        if not row or row.get("expires", 0) <= time.time():
            return None
        return {"id": sid, "name": row["name"], "tracks": row["tracks"]}


def id_from_url(url, bases):
    """Accept this server's exact public/local base only. Never fetch the URL."""
    try:
        link = urlsplit(url.strip())
        if link.scheme not in ("http", "https") or link.username or link.password:
            return None
        for base in bases:
            origin = urlsplit(base)
            prefix = origin.path.rstrip("/") + "/p/"
            if (link.scheme == origin.scheme and link.netloc.lower() == origin.netloc.lower()
                    and link.path.startswith(prefix)):
                sid = link.path[len(prefix):]
                if re.fullmatch(r"[A-Za-z0-9_-]{16}", sid):
                    return sid
    except ValueError:
        pass
    return None


def forget_by(who):
    with _lock:
        rows = _read()
        kept = {k: v for k, v in rows.items() if v.get("by") != who}
        if len(kept) != len(rows):
            write_atomic(_path(), json.dumps(kept))
        return len(rows) - len(kept)
