"""yt-dlp wrapper.

Streaming from YouTube 403s, so everything gets downloaded first and mpv is
handed the file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Track
from ..paths import cache_dir, pinned_dir

log = get("download")

CREATE_NO_WINDOW = 0x08000000
_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(self) -> None:
        self.exe = shutil.which("yt-dlp")
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._procs: set = set()          # running yt-dlp processes, for cancel
        self._cancelled = False

    # -- options -------------------------------------------------------
    def auth_args(self, client: str | None = None) -> list[str]:
        """Cookie + runtime flags, read fresh so a cookie refresh takes effect."""
        args: list[str] = []
        cf = (config.get("cookies_file") or "").strip()
        cb = (config.get("cookies_from_browser") or "").strip()
        if cf and os.path.isfile(cf):
            args += ["--cookies", cf]
        elif cb:
            args += ["--cookies-from-browser", cb]
        if config.get("js_runtime"):
            args += ["--js-runtimes", str(config.get("js_runtime"))]
        pc = config.get("player_client") if client is None else client
        if pc:
            args += ["--extractor-args", f"youtube:player_client={pc}"]
        return args

    def have_cookies(self) -> bool:
        cf = (config.get("cookies_file") or "").strip()
        return bool((cf and os.path.isfile(cf)) or config.get("cookies_from_browser"))

    # -- cache ---------------------------------------------------------
    @staticmethod
    def _safe_id(video_id: str) -> str:
        return _SAFE.sub("_", video_id or "unknown")[:100]

    def cached(self, video_id: str) -> str | None:
        sid = self._safe_id(video_id)
        for folder in (pinned_dir(), cache_dir()):
            for f in folder.glob(f"{sid}.*"):
                if f.is_file() and f.stat().st_size > 100_000:
                    return str(f)
        return None

    def pin(self, path: str) -> str | None:
        """Copy a cached file somewhere cache cleanup won't touch it."""
        try:
            src = Path(path)
            if not src.is_file():
                return None
            dst = pinned_dir() / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
            return str(dst)
        except Exception as exc:
            log.warning("pin failed: %s", exc)
            return None

    def unpin(self, video_id: str) -> bool:
        sid = self._safe_id(video_id)
        gone = False
        for f in pinned_dir().glob(f"{sid}.*"):
            try:
                f.unlink()
                gone = True
            except Exception:
                pass
        return gone

    def prune_cache(self, keep_mb: int = 2000) -> int:
        """Drop the oldest cached files once the cache gets fat."""
        files = sorted((f for f in cache_dir().glob("*") if f.is_file()),
                       key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        limit = keep_mb * 1024 * 1024
        removed = 0
        for f in files:
            if total <= limit:
                break
            size = f.stat().st_size
            try:
                f.unlink()
                total -= size
                removed += 1
            except Exception:
                pass
        return removed

    # -- fetching ------------------------------------------------------
    def _run(self, args: list[str], on_progress=None, timeout: int | None = None) -> tuple[int, str]:
        timeout = timeout or int(config.get("download_timeout", 240))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW, bufsize=1)
        with self._lock:
            self._procs.add(proc)
        out_lines: list[str] = []
        deadline = time.time() + timeout
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                out_lines.append(line)
                if on_progress:
                    m = _PCT.search(line)
                    if m:
                        try:
                            on_progress(float(m.group(1)) / 100.0)
                        except Exception:
                            pass
                if time.time() > deadline:
                    proc.kill()
                    return 1, "".join(out_lines) + "\nTIMEOUT"
            proc.wait(timeout=20)
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            return 1, "".join(out_lines) + f"\n{exc}"
        return proc.returncode, "".join(out_lines[-40:])

    def cancel_all(self) -> int:
        """Stop every running fetch (the X next to the progress bar)."""
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
        if procs:
            log.info("cancelled %d download(s)", len(procs))
        return len(procs)

    def fetch(self, track: Track, *, on_progress=None) -> str | None:
        """Download one track. Returns a path, or None if it can't be had."""
        if not self.exe:
            log.error("yt-dlp not on PATH")
            return None

        hit = self.cached(track.video_id)
        if hit:
            return hit

        # Collapse duplicate concurrent requests for the same id.
        with self._lock:
            ev = self._inflight.get(track.video_id)
            if ev is None:
                ev = threading.Event()
                self._inflight[track.video_id] = ev
                owner = True
            else:
                owner = False
        if not owner:
            ev.wait(timeout=300)
            return self.cached(track.video_id)

        try:
            return self._fetch_locked(track, on_progress)
        finally:
            with self._lock:
                self._inflight.pop(track.video_id, None)
            ev.set()

    def _client_chain(self) -> list[str]:
        """Clients to try in order. YouTube breaks these periodically."""
        chain = [str(config.get("player_client") or "")]
        for alt in (config.get("player_client_fallbacks") or []):
            alt = str(alt)
            if alt not in chain:
                chain.append(alt)
        return chain

    def _fetch_locked(self, track: Track, on_progress) -> str | None:
        sid = self._safe_id(track.video_id)
        out_tmpl = str(cache_dir() / f"{sid}.%(ext)s")
        target = track.url or f"https://www.youtube.com/watch?v={track.video_id}"
        min_dur = int(config.get("min_duration", 60) or 0)
        retries = int(config.get("download_retries", 2) or 0)
        clients = self._client_chain() if track.source == "youtube" else [""]
        last = ""

        for client in clients:
            args = [self.exe, "-f", "bestaudio/best", "--no-playlist", "--no-part",
                    "--newline", "--no-warnings", "-o", out_tmpl]
            # no 30-second preview uploads
            if min_dur > 0 and track.source == "youtube":
                args += ["--match-filter", f"duration >= {min_dur}"]
            args += self.auth_args(client=client)
            args += ["--", target]

            for attempt in range(retries + 1):
                if attempt:
                    time.sleep(min(8, 2 ** attempt))
                code, out = self._run(args, on_progress=on_progress)
                last = out
                if code == 0:
                    path = self.cached(track.video_id)
                    if path:
                        if client != clients[0]:
                            log.info("%r needed the %s client", track.title,
                                     client or "default")
                        return path
                    if "does not pass filter" in out:
                        log.info("rejected (too short): %s", track.title)
                        return None
                if self._fatal(out):
                    return None
                if self._client_broken(out):
                    break     # no point retrying this client, try the next one
            log.info("client %r failed for %r — trying the next",
                     client or "default", track.title)

        log.warning("download failed for %r: %s", track.title, self._reason(last))
        return None

    @staticmethod
    def _client_broken(out: str) -> bool:
        """Signs the player client itself is the problem, not the network."""
        low = (out or "").lower()
        return any(s in low for s in (
            "page needs to be reloaded", "403: forbidden", "unable to download video data",
            "no video formats", "only images are available"))

    @staticmethod
    def _fatal(out: str) -> bool:
        """Errors that retrying can't fix."""
        low = (out or "").lower()
        return any(s in low for s in (
            "video unavailable", "private video", "removed by the uploader",
            "does not pass filter", "members-only", "age-restricted"))

    @staticmethod
    def _reason(out: str) -> str:
        for line in reversed((out or "").splitlines()):
            if "ERROR" in line:
                return line.strip()[:180]
        return (out or "").strip()[-180:] or "unknown"

    # -- the full chain ------------------------------------------------
    def fetch_with_fallbacks(self, track: Track, alternates: list[str] | None = None,
                             *, on_progress=None) -> str | None:
        """YouTube id -> alternate ids -> SoundCloud. Never raises."""
        path = self.fetch(track, on_progress=on_progress)
        if path:
            return path

        for alt in (alternates or []):
            alt_track = Track(**{**track.to_dict(), "video_id": alt, "url": ""})
            path = self.fetch(alt_track, on_progress=on_progress)
            if path:
                return path

        return self._from_soundcloud(track, on_progress=on_progress)

    def _from_soundcloud(self, track: Track, *, on_progress=None) -> str | None:
        query = f"{track.title} {track.artist}".strip()
        if not query:
            return None
        try:
            from ..resolve.catalog import search_soundcloud
            hits = search_soundcloud(query, limit=1)
        except Exception as exc:
            log.debug("soundcloud lookup failed: %s", exc)
            return None
        if not hits:
            return None
        alt = hits[0]
        log.info("YouTube failed for %r — trying SoundCloud", query)
        sc = Track(**{**track.to_dict(),
                      "video_id": alt.video_id or f"sc_{self._safe_id(query)}",
                      "url": alt.url, "source": "soundcloud"})
        return self.fetch(sc, on_progress=on_progress)


downloader = Downloader()
