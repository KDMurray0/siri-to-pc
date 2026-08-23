"""Playlists, each one a folder under <data>/playlists/<name>/."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Track, norm_title
from ..paths import data_dir

log = get("playlists")

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", (name or "").strip())[:80] or "untitled"


class Playlists:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._downloading: set[str] = set()

    # -- layout --------------------------------------------------------
    def root(self) -> Path:
        p = data_dir() / "playlists"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def folder(self, name: str) -> Path:
        p = self.root() / _safe_name(name)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _index(self, name: str) -> Path:
        return self.folder(name) / "tracks.json"

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
                        "folder": str(self.folder(name))})
        return out

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
            if not self._index(name).exists():
                self._save(name, [])
            log.info("created playlist %r at %s", name, folder)
        return name

    def _save(self, name: str, rows: list[dict]) -> None:
        self._index(name).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        bus.publish(Ev.SETTINGS, {"playlists": True})

    def add(self, name: str, track: Track) -> dict:
        if not track or not (track.video_id or track.url):
            return {"ok": False, "message": "Nothing to add"}
        with self._lock:
            self.create(name)
            rows = [t.to_dict() for t in self.tracks(name)]
            key = norm_title(track.title, track.artist)
            if any(norm_title(r.get("title", ""), r.get("artist", "")) == key
                   for r in rows):
                return {"ok": True, "message": f"Already in {name}"}
            rows.append(track.to_dict())
            self._save(name, rows)
        if config.get("playlist_download"):
            self.download_async(name)
        return {"ok": True, "message": f"Added to {name}", "count": len(rows)}

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
