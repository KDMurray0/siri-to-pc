"""The queue.

    context -> candidate pool -> download workers -> mpv playlist

Ideas and files are separate. Only downloaded tracks reach mpv, so a failed
download costs a candidate instead of killing the whole refill.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Activity, Candidate, Track
from . import radio, spectrum
from .downloader import downloader
from ..resolve import catalog as catalog_cache
from .taste import taste

log = get("queue")

_PRUNE_EVERY = 30 * 60      # seconds


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
        # claimed when a candidate is taken, not when it finishes downloading
        self._claimed: set[str] = set()
        self._hold_radio = False                 # album/artist runs pure first
        self._request_kind = "song"              # what you last asked for
        self._anchor: Track | None = None        # the song that started it off
        self._theme = ""                         # genre/vibe asked for, if any
        self._end_after_run = False              # artist/album: stop, don't drift
        self._queue_stamp = None                 # so we only push real changes
        self._activity_mark = None               # last progress we bothered sending
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
        # yt-dlp reports progress far more often than a 76px bar can show it.
        # Send it when the stage or the song changes, otherwise only when the
        # bar would actually move.
        step = int(progress * 50)          # 2% of the bar
        mark = (stage, detail, step)
        if mark == self._activity_mark:
            return
        self._activity_mark = mark
        bus.publish(Ev.ACTIVITY, self._activity.to_dict())

    @property
    def activity(self) -> Activity:
        return self._activity

    # -- public API ----------------------------------------------------
    def play_now(self, tracks: list[Track], alternates: list[str] | None = None,
                 *, shuffle: bool = False, hold_radio: bool = False,
                 kind: str = "song", theme: str = "") -> None:
        """Replace what's playing with these tracks."""
        import random
        tracks = [t for t in tracks if t.video_id or t.url]
        if not tracks:
            return
        if shuffle or config.get("shuffle"):
            random.shuffle(tracks)
        with self._lock:
            self._work.clear()
            self._pool.clear()
            self._claimed.clear()
            self._hold_radio = hold_radio
            self._request_kind = kind
            self._anchor = tracks[0]
            # ask for grunge and the whole hour should be grunge, not just the
            # first 25 tracks before the radio wanders off somewhere else
            self._theme = (theme or "").strip()
            # Ask for an artist or an album and that's what you asked for.
            # When it runs out the queue ends rather than sliding into a radio
            # you didn't request.
            self._end_after_run = kind in ("artist", "album")
            if not radio.is_station(tracks[0]):
                radio.now_playing.stop()
            for t in tracks:
                t.reason = t.reason or "asked"
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

    def shuffle_upcoming(self) -> None:
        """Reorder what's still to come, leaving the current track alone."""
        self.mpv.command("playlist-shuffle", wait=False)
        self.publish_queue()

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
        if config.get("repeat", "off") != "off":
            return None          # repeating a set list: don't keep growing it
        # Enough music queued, by time and by count.
        if self.minutes_ahead() >= self.target_minutes():
            return None
        if self.ready_ahead() >= self.target_depth():
            return None
        cand = self._take_candidate()
        if cand is None:
            return None
        cand.track.reason = cand.reason      # so the queue can say why
        return WorkItem(cand.track, mode="append")

    def _play_station(self, track: Track) -> None:
        """Put a live station on. No queue, no radio afterwards — it's on
        until you ask for something else."""
        with self._lock:
            self._work.clear()
            self._pool.clear()
            self._hold_radio = True
            self._end_after_run = True
            self._meta[track.url] = track
        self.mpv.command("loadfile", track.url, "replace", wait=False)
        self.mpv.set("pause", False)
        radio.now_playing.start(self.mpv, track.title)
        self._set_activity("idle")
        log.info("tuned to %s", track.title)
        self.publish_queue(force=True)

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
            if config.get("shuffle"):
                # shuffle mode: pick from the good ones rather than the best one
                import random as _r
                _r.shuffle(usable[:12])
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

        if radio.is_station(track):
            # a live stream has nothing to fetch; hand mpv the url
            self._play_station(track)
            return

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
        spectrum.ensure(path)          # visualiser data, ready before it plays
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
    def target_minutes(self) -> float:
        base = float(config.get("queue_minutes", 30))
        cap = float(config.get("queue_minutes_max", 60))
        # grows a little the longer you listen
        return min(cap, base + min(20.0, self._session_plays * 1.5))

    def minutes_ahead(self) -> float:
        """How much music is actually queued up, in minutes."""
        try:
            pl = self.mpv.command("get_property", "playlist") or []
            pos = self.mpv.get("playlist-pos", 0) or 0
        except Exception:
            return 0.0
        total = 0.0
        for entry in pl[pos + 1:]:
            tr = self._meta.get(entry.get("filename", ""))
            total += (tr.duration if tr and tr.duration else 210)   # ~3.5 min guess
        return total / 60.0

    def target_depth(self) -> int:
        """Song-count equivalent, still used as a hard ceiling."""
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
        next_prune = time.monotonic() + _PRUNE_EVERY
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                self._track_progress()
                # The cache was only ever pruned at startup, and this thing
                # is meant to be left running — a few megabytes a track,
                # playing all day, is about a gigabyte a day of downloads
                # nobody deletes until the next reboot.
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + _PRUNE_EVERY
                    gone = downloader.prune_cache(keep=set(self._meta))
                    if gone:
                        log.info("pruned %d cached files", gone)
                    catalog_cache.save_cache()
                if self._hold_radio:
                    if self.ready_ahead() <= 0 and not self._end_after_run:
                        # the run is done; whatever follows is not an artist
                        # request and shouldn't behave like one
                        self._hold_radio = False
                        if self._request_kind != "genre":
                            self._request_kind = "song"
                    continue
                need_ready = (self.minutes_ahead() < self.target_minutes()
                              and self.ready_ahead() < self.target_depth())
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
        # Ask for a band and you get that band. Ask for a song and the radio
        # should be allowed to wander.
        focus = 1.0 if self._request_kind in ("artist", "album", "playlist") else 0.4
        try:
            cands = self.context.build(self.current_track(), exclude=ids,
                                       exclude_keys=keys, focus=focus,
                                       anchor=self._anchor, theme=self._theme,
                                       artist_counts=self._recent_artists())
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

    def _recent_artists(self, look_back: int = 30) -> dict[str, int]:
        """Who this session has been leaning on lately."""
        counts: dict[str, int] = {}
        for row in taste.recent(limit=look_back):
            # same normalisation the ranker uses, or the tally never matches
            a = Track(title="", artist=row.get("artist") or "").primary_artist()
            if a:
                # capped: this should push a band down the list, not ban them
                counts[a] = min(counts.get(a, 0) + 1, 3)
        return counts

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
                "reason": tr.reason if tr else "",
            })
        return out

    def publish_queue(self, *, force: bool = False) -> None:
        """Send the queue out, but only when it's actually different.

        The monitor calls this every second. The queue changes maybe once a
        song, so re-sending an identical 4KB of json 60 times a minute to every
        phone and every open tab was most of the traffic this app produced.
        """
        snap = self.snapshot()
        stamp = hash(tuple((r["index"], r["video_id"], r["current"]) for r in snap))
        if not force and stamp == self._queue_stamp:
            return
        self._queue_stamp = stamp
        bus.publish(Ev.QUEUE, snap)

    def move(self, frm: int, to: int) -> bool:
        """Put the track at `frm` at position `to`.

        mpv's playlist-move takes the entry to insert *before*, so dragging a
        track downwards needs one added or it lands a slot short.
        """
        frm, to = int(frm), int(to)
        if frm == to:
            return True
        self._undo.append(("move", to, frm))
        self.mpv.command("playlist-move", frm, to + 1 if to > frm else to,
                         wait=False)
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

    def stats(self) -> dict:
        with self._lock:
            return {
                "pool": len(self._pool),
                "work": len(self._work),
                "ready_ahead": self.ready_ahead(),
                "minutes_ahead": round(self.minutes_ahead(), 1),
                "target_minutes": round(self.target_minutes(), 1),
                "target": self.target_depth(),
                "session_plays": self._session_plays,
                "hold_radio": self._hold_radio,
            }
