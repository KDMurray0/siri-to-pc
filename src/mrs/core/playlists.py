"""Playlists, each one a folder under <data>/playlists/<name>/."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Track, norm_title
from ..paths import data_dir, write_atomic

log = get("playlists")

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# CON, PRN, AUX, NUL, COM1-9, LPT1-9: Windows won't make a directory with
# any of these names, with or without an extension.
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.I)


def _safe_name(name: str) -> str:
    """A folder name that can only ever land inside the playlists directory.

    The slash substitution already stopped "../../etc" walking out, but "."
    and ".." came through untouched — and folder("..") is the data directory,
    which delete() would then rmtree. That's the config, the cookies, the
    play stats and every cache, from one API call.
    """
    clean = _SAFE.sub("_", (name or "").strip())[:80]
    if not clean or clean.strip(".") == "" or _RESERVED.match(clean):
        return "untitled"
    return clean


class Playlists:
    """Saved lists. `home` is whose — the owner's, or one guest's folder."""

    def __init__(self, home: Path | None = None, session: str = "") -> None:
        self._home = home
        self._session = session
        self._lock = threading.RLock()
        self._downloading: set[str] = set()

    # -- layout --------------------------------------------------------
    def root(self) -> Path:
        p = (self._home or data_dir()) / "playlists"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def folder(self, name: str) -> Path:
        p = self._inside(_safe_name(name))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _index(self, name: str) -> Path:
        return self.folder(name) / "tracks.json"

    def _inside(self, leaf: str) -> Path:
        """Belt and braces: whatever _safe_name let through, this is still
        a child of the playlists directory or it doesn't happen at all.

        Worked out on the text, not the filesystem. resolve() asks Windows
        where a path really goes, and under a packaged container it redirects
        a child that exists into the app's private store while leaving the
        parent alone — so the two came back on different roots and every
        real playlist looked like an escape attempt. normpath collapses ".."
        without asking anybody, which is all this check ever needed.
        """
        root = os.path.normcase(os.path.normpath(str(self.root())))
        full = os.path.normcase(os.path.normpath(os.path.join(root, leaf)))
        if full == root or not full.startswith(root + os.sep):
            raise ValueError(f"playlist name escapes its directory: {leaf!r}")
        return Path(self.root()) / leaf

    # -- reads ---------------------------------------------------------
    def names(self) -> list[str]:
        out = []
        for d in sorted(self.root().iterdir()):
            if d.is_dir() and (d / "tracks.json").exists():
                out.append(self._display_name(d))
        return out

    @staticmethod
    def _display_name(folder: Path) -> str:
        meta = folder / "name.txt"
        if meta.exists():
            try:
                return meta.read_text(encoding="utf-8").strip() or folder.name
            except Exception:
                pass
        return folder.name

    def tracks(self, name: str) -> list[Track]:
        try:
            rows = json.loads(self._index(name).read_text("utf-8-sig"))
        except Exception:
            return []
        return [Track.from_dict(r) for r in rows]

    def summary(self) -> list[dict]:
        out = []
        for name in self.names():
            rows = self.tracks(name)
            downloaded = sum(1 for t in rows if t.path and Path(t.path).is_file())
            out.append({"name": name, "count": len(rows), "downloaded": downloaded,
                        "shared": self.is_shared(name),
                        "folder": str(self.folder(name))})
        return out

    # -- shared lists --------------------------------------------------
    # A list the whole house can add to. The folder stays the owner's —
    # there is one copy, not a copy each — and a marker file says it's open.
    # Who put each track in is kept beside the index rather than in it,
    # because the index is written out of Track objects and anything a
    # Track doesn't have a field for is lost on the next save.

    def _flag(self, name: str) -> Path:
        return self.folder(name) / "shared"

    def is_shared(self, name: str) -> bool:
        try:
            return self._flag(name).exists()
        except Exception:
            return False

    def set_shared(self, name: str, on: bool) -> dict:
        with self._lock:
            if not self._index(name).exists():
                return {"ok": False, "message": f"There's no list called {name}"}
            flag = self._flag(name)
            if on:
                flag.write_text("", encoding="utf-8")
            else:
                flag.unlink(missing_ok=True)
        self._save_event()
        return {"ok": True, "shared": bool(on),
                "message": f"{name} is {'open to everyone' if on else 'yours again'}"}

    def shared(self) -> list[str]:
        return [n for n in self.names() if self.is_shared(n)]

    def _by_file(self, name: str) -> Path:
        return self.folder(name) / "by.json"

    def credit(self, name: str) -> dict:
        """video id -> who added it. Empty for anything the owner put in."""
        try:
            return json.loads(self._by_file(name).read_text("utf-8-sig"))
        except Exception:
            return {}

    def _credit(self, name: str, tracks: list, who: str) -> None:
        if not who:
            return
        with self._lock:
            rows = self.credit(name)
            for t in tracks:
                if t and t.video_id:
                    rows[t.video_id] = who
            try:
                write_atomic(self._by_file(name), json.dumps(rows, indent=1))
            except Exception as exc:
                log.debug("couldn't record who added to %r: %s", name, exc)

    def find(self, query: str) -> list[str]:
        """Playlists whose name matches — used by search and by voice requests."""
        q = (query or "").strip().lower()
        if not q:
            return []
        exact = [n for n in self.names() if n.lower() == q]
        partial = [n for n in self.names() if q in n.lower() and n not in exact]
        return exact + partial

    # -- writes --------------------------------------------------------
    def create(self, name: str) -> str:
        with self._lock:
            folder = self.folder(name)
            (folder / "name.txt").write_text(name.strip(), encoding="utf-8")
            fresh = not self._index(name).exists()
            if fresh:
                self._save(name, [])
                # Only when it really is new. add() calls this for every
                # track, so logging unconditionally meant one line per track
                # on an import.
                log.info("created playlist %r at %s", name, folder)
        return name

    def _save_event(self) -> None:
        # Stamped with whose library changed. Unstamped, a guest editing
        # their own list sent the redraw to the owner's page and not to
        # theirs — the stream routes on that stamp.
        evt = {"playlists": True}
        if self._session:
            evt["session"] = self._session
        bus.publish(Ev.SETTINGS, evt)

    def _save(self, name: str, rows: list[dict]) -> None:
        write_atomic(self._index(name), json.dumps(rows, indent=1))
        self._save_event()

    def add(self, name: str, track: Track, by: str = "") -> dict:
        if not track or not (track.video_id or track.url):
            return {"ok": False, "message": "Nothing to add"}
        with self._lock:
            self.create(name)
            rows = [t.to_dict() for t in self.tracks(name)]
            key = norm_title(track.title, track.artist)
            if any(norm_title(r.get("title", ""), r.get("artist", "")) == key
                   for r in rows):
                # Matched on title+artist, not video id — the same song
                # re-uploaded under a different id is still the same song.
                return {"ok": True, "duplicate": True,
                        "message": f"{track.title} is already in {name}"}
            rows.append(track.to_dict())
            self._save(name, rows)
        self._credit(name, [track], by)
        if config.get("playlist_download"):
            self.download_async(name)
        return {"ok": True, "message": f"Added to {name}", "count": len(rows)}

    def add_many(self, name: str, tracks: list, by: str = "") -> dict:
        """Add a batch in one pass.

        add() re-reads the whole index, rewrites it and republishes settings
        once per track. For a fifty-track import that's fifty rewrites and
        fifty events for one logical change, and it gets quadratically worse
        the longer the playlist is.
        """
        good = [t for t in tracks if t and (t.video_id or t.url)]
        if not good:
            return {"ok": False, "message": "Nothing to add"}
        with self._lock:
            self.create(name)
            rows = [t.to_dict() for t in self.tracks(name)]
            seen = {norm_title(r.get("title", ""), r.get("artist", ""))
                    for r in rows}
            added = 0
            for t in good:
                key = norm_title(t.title, t.artist)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(t.to_dict())
                added += 1
            if added:
                self._save(name, rows)
        if added:
            self._credit(name, good, by)
        if added and config.get("playlist_download"):
            self.download_async(name)
        return {"ok": True, "message": f"Added {added} to {name}",
                "count": len(rows), "added": added}

    def remove(self, name: str, video_id: str) -> dict:
        with self._lock:
            rows = [t.to_dict() for t in self.tracks(name)
                    if t.video_id != video_id]
            self._save(name, rows)
        return {"ok": True, "message": "Removed", "count": len(rows)}

    def delete(self, name: str, *, keep_files: bool = False) -> dict:
        with self._lock:
            folder = self.folder(name)
            try:
                if keep_files:
                    self._index(name).unlink(missing_ok=True)
                else:
                    shutil.rmtree(folder, ignore_errors=True)
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": f"Deleted {name}"}

    # -- offline copies ------------------------------------------------
    def download_async(self, name: str) -> None:
        if name in self._downloading:
            return
        threading.Thread(target=self.download, args=(name,), daemon=True).start()

    def download(self, name: str) -> dict:
        """Copy every track in the playlist into its folder."""
        from .downloader import downloader
        if name in self._downloading:
            return {"ok": False, "message": "Already downloading"}
        self._downloading.add(name)
        folder = self.folder(name)
        saved = 0
        try:
            rows = self.tracks(name)
            for i, track in enumerate(rows, 1):
                if track.path and Path(track.path).is_file() and \
                        Path(track.path).parent == folder:
                    continue
                bus.publish(Ev.ACTIVITY, {"stage": "downloading",
                                          "detail": f"{name}: {track.title}",
                                          "progress": i / max(1, len(rows))})
                src = downloader.fetch_with_fallbacks(track)
                if not src:
                    continue
                stem = _safe_name(f"{track.artist} - {track.title}".strip(" -"))
                dest = folder / (stem + Path(src).suffix)
                try:
                    if not dest.exists():
                        shutil.copy2(src, dest)
                    track.path = str(dest)
                    saved += 1
                except Exception as exc:
                    log.debug("copy failed: %s", exc)
                time.sleep(0.2)      # be gentle on YouTube
            with self._lock:
                self._save(name, [t.to_dict() for t in rows])
            log.info("playlist %r: %d files on disk", name, saved)
            bus.publish(Ev.TOAST, f"{name}: {saved} tracks saved offline")
            bus.publish(Ev.ACTIVITY, {"stage": "idle"})
            return {"ok": True, "saved": saved, "folder": str(folder)}
        finally:
            self._downloading.discard(name)


playlists = Playlists()
