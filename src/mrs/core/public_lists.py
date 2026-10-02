"""Playlists anyone here can find.

A list is made public by whoever owns it, with a few tags saying what it is.
The flag lives in the list's own folder (public.json), so a rename keeps it
and a delete takes it with the list to the bin. The catalogue is every public
list on the server, read where it lives, and shown to each listener ranked by
how well its tags match what they play.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("public")

MARK = "public.json"
MAX_TAGS = 6
TTL = 30.0                       # the catalogue is re-read at most this often

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "rows": []}


def tidy_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", str(t)).strip().lower()[:30]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def forget() -> None:
    with _lock:
        _cache["at"] = 0.0


def marker(store, name: str) -> dict | None:
    try:
        got = json.loads((store._folder_path(name) / MARK).read_text("utf-8"))
        return got if isinstance(got, dict) else None
    except Exception:
        return None


def publish(store, name: str, tags, by: str, owner: str) -> dict:
    if name not in store.names():
        return {"ok": False, "message": f"There's no list called {name}"}
    tags = tidy_tags(tags)
    if not tags:
        return {"ok": False, "message": "Give it at least one tag, so it finds its people"}
    old = marker(store, name) or {}
    row = {"id": old.get("id") or secrets.token_hex(8), "tags": tags,
           "by": (by or "")[:40], "owner": owner, "at": int(time.time())}
    write_atomic(store._folder_path(name) / MARK, json.dumps(row))
    forget()
    store._save_event()
    return {"ok": True, "message": f"{name} is public", "id": row["id"], "tags": tags}


def unpublish(store, name: str) -> dict:
    path = store._folder_path(name) / MARK
    if not path.is_file():
        return {"ok": False, "message": f"{name} isn't public"}
    path.unlink(missing_ok=True)
    forget()
    store._save_event()
    return {"ok": True, "message": f"{name} is private again"}


def _stores():
    from .playlists import Playlists, playlists
    yield playlists
    root = data_dir() / "profiles"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "playlists").is_dir():
                yield Playlists(home=d)


def catalogue() -> list[dict]:
    """Every public list on the server, as of the last read."""
    with _lock:
        if time.time() - _cache["at"] < TTL:
            return list(_cache["rows"])
    rows = []
    for store in _stores():
        try:
            folders = [f for f in store.root().iterdir() if (f / MARK).is_file()]
        except OSError:
            continue
        for folder in folders:
            try:
                m = json.loads((folder / MARK).read_text("utf-8"))
                name = store._display_name(folder)
                tracks = store.tracks(name)
            except Exception as exc:
                log.debug("skipping a public list: %s", exc)
                continue
            if not tracks or not isinstance(m, dict) or not m.get("id"):
                continue
            rows.append({"id": m["id"], "name": name, "tags": tidy_tags(m.get("tags")),
                         "by": m.get("by") or "", "owner": m.get("owner") or "",
                         "count": len(tracks), "arts": [t.art for t in tracks[:4] if t.art],
                         "seconds": sum(t.duration or 0 for t in tracks), "at": m.get("at", 0),
                         "_store": store})
    with _lock:
        _cache.update(at=time.time(), rows=rows)
    return list(rows)


def find(list_id: str) -> dict | None:
    return next((r for r in catalogue() if r["id"] == list_id), None)


def tracks(row: dict):
    return row["_store"].tracks(row["name"])


def for_listener(genres: list[str], me: str, limit: int = 16) -> list[dict]:
    """Others' public lists, the ones whose tags match what this listener
    plays first; then the newest."""
    weight = {g.lower(): 1.0 / (1 + i) for i, g in enumerate(genres or [])}

    def score(row: dict) -> float:
        hit = 0.0
        for tag in row["tags"]:
            for g, w in weight.items():
                if tag == g:
                    hit += w
                elif tag in g or g in tag:
                    hit += w * .5
        return hit

    mine = [r for r in catalogue() if r["owner"] != me]
    mine.sort(key=lambda r: (-score(r), -r["at"]))
    return [describe(r) | {"fit": round(score(r), 3)} for r in mine[:limit]]


def suggest_tags(store, name: str) -> list[str]:
    """What a list sounds like, from its artists' own tags."""
    from collections import Counter
    from ..models import Track
    from .home import _NOT_GENRES
    from .tags import tagstore
    count: Counter = Counter()
    artists = list(dict.fromkeys((t.artist or "").split(",")[0].strip()
                                 for t in store.tracks(name) if t.artist))[:20]
    for who in artists:
        got = tagstore.cached(Track(title="", artist=who)) or {}
        for tag, _ in sorted(got.items(), key=lambda kv: -kv[1])[:5]:
            if tag not in _NOT_GENRES and len(tag) <= 24:
                count[tag] += 1
    return [t for t, _ in count.most_common(10)]


def describe(row: dict) -> dict:
    """What a listener may see: never whose library it's in, only the name they gave."""
    return {k: v for k, v in row.items() if not k.startswith("_") and k != "owner"}

