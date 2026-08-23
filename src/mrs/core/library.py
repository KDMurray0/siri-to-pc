"""Local music files, so owned songs play with no download."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..config import config, state_file
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Track, norm_title

log = get("library")

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma"}


class LocalLibrary:
    def __init__(self) -> None:
        self._tracks: list[Track] = []
        self._lock = threading.RLock()
        self._scanning = False
        self._load()

    # -- persistence ---------------------------------------------------
    def _index_file(self):
        return state_file("library.json")

    def _load(self) -> None:
        try:
            rows = json.loads(self._index_file().read_text("utf-8-sig"))
            with self._lock:
                self._tracks = [Track.from_dict(r) for r in rows]
            log.info("library: %d tracks", len(self._tracks))
        except Exception:
            self._tracks = []

    def _save(self) -> None:
        try:
            with self._lock:
                rows = [t.to_dict() for t in self._tracks]
            self._index_file().write_text(json.dumps(rows), encoding="utf-8")
        except Exception as exc:
            log.debug("library save failed: %s", exc)

    # -- scanning ------------------------------------------------------
    @staticmethod
    def _read_tags(path: Path) -> tuple[str, str, str, int]:
        title = path.stem
        artist = album = ""
        length = 0
        try:
            from mutagen import File as MFile
            mf = MFile(str(path), easy=True)
            if mf:
                title = (mf.get("title") or [title])[0]
                artist = (mf.get("artist") or [""])[0]
                album = (mf.get("album") or [""])[0]
                length = int(getattr(mf.info, "length", 0) or 0)
        except Exception:
            pass
        if not artist and " - " in path.stem:
            left, right = path.stem.split(" - ", 1)
            artist, title = left.strip(), right.strip()
        return title, artist, album, length

    def scan(self, folders: list[str] | None = None) -> int:
        if self._scanning:
            return 0
        self._scanning = True
        found: list[Track] = []
        roots = folders or config.get("library_paths") or []
        try:
            for root in roots:
                base = Path(root)
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in AUDIO_EXT:
                        continue
                    title, artist, album, length = self._read_tags(path)
                    found.append(Track(
                        video_id=f"local:{abs(hash(str(path))) & 0xFFFFFFFF:x}",
                        title=title, artist=artist, album=album, duration=length,
                        path=str(path), url=str(path), source="local",
                        origin="library"))
                    if len(found) % 200 == 0:
                        bus.publish(Ev.LIBRARY, {"scanning": True, "found": len(found)})
            with self._lock:
                self._tracks = found
            self._save()
            log.info("library scan complete: %d tracks", len(found))
            bus.publish(Ev.LIBRARY, {"scanning": False, "found": len(found)})
            return len(found)
        finally:
            self._scanning = False

    def scan_async(self, folders: list[str] | None = None) -> None:
        threading.Thread(target=self.scan, args=(folders,), daemon=True).start()

    # -- reads ---------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return len(self._tracks)

    def search(self, query: str, limit: int = 10) -> list[Track]:
        q = (query or "").strip().lower()
        if not q:
            return []
        words = q.split()
        out = []
        with self._lock:
            for t in self._tracks:
                hay = f"{t.title} {t.artist} {t.album}".lower()
                if all(w in hay for w in words):
                    out.append(t)
                    if len(out) >= limit:
                        break
        return out

    def find_exact(self, title: str, artist: str = "") -> Track | None:
        """Do we already own this song? Avoids downloading what's on disk."""
        want = norm_title(title, artist)
        if not want:
            return None
        with self._lock:
            for t in self._tracks:
                if t.key() == want:
                    return t
        return None


library = LocalLibrary()
