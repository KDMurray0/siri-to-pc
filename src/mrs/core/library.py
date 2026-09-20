"""Local music files, so owned songs play with no download."""

from __future__ import annotations

import json
import hashlib
import os
import threading
import time
from pathlib import Path

from ..config import config, state_file
from ..paths import write_atomic
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
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._next_monitor = 0.0
        self._load()

    # -- persistence ---------------------------------------------------
    def _index_file(self):
        return state_file("library.json")

    def _load(self) -> None:
        try:
            rows = json.loads(self._index_file().read_text("utf-8-sig"))
            if not isinstance(rows, list):
                raise TypeError("library index is not a list")
            tracks: list[Track] = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    log.warning("skipping malformed library row %d", index)
                    continue
                try:
                    clean = dict(row)
                    string_fields = ("video_id", "title", "artist", "album", "art",
                                     "url", "source", "path", "origin", "reason")
                    for field in string_fields:
                        value = clean.get(field, "")
                        if value is None:
                            clean[field] = ""
                        elif not isinstance(value, str):
                            raise TypeError(f"{field} must be text")
                    duration = clean.get("duration", 0)
                    if (isinstance(duration, bool)
                            or not isinstance(duration, (int, float))
                            or duration < 0 or duration > 86400):
                        raise TypeError("duration must be a non-negative number")
                    clean["duration"] = int(duration)
                    if not (clean.get("path") or clean.get("url")):
                        raise ValueError("row has no local file")
                    tracks.append(Track.from_dict(clean))
                except Exception as exc:
                    log.warning("skipping malformed library row %d: %s", index, exc)
            with self._lock:
                self._tracks = tracks
            log.info("library: %d tracks", len(self._tracks))
        except Exception:
            self._tracks = []

    def _save(self) -> None:
        try:
            with self._lock:
                rows = [t.to_dict() for t in self._tracks]
            write_atomic(self._index_file(), json.dumps(rows))
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

    @staticmethod
    def _path_key(path: Path) -> str:
        """Stable path identity across interpreter processes and restarts."""
        try:
            raw = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
        except Exception:
            raw = str(path)
        return raw.replace("\\", "/")

    @classmethod
    def _stable_id(cls, path: Path) -> str:
        return "local:v2:" + hashlib.sha256(
            cls._path_key(path).encode("utf-8", "surrogatepass")).hexdigest()[:32]

    def scan(self, folders: list[str] | None = None) -> int:
        with self._lock:
            if self._scanning:
                return 0
            self._scanning = True
        found: list[Track] = []
        roots = folders or config.get("library_paths") or []
        # Built once. Looking each file up by walking every known track was
        # quadratic, and a library is exactly the thing that gets large.
        with self._lock:
            known = {self._path_key(Path(t.path)): t.video_id
                     for t in self._tracks if t.path}
        try:
            for root in roots:
                base = Path(root)
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in AUDIO_EXT:
                        continue
                    title, artist, album, length = self._read_tags(path)
                    stable = self._stable_id(path)
                    # Preserve a previously persisted local id for the same
                    # path once, so old history/likes survive the v2 scheme.
                    # New files and rebuilt rows use the deterministic id.
                    old_id = known.get(self._path_key(path), "")
                    found.append(Track(
                        video_id=(old_id if old_id.startswith("local:") else stable),
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
            with self._lock:
                self._scanning = False

    def scan_async(self, folders: list[str] | None = None) -> None:
        threading.Thread(target=self.scan, args=(folders,), daemon=True).start()

    def start_monitor(self) -> None:
        """Start the opt-in periodic library refresh worker once.

        The interval is read on every tick so the setting can be changed
        without restarting the app. Zero keeps the daemon asleep; enabling it
        later wakes the same worker rather than creating duplicate scanners.
        """
        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            worker = threading.Thread(target=self._monitor, daemon=True,
                                      name="library monitor")
            self._monitor_thread = worker
        worker.start()

    def _monitor_due(self, now: float | None = None) -> bool:
        """Testable scheduler gate shared by the monitor and regression suite."""
        minutes = int(config.get("library_monitor_minutes", 0) or 0)
        if minutes <= 0 or not (config.get("library_paths") or []):
            return False
        with self._lock:
            if self._scanning:
                return False
            at = time.monotonic() if now is None else float(now)
            if at < self._next_monitor:
                return False
            # Prevent a slow scan from being queued repeatedly at each tick.
            self._next_monitor = at + max(5, minutes) * 60
        return True

    def _monitor(self) -> None:
        while not self._monitor_stop.wait(30):
            try:
                if self._monitor_due():
                    self.scan()
            except Exception as exc:
                log.warning("library monitor: %s", exc)

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
