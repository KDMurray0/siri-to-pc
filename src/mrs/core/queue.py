"""The queue.

    context -> candidate pool -> download workers -> mpv playlist

Ideas and files are separate. Only downloaded tracks reach mpv, so a failed
download costs a candidate instead of killing the whole refill.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get, spawn
from ..models import Activity, Candidate, Track
from . import radio, spectrum
from .downloader import downloader, lanes
from .sink import MpvSink
from ..resolve import catalog as catalog_cache
from .taste import taste as _default_taste

log = get("queue")

_PRUNE_EVERY = 30 * 60      # seconds — flushing the search cache
_CHECK_EVERY = 60           # seconds — holding the download cache to its limit
# Songs around the playing one that shuffle won't drop anything into.
SHUFFLE_BUFFER = 3

# Every file any queue has ever named, so any queue can name it.
#
# _meta is per-queue, which is right for ownership and wrong for naming: the
# downloader collapses concurrent fetches of the same id, tracks move between
# the shared player and a session on a handoff, and a queue can be handed a
# file it never fetched. Every one of those showed up in the list as
# "bTE8texJH7g.webm", because the only fallback was the filename.
_KNOWN: dict[str, Track] = {}
# The same tracks again, keyed by video id. Paths are a fragile key: pinning
# copies a file somewhere else, a cache move renames it, and mpv is free to
# hand a playlist entry back in a form that isn't character-for-character
# what it was given. The id survives all of that, and it's in the filename.
_KNOWN_ID: dict[str, Track] = {}
_KNOWN_LOCK = threading.Lock()
_KNOWN_MAX = 4000


def remember(path: str, track: Track) -> None:
    if not path or not track:
        return
    with _KNOWN_LOCK:
        _KNOWN[path] = track
        if track.video_id:
            _KNOWN_ID[track.video_id] = track
        if len(_KNOWN) > _KNOWN_MAX:
            for old in list(_KNOWN)[:len(_KNOWN) - _KNOWN_MAX]:
                _KNOWN.pop(old, None)
        if len(_KNOWN_ID) > _KNOWN_MAX:
            for old in list(_KNOWN_ID)[:len(_KNOWN_ID) - _KNOWN_MAX]:
                _KNOWN_ID.pop(old, None)


def recall(path: str) -> Track | None:
    """Whatever we know about this file, by path and then by id."""
    with _KNOWN_LOCK:
        got = _KNOWN.get(path)
        if got:
            return got
    vid = _vid_from(path)
    if not vid:
        return None
    with _KNOWN_LOCK:
        return _KNOWN_ID.get(vid)


def _vid_from(path: str) -> str:
    """The video id a cache file is named after, if it is one."""
    stem = path.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    return stem if 8 <= len(stem) <= 24 and "%" not in stem else ""


def _label(path: str) -> str:
    """Something a person would accept when we genuinely don't know.

    A row saying "Loading…" is honest and disappears on the next publish. A
    row saying "bTE8texJH7g.webm" is a bug report.
    """
    if not path:
        return "Loading…"
    if path.startswith("http"):
        return "Live stream"
    return "Loading…"


def _cached_first(tracks: list[Track]) -> list[Track]:
    """Already-downloaded ones to the front, keeping the order they came in.

    Shuffling a forty-track album and then waiting on a download for the one
    that happened to land first is a wait for nothing when thirty of them are
    already on disk. Called only after the list has been shuffled, so each
    group is already in random order and this just moves the ready ones up —
    still random, and it starts immediately.

    Nothing is dropped: the rest follow, and by the time they're reached
    they've had the whole first stretch to download.
    """
    have, missing = [], []
    for t in tracks:
        ready = t.video_id and downloader.cached(t.video_id)
        (have if ready else missing).append(t)
    return have + missing


@dataclass
class WorkItem:
    track: Track
    mode: str = "append"          # now | next | append
    alternates: list[str] | None = None
    announce: bool = False


class QueueManager:
    def __init__(self, sink, context_builder, taste=None, session_id: str = "",
                 workers: int = 0, cap: int = 0, max_minutes: float = 0) -> None:
        # Whatever is going to play these — mpv for the owner, a plain list
        # for a guest's browser. The engine above it doesn't care which.
        self.sink = sink
        self.context = context_builder
        # Handed in so a guest is scored by, and teaches, a neutral store.
        self.taste = taste if taste is not None else _default_taste
        # Stamped onto every event this queue publishes, so the stream can be
        # filtered per listener instead of shouting everything at everybody.
        self.session_id = session_id
        self._worker_count = workers
        self._cap = cap                     # 0 = the owner's, uncapped
        # Somebody else's player. Their queue must not be steered by the
        # owner's toggles: the owner turning shuffle on was scattering every
        # guest's running order, and the owner setting repeat starved every
        # guest's queue outright, because a repeating set list stops growing.
        self._solo = bool(session_id)
        # A hard ceiling on how far ahead to build, in minutes. 0 = none.
        self._max_minutes = float(max_minutes or 0)
        self._request_times: deque[float] = deque(maxlen=200)
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
        # the records that started it off — usually one, more when the
        # request named more than one thing
        self._anchors: list[Track] = []
        # the last thing skipped, so a mis-hit can be taken back
        self._last_skipped: tuple[Track, float] | None = None
        self._refilling = False
        self._theme = ""                         # genre/vibe asked for, if any
        self._end_after_run = False              # artist/album: stop, don't drift
        self._queue_stamp = None                 # so we only push real changes
        self._activity_mark = None               # last progress we bothered sending
        self._undo: deque[tuple] = deque(maxlen=20)
        self._workers: list[threading.Thread] = []
        self._activity = Activity()
        self._last_pos = -1
        # Bumped whenever what's wanted changes. A worker captures it when it
        # takes a job and checks it again before touching the playlist, so a
        # download that was already in flight when you asked for something
        # else can't land in the queue you asked for instead.
        self._era = 0
        # The last running order we saw, kept off the sink so a crash can't
        # take it away. See snapshot() and restore_order().
        self._last_order: list[str] = []
        self._last_index = 0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        n = self._worker_count or max(1, int(config.get("download_workers", 2)))
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
        bus.publish(Ev.ACTIVITY, dict(self._activity.to_dict(),
                                      session=self.session_id))

    @property
    def activity(self) -> Activity:
        return self._activity

    # -- public API ----------------------------------------------------
    def play_now(self, tracks: list[Track], alternates: list[str] | None = None,
                 *, shuffle: bool = False, hold_radio: bool = False,
                 kind: str = "song", theme: str = "",
                 anchors: list[Track] | None = None) -> None:
        """Replace what's playing with these tracks."""
        tracks = [t for t in tracks if t.video_id or t.url]
        if not tracks:
            return
        if config.get("party_mode") and self.sink.count():
            # In party mode nobody replaces what's on — not even by asking
            # for it outright. It goes on the end like everyone else's, and
            # the owner is still free to drag it wherever they like.
            self.enqueue(tracks)
            return
        if shuffle or self._pref_shuffle():
            random.shuffle(tracks)
            tracks = _cached_first(tracks)
        with self._lock:
            self._work.clear()
            self._pool.clear()
            self._claimed.clear()
            # This is the other way a request gets replaced: not cancelled,
            # just superseded by the next "play X". Anything a worker is
            # already holding belongs to the old one.
            self._era += 1
            self._hold_radio = hold_radio
            self._request_kind = kind
            self._anchors = [a for a in (anchors or []) if a] or [tracks[0]]
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
        if config.get("party_mode"):
            self.enqueue([track])       # no queue-jumping while the party's on
            return
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
            self._era += 1        # nothing older than this may reach the sink
        # Only this queue's. Everybody else is still listening to theirs.
        killed = downloader.cancel_all(self.session_id)
        self._set_activity("idle")
        return {"ok": True, "cancelled": killed, "dropped": dropped,
                "message": "Stopped" if (killed or dropped) else "Nothing to stop"}

    def shuffle_upcoming(self) -> None:
        """Reorder what's still to come, and only that.

        Not playlist-shuffle: that shuffles the entire list, played entries
        included, so tracks you hadn't heard yet got dealt behind the playing
        one and vanished into the history. Only the entries after the current
        position move, one playlist-move at a time.
        """
        try:
            pl = self.sink.playlist()
            pos = self.sink.pos()
        except Exception:
            return
        if pos is None or pos < 0:
            pos = -1
        head = pos + 1
        live = [e.get("filename") for e in pl[head:]]

        # The ones still waiting to be downloaded, first — start a fifty-track
        # playlist and only three are on disk yet, so shuffling just those
        # looked like the button did nothing while the other forty-seven sat
        # in their original order. This has to happen before the early return
        # below, which is exactly the case that hits it.
        with self._lock:
            fixed = [w for w in self._work if w.mode != "append"]
            rest = [w for w in self._work if w.mode == "append"]
            if len(rest) > 1:
                random.shuffle(rest)
                self._work.clear()
                self._work.extend(fixed + rest)

        if len(live) < 2:
            self.publish_queue()
            return
        want = live[:]
        random.shuffle(want)
        # Selection sort: put the right file in each slot in turn, tracking
        # where everything has moved to as we go.
        for slot, name in enumerate(want):
            k = live.index(name, slot)
            if k != slot:
                self.sink.move(head + k, head + slot)
                live.insert(slot, live.pop(k))
        self.publish_queue()

    def _scatter_new(self, path: str) -> None:
        """With shuffle on, drop a freshly appended track somewhere random.

        Never within the next few songs: those are already downloaded, already
        on screen, and about to play. Landing a new arrival in the middle of
        them is what makes playback jump.
        """
        try:
            pl = self.sink.playlist()
            pos = self.sink.pos()
        except Exception:
            return
        last = len(pl) - 1
        if pos is None or last < 0:
            return
        floor = pos + 1 + SHUFFLE_BUFFER
        if floor >= last:
            return                      # not enough runway to move it into
        self.sink.move(last, random.randint(floor, last))

    def release_hold(self) -> None:
        self._hold_radio = False
        self._wake.set()

    # -- worker --------------------------------------------------------
    def _worker(self, idx: int) -> None:
        # Everything this thread downloads belongs to this queue, so a stop
        # only stops its own.
        downloader.claim_lane(self.session_id)
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
        if config.get("party_mode"):
            # Nothing but what people actually asked for. The radio filling
            # gaps is the right behaviour on an ordinary evening and exactly
            # wrong when six people are queueing things.
            return None
        if self._pref_repeat() != "off":
            return None          # repeating a set list: don't keep growing it
        # How much is queued is a question about time, not track count — an
        # album of two-minute songs shouldn't run dry sooner than one of
        # six-minute ones. The count is only a runaway guard.
        if self.minutes_ahead() >= self.target_minutes():
            return None
        if self.ready_ahead() >= self.hard_cap():
            return None
        if self._cap and self.sink.count() >= self._cap:
            return None          # a guest gets a full queue, not a boundless one
        cand = self._take_candidate()
        if cand is None:
            return None
        cand.track.reason = cand.reason      # so the queue can say why
        return WorkItem(cand.track, mode="append")

    def adopt(self, track: Track) -> bool:
        """Play a track this machine already has, without touching the workers.

        A handoff between the speakers and a phone is moving a file that is by
        definition already on disk — it was coming out of something a second
        ago. Sending it back round resolve-then-download made changing rooms
        take as long as asking for the song from scratch, which is most of why
        switching output felt broken rather than slow.

        Returns False when the file isn't here after all, so the caller can
        fall back to the ordinary path.
        """
        path = track.path or (downloader.cached(track.video_id)
                              if track.video_id else "")
        if not path:
            return False
        track.path = path
        with self._lock:
            self._work.clear()
            self._meta[path] = track
            self._anchors = [track]
            self._request_kind = "song"
            self._hold_radio = False
            self._end_after_run = False
            if track.key():
                self._claimed.add(track.key())
        remember(path, track)
        self.sink.clear()
        self.sink.load(path, "replace")
        self.sink.set_paused(False)
        self._set_activity("playing", f"{track.title} — {track.artist}")
        self.publish_queue(force=True)
        self._wake.set()          # and start building what comes after it
        log.info("adopted %s — %s", track.title, track.artist)
        return True

    def _play_station(self, track: Track) -> None:
        """Put a live station on. No queue, no radio afterwards — it's on
        until you ask for something else."""
        with self._lock:
            self._work.clear()
            self._pool.clear()
            self._hold_radio = True
            self._end_after_run = True
            self._meta[track.url] = track
        remember(track.url, track)
        self.sink.load(track.url, "replace")
        self.sink.set_paused(False)
        # Station metadata is polled out of mpv, so it only exists on the
        # speaker sink. A browser session plays the stream and simply doesn't
        # get the "what's on right now" line — better than pretending.
        if getattr(self.sink, "mpv", None):
            radio.now_playing.start(self.sink.mpv, track.title)
        self._set_activity("idle")
        log.info("tuned to %s", track.title)
        self.publish_queue(force=True)

    def note_skip(self, track: Track | None, position: float = 0.0) -> None:
        if track and track.video_id:
            self._last_skipped = (track, position)

    def unskip(self) -> dict:
        """Put the last skipped track back on, and unlearn the skip."""
        got = self._last_skipped
        if not got:
            return {"ok": False, "message": "Nothing to bring back"}
        track, _pos = got
        self._last_skipped = None
        self.taste.unskip(track)
        # Straight to the front, ahead of whatever the skip promoted.
        with self._lock:
            self._work.appendleft(WorkItem(track, mode="next"))
        self._wake.set()
        return {"ok": True, "message": f"Back to {track.title}",
                "title": track.title, "artist": track.artist}

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
            if self._pref_shuffle():
                # shuffle mode: pick from the good ones rather than the best
                # one. usable[:12] is a copy, so shuffling that shuffled
                # nothing and shuffle mode played the same order as normal.
                head = usable[:12]
                random.shuffle(head)
                usable[:len(head)] = head
            usable = [c for c in usable if c.track.key() not in self._claimed]
            if not usable:
                return None

            pick = None
            # Two tracks by one act back to back isn't the mild same-artist
            # bias that's wanted, it's a run. So prefer somebody who hasn't
            # been on for a few records — but not every time. A rule that
            # never bends is its own kind of wrong: sometimes the best thing
            # to play next really is the same band again.
            gap = int(config.get("artist_gap", 4))
            slip = float(config.get("artist_gap_slip", 0.15))
            near = {a for a in self._tail_artists(gap)}
            if near and random.random() >= slip:
                pick = next((c for c in usable
                             if c.track.primary_artist() not in near), None)

            # And a hard stop on an actual run, which bends for nobody.
            if pick is None and recent_artists and len(set(recent_artists)) == 1:
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
        # What was being asked for when this job was picked up. Clearing the
        # work list stops jobs that haven't started; this is what stops the
        # one already downloading — or, worse, already downloaded, because a
        # cached track goes from "taken" to "in the playlist" with no pause
        # at all, which is how a track from the search you'd abandoned still
        # turned up in the results of the one you replaced it with.
        era = self._era
        if item.mode in ("now", "next"):
            self._set_activity("finding", f"{track.title}")

        def progress(frac: float) -> None:
            self._set_activity("downloading", track.title, frac)

        if radio.is_station(track):
            # a live stream has nothing to fetch; hand mpv the url
            self._play_station(track)
            return

        # The owner's lane drains first; a guest waits behind it rather than
        # racing it. Released in the finally so a failed fetch can't wedge
        # everybody else out.
        mine = not self.session_id
        lanes.enter(mine)
        try:
            path = downloader.fetch_with_fallbacks(track, item.alternates,
                                                   on_progress=progress)
        finally:
            lanes.leave(mine)
        if not path:
            if item.mode == "now":
                # The headline track died; promote the next thing we have.
                msg = f"Couldn't get {track.title}"
                bus.publish(Ev.TOAST,
                            {"text": msg, "session": self.session_id}
                            if self.session_id else msg)
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
        # And in the registry every queue reads, so a handoff or a collapsed
        # concurrent fetch can still put a name on it.
        remember(path, track)

        # The moment of truth. Everything above is harmless — a file on disk
        # and a name in a dictionary — but from here we change what plays, and
        # if the request that wanted this has been replaced, changing it is
        # the bug. A cached track reaches this point in milliseconds, which is
        # why clearing the work list alone never closed the window.
        if era != self._era:
            log.info("dropped %s — a newer request replaced it", track.title)
            with self._lock:
                if track.key():
                    self._claimed.discard(track.key())
            return

        self.taste.mark_queued(track)

        if item.mode == "now":
            self._set_activity("loading", track.title)
            self.sink.clear()
            self.sink.load(path, "replace")
            self.sink.set_paused(False)
            self._set_activity("playing", f"{track.title} — {track.artist}")
        elif item.mode == "next":
            pos = self.sink.pos() or 0
            count = self.sink.count()
            self.sink.load(path, "append")
            if count > pos + 1:
                self.sink.move(count, pos + 1)
        else:
            self.sink.load(path, "append")
            if self._pref_shuffle():
                # Shuffle on means the order shouldn't be arrival order. Deal
                # it in somewhere random rather than always on the end.
                self._scatter_new(path)

        self.publish_queue()

    # -- depth / maintenance -------------------------------------------
    def target_minutes(self) -> float:
        base = float(config.get("queue_minutes", 30))
        cap = float(config.get("queue_minutes_max", 60))
        # grows a little the longer you listen
        want = min(cap, base + min(20.0, self._session_plays * 1.5))
        # Somebody playing on their own phone gets a much shorter buffer.
        # Every track ahead of them is fetched, transcoded and pushed over
        # the network to a device that may leave the building; half an hour
        # of that is a gigabyte nobody hears. Doesn't apply to a phone being
        # used as a remote — that's the PC's queue, which has none of the cost.
        if self._max_minutes:
            want = min(want, self._max_minutes)
        return want

    def minutes_ahead(self) -> float:
        """How much music is actually queued up, in minutes."""
        try:
            pl = self.sink.playlist()
            pos = self.sink.pos() or 0
        except Exception:
            return 0.0
        total = 0.0
        for entry in pl[pos + 1:]:
            tr = self._meta.get(entry.get("filename", ""))
            total += (tr.duration if tr and tr.duration else 210)   # ~3.5 min guess
        return total / 60.0

    def target_depth(self) -> int:
        """Song-count equivalent. Advisory — how much to queue is decided by
        target_minutes; this only keeps a run of 90-second tracks from
        queueing forty of them."""
        base = int(config.get("queue_target", 12))
        cap = int(config.get("queue_max", 30))
        return max(1, min(cap, base + min(12, self._session_plays)))

    def hard_cap(self) -> int:
        """The runaway guard. Duration decides; this stops absurdity."""
        return max(2, int(config.get("queue_max", 30)))

    def ready_ahead(self) -> int:
        pos = self.sink.pos()
        count = self.sink.count()
        if pos is None or pos < 0:
            return max(0, count)
        return max(0, count - pos - 1)

    def _tail_artists(self, n: int) -> list[str]:
        """Primary artists of the last n ready tracks, newest last."""
        try:
            pl = self.sink.playlist()
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
        next_check = time.monotonic() + _CHECK_EVERY
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                self._track_progress()
                # The cache was only ever pruned at startup, and this thing
                # is meant to be left running — a few megabytes a track,
                # playing all day, is about a gigabyte a day of downloads
                # nobody deletes until the next reboot.
                #
                # Checked every minute rather than every half hour: a limit
                # that's only enforced twice an hour is a limit you watch
                # yourself go over. prune_cache returns immediately when
                # there's nothing to do, so this costs one stat sweep.
                if time.monotonic() >= next_check and not self.session_id:
                    next_check = time.monotonic() + _CHECK_EVERY
                    from .session import sessions
                    sessions.reap()
                    # Everyone's pins, not just this queue's — a guest's
                    # next track is as untouchable as the owner's.
                    keep = self.keep_paths()
                    if not self.session_id:
                        from .session import sessions
                        keep |= sessions.keep_paths()
                    gone = downloader.prune_cache(keep=keep)
                    if gone:
                        log.info("pruned %d cached files", gone)
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + _PRUNE_EVERY
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
                              and self.ready_ahead() < self.hard_cap())
                pool_low = len(self._pool) < int(config.get("queue_pool_min", 20))
                if (pool_low and not self._refilling
                        and self.sink.count()):
                    # Off the loop, not in it. Working out what could play
                    # next means eighty seconds of talking to four services
                    # on a cold cache, and this loop is also what moves the
                    # progress bar, starts the crossfade and publishes
                    # status — all of which stopped dead for the duration.
                    self._refilling = True
                    spawn(self._refill_and_release, name="pool refill")
                if need_ready:
                    self._wake.set()
                if self.ready_ahead() < int(config.get("queue_min_ready", 3)):
                    self._wake.set()
            except Exception as exc:
                log.debug("maintain: %s", exc)

    def _refill_and_release(self) -> None:
        try:
            self._refill_pool()
        finally:
            self._refilling = False
            # A worker that ran dry while this was in flight is sitting on
            # the event, not polling the pool.
            self._wake.set()

    def _refill_pool(self) -> None:
        ids, keys = self._exclusions()
        # Cold pool: put something playable in it now rather than after a
        # minute of network calls. Ranked candidates land on top of these.
        if not self._pool:
            try:
                fast = self.context.quick(self.current_track(), exclude=ids,
                                          exclude_keys=keys, limit=6)
            except Exception as exc:
                log.debug("quick prefill failed: %s", exc)
                fast = []
            if fast:
                with self._lock:
                    self._pool.extend(fast)
                log.info("prefilled %d while the real build runs", len(fast))
                self._wake.set()
                ids, keys = self._exclusions()
        # Ask for a band and you get that band. Ask for a song and the radio
        # should be allowed to wander.
        focus = 1.0 if self._request_kind in ("artist", "album", "playlist") else 0.4
        try:
            cands = self.context.build(self.current_track(), exclude=ids,
                                       exclude_keys=keys, focus=focus,
                                       anchor=self._anchors, theme=self._theme,
                                       artist_counts=self._recent_artists(),
                                       queued_titles=self.queued_titles())
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

    def note_request(self) -> None:
        self._request_times.append(time.time())

    def recent_requests(self) -> int:
        """How hard this listener has been asking, over the last hour."""
        cutoff = time.time() - 3600
        return sum(1 for t in self._request_times if t > cutoff)

    def _pref_shuffle(self) -> bool:
        """Shuffle is the owner's switch and lives on the owner's player."""
        return False if self._solo else bool(config.get("shuffle"))

    def _pref_repeat(self) -> str:
        return "off" if self._solo else str(config.get("repeat", "off"))

    def oldest_request(self) -> float:
        """When the hour's allowance started running, so a refusal can say
        how long the wait is rather than just "no"."""
        cutoff = time.time() - 3600
        recent = [t for t in self._request_times if t > cutoff]
        return recent[0] if recent else time.time()

    def queued_titles(self) -> list[str]:
        """Song titles already in the running order, however they're credited.

        History alone doesn't cover this: a track queued two minutes ago
        hasn't been played yet, so nothing has recorded it, and the radio was
        free to line up the same song again under a different artist credit.
        """
        with self._lock:
            names = [t.title for t in self._meta.values() if t and t.title]
        return names[-120:]

    def keep_paths(self) -> set[str]:
        """Files the queue still needs, so pruning doesn't take them."""
        with self._lock:
            return set(self._meta)

    def _recent_artists(self, look_back: int = 30) -> dict[str, int]:
        """Who this session has been leaning on lately."""
        counts: dict[str, int] = {}
        for row in self.taste.recent(limit=look_back):
            # same normalisation the ranker uses, or the tally never matches
            a = Track(title="", artist=row.get("artist") or "").primary_artist()
            if a:
                # capped: this should push a band down the list, not ban them
                counts[a] = min(counts.get(a, 0) + 1, 3)
        return counts

    def _exclusions(self) -> tuple[set[str], set[str]]:
        """(video ids, normalized names) that must not come back."""
        ids = set(self.taste.history_ids())
        with self._lock:
            ids |= {t.video_id for t in self._meta.values() if t.video_id}
            ids |= {c.track.video_id for c in self._pool}
            keys = set(self._claimed)
            keys |= {t.key() for t in self._meta.values() if t.key()}
            keys |= {c.track.key() for c in self._pool if c.track.key()}
        return ids, keys

    # -- playback tracking ---------------------------------------------
    def _track_progress(self) -> None:
        pos = self.sink.pos()
        if pos is None:
            return
        if self._last_pos >= 0 and pos > self._last_pos:
            self._session_plays += pos - self._last_pos
        self._last_pos = pos

    def current_track(self) -> Track | None:
        path = self.sink.path()
        return self._meta.get(path) if path else None

    def track_for(self, path: str) -> Track | None:
        return self._meta.get(path)

    # -- queue view + edits ---------------------------------------------
    def snapshot(self) -> list[dict]:
        try:
            pl = self.sink.playlist()
        except Exception:
            return []
        out = []
        for i, entry in enumerate(pl):
            path = entry.get("filename", "")
            # Mine first, then anyone's, by path and then by the id in the
            # filename. Falling straight through put "bTE8texJH7g.webm" in
            # the list; falling through to "Loading…" is better but still a
            # row nobody can read, so say so in the log when it happens.
            tr = self._meta.get(path) or recall(path)
            if tr is None and path:
                log.debug("no metadata for queue entry %s", path)
            out.append({
                "index": i,
                "current": bool(entry.get("current")),
                "title": (tr.title if tr and tr.title else "") or _label(path),
                "artist": tr.artist if tr else "",
                "art": tr.art if tr else "",
                "video_id": tr.video_id if tr else _vid_from(path),
                "origin": tr.origin if tr else "",
                "reason": tr.reason if tr else "",
            })
        # Keep the running order somewhere the sink can't take it with it.
        # A crashed mpv comes back as a fresh process with an empty playlist,
        # and this is the only copy of what was in the old one. Free: the
        # monitor already reads the playlist once a second to publish it.
        if pl:
            with self._lock:
                self._last_order = [r["path"] for r in
                                    ({"path": e.get("filename", "")} for e in pl)
                                    if r["path"]]
                here = next((i for i, e in enumerate(pl) if e.get("current")), None)
                if here is not None:
                    self._last_index = here
        return out

    def restore_order(self, *, resume: str = "", drop: str = "") -> dict:
        """Put a crashed player's running order back.

        A fresh mpv starts with an empty playlist, so everything queued went
        with the process — and the refill loop wouldn't rebuild it, because it
        only tops up a queue that already has something in it. One bad file
        therefore didn't just stop a song, it ended the evening.

        `drop` leaves a file out: the one that has now taken the player down
        twice. `resume` is where to pick up.
        """
        import os

        with self._lock:
            paths = [p for p in self._last_order if p and p != drop]
            idx = self._last_index
        # Anything pruned out from under us while it was down.
        paths = [p for p in paths if p.startswith("http") or os.path.isfile(p)]
        if not paths:
            return {"restored": 0, "resumed": ""}
        if resume and resume in paths:
            idx = paths.index(resume)
        idx = max(0, min(idx, len(paths) - 1))

        self.sink.clear()
        for i, p in enumerate(paths):
            self.sink.load(p, "replace" if i == 0 else "append")
        self.sink.jump(idx)
        self.sink.set_paused(False)
        self.publish_queue(force=True)
        tr = self._meta.get(paths[idx]) or recall(paths[idx])
        log.info("put %d tracks back after the player restarted", len(paths))
        return {"restored": len(paths),
                "resumed": f"{tr.title} — {tr.artist}" if tr else ""}

    def forget_file(self, path: str) -> None:
        """Bin a file that keeps killing the player.

        Almost always a truncated or malformed download rather than anything
        wrong with the song, so the file goes and the track stays askable —
        request it again and it fetches cleanly.
        """
        import os

        if not path or path.startswith("http"):
            return
        with self._lock:
            tr = self._meta.pop(path, None)
            if tr and tr.key():
                self._claimed.discard(tr.key())
            self._last_order = [p for p in self._last_order if p != path]
        with _KNOWN_LOCK:
            _KNOWN.pop(path, None)
        try:
            os.remove(path)
            log.warning("deleted %s — it stopped the player twice", path)
        except OSError as exc:
            log.debug("couldn't delete %s: %s", path, exc)

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
        bus.publish(Ev.QUEUE, {"rows": snap, "session": self.session_id}
                    if self.session_id else snap)

    def move(self, frm: int, to: int) -> bool:
        """Put the track at `frm` at position `to`.

        mpv's playlist-move takes the entry to insert *before*, so dragging a
        track downwards needs one added or it lands a slot short.
        """
        frm, to = int(frm), int(to)
        if frm == to:
            return True
        self._undo.append(("move", to, frm))
        self.sink.move(frm, to + 1 if to > frm else to)
        self.publish_queue()
        return True

    def remove(self, index: int) -> bool:
        snap = self.snapshot()
        if 0 <= index < len(snap):
            self._undo.append(("remove", index, snap[index]))
        self.sink.remove(int(index))
        self.publish_queue()
        return True

    def jump(self, index: int) -> bool:
        self.sink.jump(int(index))
        self.sink.set_paused(False)
        self.publish_queue()
        return True

    def undo(self) -> str:
        """Undo the last queue edit. Returns a human-readable result."""
        if not self._undo:
            return "Nothing to undo"
        op = self._undo.pop()
        if op[0] == "move":
            self.sink.move(int(op[1]), int(op[2]))
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
