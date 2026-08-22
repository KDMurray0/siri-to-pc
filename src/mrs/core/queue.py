"""The queue.

The old design asked "is the playlist short? then download some songs" on one
thread, so a couple of failed downloads ended a refill with nothing added and
the queue quietly ran dry. This one separates *ideas* from *files*:

    context  ->  candidate pool  ->  download workers  ->  mpv playlist
                 (scored, cheap)     (retry, backoff)      (only real files)

Consequences that matter:
  * a failed download costs a candidate, never the refill — the worker just
    takes the next one;
  * we can guarantee "N downloaded tracks are ready ahead of you" rather than
    hoping;
  * user requests always jump the radio, because they enter a priority lane;
  * an artist run-length cap stops the queue turning into one album, while the
    cohesion weight still keeps it on the band you asked for.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Activity, Candidate, Track, is_derivative, norm_title
from .downloader import downloader
from .taste import taste

log = get("queue")


@dataclass
class WorkItem:
    track: Track
    mode: str = "append"          # now | next | append
    alternates: list[str] | None = None
    announce: bool = False


class QueueManager:
    def __init__(self, mpv, context_builder) -> None:
        self.mpv = mpv
        self.context = context_builder
        self._work: deque[WorkItem] = deque()
        self._pool: list[Candidate] = []
        self._meta: dict[str, Track] = {}        # file path -> Track
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._session_plays = 0
        # Names already queued or being downloaded. Claimed the moment a
        # candidate is taken, not when its download finishes — otherwise a
        # refill in that window happily adds the same song again.
        self._claimed: set[str] = set()
        self._hold_radio = False                 # album/artist runs pure first
        self._undo: deque[tuple] = deque(maxlen=20)
        self._workers: list[threading.Thread] = []
        self._activity = Activity()
        self._last_pos = -1

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        n = max(1, int(config.get("download_workers", 2)))
        for i in range(n):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True,
                                 name=f"dl-{i}")
            t.start()
            self._workers.append(t)
        threading.Thread(target=self._maintain, daemon=True, name="queue").start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- activity ------------------------------------------------------
    def _set_activity(self, stage: str, detail: str = "", progress: float = 0.0) -> None:
        self._activity = Activity(stage=stage, detail=detail, progress=progress)
        bus.publish(Ev.ACTIVITY, self._activity.to_dict())

    @property
    def activity(self) -> Activity:
        return self._activity

    # -- public API ----------------------------------------------------
    def play_now(self, tracks: list[Track], alternates: list[str] | None = None,
                 *, shuffle: bool = False, hold_radio: bool = False) -> None:
        """Replace what's playing with these tracks."""
        import random
        tracks = [t for t in tracks if t.video_id or t.url]
        if not tracks:
            return
        if shuffle:
            random.shuffle(tracks)
        with self._lock:
            self._work.clear()
            self._pool.clear()
            self._claimed.clear()
            self._hold_radio = hold_radio
            self._work.append(WorkItem(tracks[0], mode="now", alternates=alternates))
            for t in tracks[1:]:
                self._work.append(WorkItem(t, mode="append"))
        self._set_activity("finding", tracks[0].title)
        self._wake.set()

    def play_next(self, track: Track) -> None:
        with self._lock:
            self._work.appendleft(WorkItem(track, mode="next"))
        self._wake.set()

    def enqueue(self, tracks: list[Track]) -> None:
        with self._lock:
            for t in tracks:
                self._work.append(WorkItem(t, mode="append"))
        self._wake.set()

    def cancel(self) -> dict:
        """Abandon whatever we're fetching and stop chasing it."""
        with self._lock:
            dropped = len(self._work)
            self._work.clear()
        killed = downloader.cancel_all()
        self._set_activity("idle")
        return {"ok": True, "cancelled": killed, "dropped": dropped,
                "message": "Stopped" if (killed or dropped) else "Nothing to stop"}

    def release_hold(self) -> None:
        self._hold_radio = False
        self._wake.set()

    # -- worker --------------------------------------------------------
    def _worker(self, idx: int) -> None:
        while not self._stop.is_set():
            item = self._take_work()
            if item is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            try:
                self._process(item)
            except Exception as exc:
                log.exception("worker %s failed: %s", idx, exc)

    def _take_work(self) -> WorkItem | None:
        """Explicit requests first; otherwise top up from the candidate pool."""
        with self._lock:
            if self._work:
                return self._work.popleft()
        if self._hold_radio:
            return None
        if self.ready_ahead() >= self.target_depth():
            return None
        cand = self._take_candidate()
        if cand is None:
            return None
        return WorkItem(cand.track, mode="append")

    def _take_candidate(self) -> Candidate | None:
        now = time.monotonic()
        recent_artists = self._tail_artists(int(config.get("artist_run_limit", 3)))
        with self._lock:
            usable = [c for c in self._pool if c.can_try(now)]
            if not usable:
                return None
            # Highest score wins, but skip anything that would extend an
            # artist run past the cap — unless that's all we have.
            usable.sort(key=lambda c: c.score, reverse=True)
            usable = [c for c in usable if c.track.key() not in self._claimed]
            if not usable:
                return None
            pick = None
            if recent_artists and len(set(recent_artists)) == 1:
                blocked = recent_artists[0]
                pick = next((c for c in usable
                             if c.track.primary_artist() != blocked), None)
            if pick is None:
                pick = usable[0]
            self._pool.remove(pick)
            key = pick.track.key()
            if key:
                self._claimed.add(key)
            return pick

    def _process(self, item: WorkItem) -> None:
        track = item.track
        if item.mode in ("now", "next"):
            self._set_activity("finding", f"{track.title}")

        def progress(frac: float) -> None:
            self._set_activity("downloading", track.title, frac)

        path = downloader.fetch_with_fallbacks(track, item.alternates,
                                               on_progress=progress)
        if not path:
            if item.mode == "now":
                # The headline track died; promote the next thing we have.
                bus.publish(Ev.TOAST, f"Couldn't get {track.title}")
                self._set_activity("idle")
                nxt = None
                with self._lock:
                    if self._work:
                        nxt = self._work.popleft()
                        nxt.mode = "now"
                if nxt:
                    self._process(nxt)
            return

        track.path = path
        with self._lock:
            self._meta[path] = track
            if track.key():
                self._claimed.add(track.key())
        taste.mark_queued(track)

        if item.mode == "now":
            self._set_activity("loading", track.title)
            self.mpv.command("playlist-clear", wait=False)
            self.mpv.command("loadfile", path, "replace", wait=False)
            self.mpv.set("pause", False)
            self._set_activity("playing", f"{track.title} — {track.artist}")
        elif item.mode == "next":
            pos = self.mpv.get("playlist-pos", 0) or 0
            count = self.mpv.get("playlist-count", 0) or 0
            self.mpv.command("loadfile", path, "append", wait=False)
            if count > pos + 1:
                self.mpv.command("playlist-move", count, pos + 1, wait=False)
        else:
            self.mpv.command("loadfile", path, "append", wait=False)

        self.publish_queue()

    # -- depth / maintenance -------------------------------------------
    def target_depth(self) -> int:
        base = int(config.get("queue_target", 12))
        cap = int(config.get("queue_max", 30))
        return max(1, min(cap, base + min(12, self._session_plays)))

    def ready_ahead(self) -> int:
        pos = self.mpv.get("playlist-pos", None)
        count = self.mpv.get("playlist-count", 0) or 0
        if pos is None or pos < 0:
            return max(0, count)
        return max(0, count - pos - 1)

    def _tail_artists(self, n: int) -> list[str]:
        """Primary artists of the last n ready tracks, newest last."""
        try:
            pl = self.mpv.command("get_property", "playlist") or []
        except Exception:
            return []
        artists = []
        for entry in pl[-n:]:
            tr = self._meta.get(entry.get("filename", ""))
            if tr:
                artists.append(tr.primary_artist())
        return [a for a in artists if a]

    def _maintain(self) -> None:
        """Keep the pool stocked and the ready buffer deep enough."""
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                self._track_progress()
                if self._hold_radio:
                    if self.ready_ahead() <= 0:
                        self._hold_radio = False
                    continue
                need_ready = self.ready_ahead() < self.target_depth()
                pool_low = len(self._pool) < int(config.get("queue_pool_min", 20))
                if pool_low and self.mpv.get("playlist-count", 0):
                    self._refill_pool()
                if need_ready:
                    self._wake.set()
                if self.ready_ahead() < int(config.get("queue_min_ready", 3)):
                    self._wake.set()
            except Exception as exc:
                log.debug("maintain: %s", exc)

    def _refill_pool(self) -> None:
        ids, keys = self._exclusions()
        try:
            cands = self.context.build(self.current_track(), exclude=ids,
                                       exclude_keys=keys)
        except Exception as exc:
            log.warning("context build failed: %s", exc)
            return
        if not cands:
            return
        with self._lock:
            have = {c.track.video_id for c in self._pool}
            keys = {c.track.key() for c in self._pool}
            for c in cands:
                if c.track.video_id in have or c.track.key() in keys:
                    continue
                self._pool.append(c)
                have.add(c.track.video_id)
                keys.add(c.track.key())
        log.info("pool now %d candidates", len(self._pool))

    def _exclusions(self) -> tuple[set[str], set[str]]:
        """(video ids, normalized names) that must not come back."""
        ids = set(taste.history_ids())
        with self._lock:
            ids |= {t.video_id for t in self._meta.values() if t.video_id}
            ids |= {c.track.video_id for c in self._pool}
            keys = set(self._claimed)
            keys |= {t.key() for t in self._meta.values() if t.key()}
            keys |= {c.track.key() for c in self._pool if c.track.key()}
        return ids, keys

    # -- playback tracking ---------------------------------------------
    def _track_progress(self) -> None:
        pos = self.mpv.get("playlist-pos", None)
        if pos is None:
            return
        if self._last_pos >= 0 and pos > self._last_pos:
            self._session_plays += pos - self._last_pos
        self._last_pos = pos

    def current_track(self) -> Track | None:
        path = self.mpv.get("path", "")
        return self._meta.get(path) if path else None

    def track_for(self, path: str) -> Track | None:
        return self._meta.get(path)

    # -- queue view + edits ---------------------------------------------
    def snapshot(self) -> list[dict]:
        try:
            pl = self.mpv.command("get_property", "playlist") or []
        except Exception:
            return []
        out = []
        for i, entry in enumerate(pl):
            path = entry.get("filename", "")
            tr = self._meta.get(path)
            out.append({
                "index": i,
                "current": bool(entry.get("current")),
                "title": tr.title if tr else path.rsplit("\\", 1)[-1],
                "artist": tr.artist if tr else "",
                "art": tr.art if tr else "",
                "video_id": tr.video_id if tr else "",
                "origin": tr.origin if tr else "",
            })
        return out

    def publish_queue(self) -> None:
        bus.publish(Ev.QUEUE, self.snapshot())

    def move(self, frm: int, to: int) -> bool:
        self._undo.append(("move", to, frm))
        self.mpv.command("playlist-move", int(frm), int(to), wait=False)
        self.publish_queue()
        return True

    def remove(self, index: int) -> bool:
        snap = self.snapshot()
        if 0 <= index < len(snap):
            self._undo.append(("remove", index, snap[index]))
        self.mpv.command("playlist-remove", int(index), wait=False)
        self.publish_queue()
        return True

    def jump(self, index: int) -> bool:
        self.mpv.set("playlist-pos", int(index))
        self.mpv.set("pause", False)
        self.publish_queue()
        return True

    def undo(self) -> str:
        """Undo the last queue edit. Returns a human-readable result."""
        if not self._undo:
            return "Nothing to undo"
        op = self._undo.pop()
        if op[0] == "move":
            self.mpv.command("playlist-move", int(op[1]), int(op[2]), wait=False)
            self.publish_queue()
            return "Move undone"
        if op[0] == "remove":
            row = op[2]
            tr = Track(video_id=row.get("video_id", ""), title=row.get("title", ""),
                       artist=row.get("artist", ""), art=row.get("art", ""))
            self.play_next(tr)
            return f"Restoring {tr.title}"
        return "Nothing to undo"

    def clear_radio(self) -> None:
        with self._lock:
            self._pool.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "pool": len(self._pool),
                "work": len(self._work),
                "ready_ahead": self.ready_ahead(),
                "target": self.target_depth(),
                "session_plays": self._session_plays,
                "hold_radio": self._hold_radio,
            }
