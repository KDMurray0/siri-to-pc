"""One listener, one queue.

The owner's session is the machine itself: mpv, the speakers, the taste it
has been learning for months. A guest's is a browser holding a list — same
engine above it, but its own queue, its own history, and a taste store that
reads flat so their evening doesn't rewrite your radio.

Sessions are keyed by pass, not by device. Michael opening his link on a
phone and a laptop is one Michael with one queue, which is what he'd expect;
Michael and Sarah are two people who never see each other.

A session is created the first time a pass actually asks for something, and
dropped after a stretch of silence — nothing closes a browser tab politely,
so waiting to be told would mean keeping every session ever opened.
"""

from __future__ import annotations

import threading
import time

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get
from ..resolve import catalog
from .context import ContextBuilder
from .sink import ListSink
from .taste import NeutralTaste

log = get("session")

IDLE_DEATH = 6 * 3600          # silence after which a session is let go
GUEST_QUEUE_CAP = 40           # tracks one guest may pin at once


class Session:
    """A guest's player: their queue, their sink, nothing of yours."""

    def __init__(self, pass_id: str, name: str, scope: str) -> None:
        from .queue import QueueManager      # imported late; queue imports sink

        self.id = pass_id
        self.name = name or "guest"
        self.scope = scope or "full"
        self.sink = ListSink()
        # A neutral taste store, and its own context builder — sharing the
        # owner's would score this queue by the owner's history.
        self.queue = QueueManager(self.sink,
                                  ContextBuilder(catalog, taste=NeutralTaste()),
                                  taste=NeutralTaste(), session_id=pass_id,
                                  # Two, like the owner's. One meant a guest
                                  # fetched strictly one track at a time and
                                  # spent the whole evening saying
                                  # "downloading"; the shared ceiling in
                                  # downloader.lanes is what actually protects
                                  # the machine, not starving them here.
                                  workers=2, cap=GUEST_QUEUE_CAP,
                                  max_minutes=float(
                                      config.get("cast_queue_minutes", 10)))
        self.created = time.time()
        self.last_seen = time.time()
        # Seconds, as last reported by the client, and which track it was
        # talking about. Stamped because an unstamped number is worse than
        # none: the next track inherited the last one's position and the
        # browser dutifully seeked into the middle of it.
        self.position = 0.0
        self._pos_vid = ""
        self.plays = 0             # tracks this guest has finished
        self.listened = 0.0        # seconds of audio, for the owner's list
        self._dropped = False      # their connection went away and we paused
        self._started = False
        self._stop = threading.Event()

    def start(self) -> None:
        if not self._started:
            self._started = True
            self.queue.start()
            threading.Thread(target=self._beat, daemon=True,
                             name=f"session {self.name}").start()

    def _beat(self) -> None:
        """A heartbeat, because nothing else is going to send one.

        The shared player publishes its status from mpv's monitor loop. A
        browser session has no mpv and so had no heartbeat at all — the queue
        arrived, the song appeared in the list, and the page never learned
        there was anything to play. That is the whole of "it pulls up a song
        and then does nothing".
        """
        last, quiet = None, 0
        while not self._stop.wait(1.0):
            try:
                now = self.status()
                # Only when it actually changes: this goes to a phone, and a
                # kilobyte a second of identical json is somebody's data. But
                # never silent for long either — a dropped event would
                # otherwise strand the client until the next track.
                mark = (now["state"], now["playlist_pos"], now["playlist_count"],
                        now["track"].get("video_id", ""))
                # The track moved under us — a skip, a jump, the queue
                # rearranged. Drop the old clock before anyone reads it.
                if last and mark[3] != last[3]:
                    self.rewound()
                    now = self.status()
                quiet += 1
                if mark != last or quiet >= 5:
                    last, quiet = mark, 0
                    bus.publish(Ev.STATUS, now)
            except Exception as exc:
                log.debug("%s heartbeat: %s", self.name, exc)

    def touch(self) -> None:
        self.last_seen = time.time()
        # Back after a drop: pick up exactly where they were paused, rather
        # than leaving them staring at a stopped player until they press
        # something.
        if self._dropped:
            self._dropped = False
            self.sink.set_paused(False)
            log.info("%r is back — resuming", self.name)

    def quiet_for(self) -> float:
        return time.time() - self.last_seen

    def check_alive(self) -> str:
        """Pause a guest who has gone off the network, and say what happened.

        A browser that loses its connection stops sending anything at all —
        it does not get to say goodbye. Without this the session carried on
        advancing through a queue nobody could hear, burning downloads and
        showing in the owner's list as somebody actively listening.
        """
        quiet = self.quiet_for()
        if quiet > float(config.get("guest_quiet_close", 900)):
            return "close"
        if quiet > float(config.get("guest_quiet_pause", 45)):
            if not self._dropped:
                self._dropped = True
                self.sink.set_paused(True)
                log.info("%r went quiet after %ds — paused", self.name, int(quiet))
                bus.publish(Ev.STATUS, self.status())
            return "paused"
        return "live"

    def vid(self) -> str:
        track = self.current()
        return track.video_id if track else ""

    def mark_position(self, pos: float) -> float:
        """Take the client's clock. Returns how much it moved, in seconds.

        The browser owns the time here — there is no mpv to ask — so this is
        the only place the number comes from, and it is only believed about
        the track that is actually playing.
        """
        pos = max(0.0, float(pos))
        now = self.vid()
        moved = pos - self.position if now == self._pos_vid else 0.0
        self._pos_vid, self.position = now, pos
        # Only forward motion counts: a seek backwards isn't negative listening.
        if 0 < moved < 120:
            self.listened += moved
        return max(0.0, moved)

    def rewound(self) -> None:
        """A new track started, so the old position means nothing."""
        self.position = 0.0
        self._pos_vid = ""

    def stop(self) -> None:
        self._stop.set()
        try:
            self.queue.stop()
        except Exception as exc:
            log.debug("stopping %s: %s", self.name, exc)

    # -- what the client and the owner's guest list both want --------------
    def current(self):
        return self.queue.track_for(self.sink.path())

    def status(self) -> dict:
        track = self.current()
        pos = self.sink.pos()
        playing = bool(track) and not self.sink.paused
        vid = track.video_id if track else ""
        return {
            "session": self.id,          # stamped, so the stream routes it
            "state": "playing" if playing else ("paused" if track else "idle"),
            # Only ever about the track it was measured on. The client is the
            # clock; this is an echo, and a stale echo is what had every phone
            # seeking back to zero every few seconds.
            "position": self.position if vid and vid == self._pos_vid else 0.0,
            "playlist_pos": pos,
            "playlist_count": self.sink.count(),
            "volume": 100,             # the phone's own, not ours to set
            "shuffle": False,
            "repeat": "off",
            "crossfade": 0,
            "activity": self.queue.activity.to_dict(),
            "timer": None,
            "track": {
                "name": track.title if track else "",
                "artist": track.artist if track else "",
                "album": track.album if track else "",
                "art": track.art if track else "",
                "video_id": track.video_id if track else "",
                "duration": (track.duration if track else 0) or 0,
                "live": False, "song_known": False, "liked": False,
            } if track else {"name": "", "artist": "", "art": "", "duration": 0,
                             "live": False, "song_known": False},
        }

    def describe(self) -> dict:
        """A row in the owner's list of who's listening."""
        track = self.current()
        quiet = time.time() - self.last_seen
        return {
            "id": self.id, "name": self.name, "scope": self.scope,
            "playing": f"{track.title} — {track.artist}" if track else "",
            "artist": track.artist if track else "",
            "title": track.title if track else "",
            "art": track.art if track else "",
            "position": round(self.position),
            "duration": (track.duration if track else 0) or 0,
            "queued": self.sink.count(),
            "ahead": max(0, self.sink.count() - (self.sink.pos() or 0) - 1),
            "idle_minutes": round(quiet / 60, 1),
            "active": quiet < 120,
            "dropped": self._dropped,
            "requests_hour": self.queue.recent_requests(),
            "plays": self.plays,
            "listened_minutes": round(self.listened / 60, 1),
            "since_minutes": round((time.time() - self.created) / 60),
        }


