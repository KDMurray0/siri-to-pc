"""yt-dlp wrapper.

Streaming from YouTube 403s, so everything gets downloaded first and mpv is
handed the file.
"""

from __future__ import annotations

import os
import queue as queue_mod
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..config import config
from ..events import Ev
from ..logging_setup import get
from ..models import Track
from ..paths import cache_dir, pinned_dir

log = get("download")

CREATE_NO_WINDOW = 0x08000000
_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class DownloadError(Exception):
    pass


# How long a guest defers to the owner before going ahead regardless. Short
# on purpose: the owner still wins every contested slot, and six seconds a
# track is long enough to feel like the thing is broken rather than busy.
GUEST_YIELD = 2.5


class Lanes:
    """Who gets to use the network, and in what order.

    Two rules, and the first one is absolute: while the owner has anything to
    fetch, a guest's fetch does not start. Not "usually" — a friend queueing
    forty tracks cannot make your next song wait, because their work simply
    isn't eligible while yours exists.

    Between guests it's first come, first served under a shared ceiling, so
    one person hitting a script can't spend the whole machine's bandwidth.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._owner = 0            # owner fetches running or waiting
        self._running = 0          # everything running, guests included

    def _ceiling(self) -> int:
        return max(1, int(config.get("max_downloads", 4)))

    def enter(self, owner: bool) -> None:
        """Owner first, but never *never* for a guest.

        The obvious rule — a guest waits while the owner has anything pending
        — starves them completely, because the owner's queue refills itself
        continuously and is therefore almost never idle. So a guest yields for
        a few seconds and then goes anyway: the owner still wins every
        contested slot in practice, and a guest's song arrives a little later
        instead of never.
        """
        deadline = time.monotonic() + GUEST_YIELD
        with self._cond:
            if owner:
                self._owner += 1
            while True:
                room = self._running < self._ceiling()
                yielded = owner or self._owner == 0 or time.monotonic() >= deadline
                if room and yielded:
                    self._running += 1
                    return
                self._cond.wait(timeout=0.25)

    def leave(self, owner: bool) -> None:
        with self._cond:
            self._running = max(0, self._running - 1)
            if owner:
                self._owner = max(0, self._owner - 1)
            self._cond.notify_all()

    def busy(self) -> dict:
        with self._cond:
            return {"running": self._running, "owner_waiting": self._owner,
                    "ceiling": self._ceiling()}


lanes = Lanes()


# Which queue a download thread is fetching for. Thread-local because the
# fetch runs on that queue's own worker — there is nothing to thread through
# five call sites, and getting it wrong is how one person's stop button came
# to kill everybody's downloads.
_lane_of = threading.local()


# What is arriving right now. This is progress metadata only: the file stays
# private until yt-dlp has atomically published its final name.
_INFLIGHT: dict[str, dict] = {}
_INFLIGHT_LOCK = threading.Lock()
# "[download]  12.3% of  3.69MiB at ..." — the size it will end up.
_TOTAL = re.compile(r"of\s+~?([\d.]+)\s*([KMG])iB", re.I)
_UNIT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _note_inflight(vid: str, **kw) -> None:
    if not vid:
        return
    with _INFLIGHT_LOCK:
        row = _INFLIGHT.setdefault(vid, {"total": 0, "started": time.time()})
        row.update(kw)


def _drop_inflight(vid: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(vid, None)


def arriving(vid: str) -> dict | None:
    """{'total': bytes} while this id is downloading, else None.

    The point of knowing the final size is that it lets the partial file be
    served with a truthful Content-Range — which is what a phone needs before
    it will show a duration or let anyone scrub.
    """
    with _INFLIGHT_LOCK:
        row = _INFLIGHT.get(vid)
        return dict(row) if row else None


class Downloader:
    def __init__(self) -> None:
        self.exe = shutil.which("yt-dlp")
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        # running yt-dlp processes -> whose queue asked for them
        self._procs: dict = {}
        self._cancelled = False
        self._remove_stale_partials()

    @staticmethod
    def _remove_stale_partials() -> int:
        """Remove abandoned yt-dlp temporary files from an older run."""
        removed = 0
        try:
            age = max(3600, int(config.get("download_timeout", 240)) * 2)
            cutoff = time.time() - age
            root = cache_dir()
            for path in root.glob("*.part"):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        except Exception as exc:
            log.debug("couldn't clean stale download parts: %s", exc)
        if removed:
            log.info("removed %d abandoned download part(s)", removed)
        return removed

    @staticmethod
    def claim_lane(tag: str) -> None:
        """Say whose downloads this thread is doing. "" is the owner's."""
        _lane_of.tag = tag or ""

    @staticmethod
    def _lane() -> str:
        return getattr(_lane_of, "tag", "")

    # -- options -------------------------------------------------------
    def auth_args(self, client: str | None = None) -> list[str]:
        """Cookie + runtime flags, read fresh so a cookie refresh takes effect."""
        args: list[str] = []
        # a copy, never the master — yt-dlp rewrites whatever it's given
        from .cookies import ensure_session
        cf = ensure_session()
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
                # `.part` and `.ytdl` are downloader-owned temporary state,
                # never a playable cache hit. A short legitimate file should
                # not be rejected merely because it is under 100 KB.
                if (f.is_file() and f.stat().st_size > 0
                        and not f.name.endswith((".part", ".ytdl", ".complete"))):
                    return str(f)

    @staticmethod
    def _mark_complete(path: str | None) -> None:
        if not path:
            return
        try:
            Path(path).with_name(Path(path).name + ".complete").write_text(
                "1", encoding="ascii")
        except OSError:
            pass
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
            marker = src.with_name(src.name + ".complete")
            if marker.is_file():
                shutil.copy2(marker, dst.with_name(dst.name + ".complete"))
            return str(dst)
        except Exception as exc:
            log.warning("pin failed: %s", exc)
            return None

    def prune_cache(self, keep_mb: int | None = None,
                    keep: set[str] | None = None) -> int:
        """Hold the cache under its size limit, dropping the least-played first.

        Oldest-first was throwing away the songs played every day: playing a
        file doesn't touch its mtime, so a favourite downloaded in March looked
        exactly as stale as something heard once and never again. Ranked by how
        often it has actually been played through, with liked tracks weighted
        up and recency only breaking ties.

        `keep` is whatever the queue still needs — a downloaded track that
        hasn't been reached yet is otherwise just an old file on disk.
        """
        if keep_mb is None:
            keep_mb = int(config.get("cache_size_mb", 2000))
        from .taste import taste
        from . import cast

        cast.prune()          # transcodes whose source already went

        root = cache_dir()
        # The limit is about disk used, so everything under here counts: the
        # cast transcodes in the subfolder and the files the queue still needs
        # included. Measuring only the prunable ones is how it sat at 105% —
        # it was reporting the part it was allowed to delete, not the total.
        every = [f for f in root.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in every)
        limit = max(0, keep_mb) * 1024 * 1024
        if total <= limit:
            return 0

        spare = {os.path.normcase(os.path.abspath(p)) for p in (keep or ())}
        with _INFLIGHT_LOCK:
            active_ids = set(_INFLIGHT)
        plays, liked = taste.play_counts(), taste.liked_ids()
        files = [f for f in every
                 if f.parent == root
                 and not f.name.endswith((".part", ".ytdl", ".complete"))
                 and not any(f.name.startswith(f"{self._safe_id(vid)}.")
                             for vid in active_ids)
                 and os.path.normcase(str(f.resolve())) not in spare]

        def worth(f: Path) -> tuple:
            vid = f.stem
            # A like is worth about three plays: it says keep this even if the
            # run of listens hasn't happened yet.
            return (plays.get(vid, 0) + (3 if vid in liked else 0),
                    f.stat().st_mtime)

        files.sort(key=worth)          # least worth keeping goes first
        removed = 0
        for f in files:
            if total <= limit:
                break
            size, vid = f.stat().st_size, f.stem
            try:
                f.unlink()
            except Exception:
                continue
            try:
                f.with_name(f.name + ".complete").unlink(missing_ok=True)
            except OSError:
                pass
            total -= size
            removed += 1
            # Its transcode is dead weight the moment the source goes.
            output_exts = {spec["ext"] for spec in cast.FORMATS.values()}
            for t in cast.work_dir().iterdir():
                if (not t.is_file() or t.suffix.lower() not in output_exts
                        or (t.stem != vid and not t.stem.startswith(f"{vid}~"))):
                    continue
                try:
                    total -= t.stat().st_size
                    t.unlink()
                except Exception:
                    pass
        return removed

    def cache_size(self) -> int:
        """Bytes on disk, transcodes and all. Cheap enough to poll."""
        return sum(f.stat().st_size
                   for f in cache_dir().rglob("*") if f.is_file())

    def cache_stats(self) -> dict:
        """Size on disk against the limit, for the settings panel."""
        files = [f for f in cache_dir().rglob("*") if f.is_file()]
        used = sum(f.stat().st_size for f in files)
        limit = int(config.get("cache_size_mb", 2000))
        return {"files": len(files), "used_mb": round(used / 1024 / 1024, 1),
                "mb": round(used / 1024 / 1024, 1),   # health panel's name for it
                "limit_mb": limit,
                "percent": round(used / (limit * 1024 * 1024) * 100, 1)
                           if limit else 0.0}

    # -- fetching ------------------------------------------------------
    def _run(self, args: list[str], on_progress=None, timeout: int | None = None,
             vid: str = "") -> tuple[int, str]:
        timeout = timeout or int(config.get("download_timeout", 240))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW, bufsize=1)
        with self._lock:
            self._procs[proc] = self._lane()
        out_lines: list[str] = []
        deadline = time.monotonic() + timeout

        # Iterating directly over a pipe blocks until yt-dlp emits a line.
        # A network stall is exactly the case where it emits nothing, so read
        # on a helper thread and let this thread own the deadline.
        lines: "queue_mod.Queue[str | None]" = queue_mod.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            except Exception as exc:
                lines.put(f"\nOUTPUT ERROR: {exc}\n")
            finally:
                lines.put(None)

        reader_thread = threading.Thread(target=reader, daemon=True,
                                         name=f"yt-dlp output {vid or 'job'}")
        reader_thread.start()
        try:
            eof = False
            timed_out = False
            while not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = lines.get(timeout=min(0.2, remaining))
                except queue_mod.Empty:
                    if proc.poll() is not None:
                        # The reader may still have a final buffered line.
                        continue
                    continue
                if line is None:
                    eof = True
                    continue
                out_lines.append(line)
                if vid:
                    t = _TOTAL.search(line)
                    if t:
                        try:
                            _note_inflight(vid, total=int(
                                float(t.group(1)) * _UNIT[t.group(2).upper()]))
                        except Exception:
                            pass
                if on_progress:
                    m = _PCT.search(line)
                    if m:
                        try:
                            on_progress(float(m.group(1)) / 100.0)
                        except Exception:
                            pass
            if timed_out:
                self._stop_process(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._stop_process(proc, force=True)
                    proc.wait(timeout=5)
                return 1, "".join(out_lines) + "\nTIMEOUT"
            proc.wait(timeout=20)
            # Drain anything the reader queued between process exit and EOF.
            while True:
                try:
                    line = lines.get_nowait()
                except queue_mod.Empty:
                    break
                if line is None:
                    break
                out_lines.append(line)
        except Exception as exc:
            self._stop_process(proc, force=True)
            return 1, "".join(out_lines) + f"\n{exc}"
        finally:
            reader_thread.join(timeout=2)
            # Finished processes used to stay in the list forever, so the
            # cancel count was the number of downloads since boot.
            with self._lock:
                self._procs.pop(proc, None)
        return proc.returncode, "".join(out_lines[-40:])

    @staticmethod
    def _stop_process(proc, force: bool = False) -> None:
        """Stop yt-dlp and its ffmpeg children, then let the pipe close."""
        try:
            if os.name == "nt" and proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T",
                                "/F"], capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
            elif proc.poll() is None:
                (proc.kill if force else proc.terminate)()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def cancel_all(self, lane: str | None = None) -> int:
        """Stop running fetches (the X next to the progress bar).

        `lane` is whose. Without it this killed every yt-dlp in the house, so
        one guest pressing stop — or simply asking for another song, which
        cancels first — took out the owner's download and everybody else's.
        None still means all of them, which is what shutdown wants.
        """
        with self._lock:
            procs = [p for p, tag in self._procs.items()
                     if lane is None or tag == lane]
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
        if procs:
            log.info("cancelled %d download(s)%s", len(procs),
                     "" if lane is None else f" for {lane or 'the player'}")
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
            path = self._fetch_locked(track, on_progress)
            if path:
                from . import stats
                try:
                    stats.note(stats.HOUSE, downloads=1,
                               bytes_in=Path(path).stat().st_size)
                except OSError:
                    pass
            return path
        finally:
            # This is the one cleanup boundary that also covers an exception
            # from config, argument construction, logging, or a mocked
            # process. No caller can be left permanently marked arriving.
            _drop_inflight(track.video_id)
            with self._lock:
                self._inflight.pop(track.video_id, None)
            ev.set()

    def _format(self) -> str:
        """What to ask YouTube for.

        bestaudio picks opus over m4a on a five-kilobit difference — 135k
        against 130k — and that five kilobits costs two to four seconds of
        transcoding every time a phone plays the track, because no iPhone
        will play opus in a webm. Worse, the transcode is opus decoded and
        re-encoded to AAC: a second lossy pass, which is a worse record than
        the AAC that was sitting there all along.

        So prefer the one phones can play. The PC loses about five kilobits
        of a more efficient codec; the phones lose the wait entirely.
        """
        if config.get("prefer_native_audio", True):
            return ("bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/"
                    "bestaudio/best")
        return "bestaudio/best"

    def _client_chain(self) -> list[str]:
        """Clients to try, the one that worked last time first.

        YouTube breaks these periodically and the order here was fixed, so
        every download in the house paid the same eight seconds to rediscover
        the same breakage: 390 first-client failures in the current log and
        390 rescues by mweb, a perfect correlation and about fifty minutes of
        waiting. Remembering the answer costs one config write per change.
        """
        chain: list[str] = []
        good = config.get("player_client_good")
        if good is not None:
            chain.append(str(good))
        for name in ([str(config.get("player_client") or "")]
                     + list(config.get("player_client_fallbacks") or [])):
            if str(name) not in chain:
                chain.append(str(name))
        return chain

    def _fetch_locked(self, track: Track, on_progress) -> str | None:
        sid = self._safe_id(track.video_id)
        out_tmpl = str(cache_dir() / f"{sid}.%(ext)s")
        target = track.url or f"https://www.youtube.com/watch?v={track.video_id}"
        min_dur = int(config.get("min_duration", 60) or 0)
        retries = int(config.get("download_retries", 2) or 0)
        clients = self._client_chain() if track.source == "youtube" else [""]
        last = ""

        _note_inflight(track.video_id, total=0)
        for client in clients:
            args = [self.exe, "-f", self._format(), "--no-playlist", "--part",
                    "--newline", "--no-warnings", "-o", out_tmpl]
            # no 30-second preview uploads
            if min_dur > 0 and track.source == "youtube":
                args += ["--match-filter", f"duration >= {min_dur}"]
            args += self.auth_args(client=client)
            args += ["--", target]

            for attempt in range(retries + 1):
                if attempt:
                    time.sleep(min(8, 2 ** attempt))
                code, out = self._run(args, on_progress=on_progress,
                                      vid=track.video_id)
                last = out
                if code == 0:
                    path = self.cached(track.video_id)
                    if path:
                        self._mark_complete(path)
                        if client != clients[0]:
                            log.info("%r needed the %s client", track.title,
                                     client or "default")
                        # Start here next time. Written only on a change, so
                        # the steady state costs nothing.
                        if config.get("player_client_good") != client:
                            config.set("player_client_good", client)
                            log.info("remembering %r as the client that works",
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