class Sessions:
    """Everyone currently listening who isn't you."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rooms: dict[str, Session] = {}

    def for_pass(self, pass_id: str, name: str = "", scope: str = "full") -> Session:
        with self._lock:
            room = self._rooms.get(pass_id)
            if room is None:
                room = Session(pass_id, name, scope)
                self._rooms[pass_id] = room
                log.info("opened a session for %r (%s)", room.name, room.scope)
        room.start()
        room.touch()
        return room

    def find(self, pass_id: str) -> Session | None:
        with self._lock:
            return self._rooms.get(pass_id)

    def close(self, pass_id: str) -> bool:
        with self._lock:
            room = self._rooms.pop(pass_id, None)
        if room:
            room.stop()
            log.info("closed the session for %r", room.name)
        return bool(room)

    def listing(self) -> list[dict]:
        with self._lock:
            rooms = list(self._rooms.values())
        return sorted((r.describe() for r in rooms),
                      key=lambda r: (not r["active"], r["name"].lower()))

    def keep_paths(self) -> set[str]:
        """Every file a guest still needs, so pruning doesn't take it."""
        out: set[str] = set()
        with self._lock:
            rooms = list(self._rooms.values())
        for r in rooms:
            try:
                out |= r.queue.keep_paths()
            except Exception as exc:
                # This is the list of files the cleaner must not delete. A
                # miss here doesn't fail loudly, it deletes somebody's next
                # track while they're listening to the one before it.
                log.warning("couldn't ask %s what it still needs: %s",
                            r.name, exc)
        return out

    def reap(self) -> int:
        """Pause whoever has dropped off, let go of whoever isn't coming back.

        Also drops sessions whose pass was revoked, so taking a link away
        stops the music that link is playing rather than only the next thing
        it asks for.
        """
        from ..web.security import list_passes

        alive = {p["id"] for p in list_passes()
                 if not p["revoked"] and not p["expired"]}
        now = time.time()
        with self._lock:
            rooms = list(self._rooms.items())
        dead = []
        for pid, room in rooms:
            if now - room.last_seen > IDLE_DEATH or pid not in alive:
                dead.append(pid)
                continue
            if room.check_alive() == "close":
                dead.append(pid)
        for pid in dead:
            self.close(pid)
        return len(dead)

    def watch(self) -> None:
        """Run reap on a timer of its own.

        It used to ride on the owner's queue maintenance loop, once a minute.
        A minute is far too coarse for "pause them when they walk out of
        range", and it doesn't run at all if the owner's queue is stopped.
        """
        def loop() -> None:
            while True:
                time.sleep(5.0)
                try:
                    self.reap()
                except Exception as exc:
                    log.debug("reap: %s", exc)

        threading.Thread(target=loop, daemon=True, name="sessions").start()


sessions = Sessions()
