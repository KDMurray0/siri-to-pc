"""What you actually like, from what you sit through.

30% listened counts as played. Play-throughs pull an artist up, skips push
them down, likes count for more.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import defaultdict

from ..config import config, state_file
from ..paths import write_atomic
from ..logging_setup import get
from ..models import Track, norm_title

log = get("taste")


class TasteEngine:
    """What somebody has listened to, and what that says about them.

    `root` is whose. The owner's is the data directory; a guest with a
    permanent link gets their own folder, so their evening teaches their
    radio and nothing else's. Left unset it's the owner's, which is what
    every existing caller means.
    """

    def __init__(self, root=None) -> None:
        self._root = root
        self._lock = threading.RLock()
        self._liked: list[dict] = []
        self._blocked_songs: dict[str, dict] = {}
        self._blocked_artists: set[str] = set()
        self._song: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # id -> [plays, skips]
        self._artist: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self._history: list[str] = []          # completed video ids, newest last
        self._recent_meta: list[dict] = []     # for the Recent panel
        self._played_at: dict[str, float] = {}  # norm title -> last play time
        self._dirty = False
        self._last_save = 0.0
        self._fatigue = 0.0                    # late skips: bored of this run
        self._load()

    # -- persistence ---------------------------------------------------
    def _file(self, name: str, create: bool = False):
        if self._root is None:
            return state_file(name)
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / name

    def _load(self) -> None:
        try:
            d = json.loads(self._file("play_stats.json").read_text("utf-8-sig"))
            self._song.update({k: list(v) for k, v in d.get("songs", {}).items()})
            self._artist.update({k: list(v) for k, v in d.get("artists", {}).items()})
            self._history = d.get("history", [])[-config.get("history_size", 200):]
            self._recent_meta = d.get("recent", [])[-100:]
            self._played_at = d.get("played_at", {})
        except Exception:
            pass
        try:
            self._liked = json.loads(self._file("liked_songs.json").read_text("utf-8-sig"))
        except Exception:
            self._liked = []
        try:
            raw = json.loads(self._file("blocked_music.json").read_text("utf-8-sig"))
            self._blocked_songs = dict(raw.get("songs") or {})
            self._blocked_artists = set(raw.get("artists") or [])
        except Exception:
            self._blocked_songs, self._blocked_artists = {}, set()

    def _prune(self) -> None:
        """Drop the play times we can no longer act on.

        played_at only exists to answer "did this play recently", and recently
        means dedupe_hours. Entries older than that are dead weight, but it was
        keeping one per track forever and writing the lot to disk on every
        song change.
        """
        window = float(config.get("dedupe_hours", 12)) * 3600
        cutoff = time.time() - max(window * 2, 86400)
        if len(self._played_at) > 400:
            self._played_at = {k: v for k, v in self._played_at.items() if v > cutoff}

    def save(self) -> None:
        with self._lock:
            self._prune()
            try:
                write_atomic(self._file("play_stats.json"), json.dumps({
                    "songs": self._song, "artists": self._artist,
                    "history": self._history[-config.get("history_size", 200):],
                    "recent": self._recent_meta[-100:],
                    "played_at": self._played_at,
                }))
            except Exception as exc:
                log.debug("stats save failed: %s", exc)
            self._dirty = False
            self._last_save = time.monotonic()

    def save_soon(self) -> None:
        """Write at most every few seconds; a skipped track shouldn't cost a
        full rewrite of the stats file."""
        with self._lock:
            self._dirty = True
            if time.monotonic() - self._last_save < 20:
                return
        self.save()

    def flush(self) -> None:
        """Write anything save_soon() deferred.

        save_soon() sets the dirty flag and returns without writing if it
        wrote recently, on the understanding that somebody comes back for it.
        For the owner's store the player does, on shutdown. A guest's store
        had nobody: everything recorded in the twenty seconds before they
        stopped listening — usually the last track, often all of them — stayed
        in memory until the session was dropped, and then went with it.
        """
        with self._lock:
            if not self._dirty:
                return
        self.save()

    def _save_liked(self) -> None:
        try:
            self._file("liked_songs.json").write_text(
                json.dumps(self._liked[-500:]), encoding="utf-8")
        except Exception as exc:
            # Silence here loses everything you've ever liked, and the only
            # sign is that the list is short next time you look.
            log.warning("couldn't save your liked songs: %s", exc)

    # -- recording -----------------------------------------------------
    def seed_from(self, artists: list[str]) -> int:
        """Give the ranker a head start from somebody's listening history.

        Taste is deliberately bounded to a tiebreaker, so this is a light
        thumb on the scale and not a verdict — one play each, which is worth
        0.1 and caps out at 0.4. Enough to break a tie between two records
        that fit equally well; nowhere near enough to choose the queue.
        """
        added = 0
        with self._lock:
            for name in artists:
                key = Track(artist=name, title="").primary_artist()
                if not key or key in self._artist:
                    continue
                self._artist[key] = [1, 0]
                added += 1
            if added:
                self._dirty = True
        return added

    def unskip(self, track: Track) -> None:
        """Take back the last thing a skip taught us.

        A mis-hit on the media key otherwise teaches the ranker something
        false, permanently and invisibly — the counts only ever go up.
        """
        if not track:
            return
        vid, artist = track.video_id, track.primary_artist()
        with self._lock:
            if vid and vid in self._song and self._song[vid][1] > 0:
                self._song[vid][1] -= 1
            if artist and artist in self._artist and self._artist[artist][1] > 0:
                self._artist[artist][1] -= 1
            self._fatigue = max(0.0, self._fatigue - 0.5)
            self._dirty = True

    def record(self, track: Track, position: float, duration: float) -> bool:
        """Log a finished/abandoned track. Returns True if it counted as played."""
        if not track or not duration:
            return False
        ratio = position / duration if duration else 0
        completed = ratio >= float(config.get("completion_ratio", 0.30))
        vid = track.video_id
        artist = track.primary_artist()
        # Only what you asked for shapes what you like. Left alone for six
        # hours the radio drifts, and if autoplay counted as taste that drift
        # would bake itself in — an artist you never chose ends up a favourite
        # and gets pushed at you tomorrow. Skips still count either way: those
        # are you telling it no, and it should hear that wherever it came from.
        asked_for = track.origin in ("request", "playlist", "library")
        learns = asked_for or not config.get("taste_from_requests", True)
        counts = learns or not completed        # a skip is a no wherever it came from

        # Where you skipped says what you meant. Bailing in the first few
        # seconds is "not this song" — the track is wrong, the direction is
        # fine. Sitting through most of it and then skipping is "enough of
        # this", which is about the run rather than the song, so it counts
        # against the track much more gently.
        early = not completed and ratio < 0.10
        with self._lock:
            if vid and counts:
                self._song[vid][0 if completed else 1] += 1
            if artist and counts:
                # only a quick skip is held against the band
                if completed or early:
                    self._artist[artist][0 if completed else 1] += 1
            if not completed:
                self._fatigue = min(3.0, self._fatigue + (0.15 if early else 0.5))
            else:
                self._fatigue = max(0.0, self._fatigue - 0.35)
            if completed and vid:
                if vid in self._history:
                    self._history.remove(vid)
                self._history.append(vid)
                self._history = self._history[-config.get("history_size", 200):]
                self._recent_meta = [m for m in self._recent_meta
                                     if m.get("video_id") != vid]
                self._recent_meta.append({
                    "video_id": vid, "title": track.title, "artist": track.artist,
                    "art": track.art, "at": time.time()})
            key = norm_title(track.title, track.artist)
            if key:
                self._played_at[key] = time.time()
        self.save_soon()
        return completed

    def mark_queued(self, track: Track) -> None:
        """Remember that we've already lined this song up (name-based dedupe)."""
        key = norm_title(track.title, track.artist)
        if key:
            with self._lock:
                self._played_at[key] = time.time()

    def fatigue(self) -> float:
        """How much the current run is being sat through, 0 to 3.

        Late skips push it up, finished tracks pull it down. The pool widens
        when it's high — you're not rejecting songs, you're rejecting the
        direction.
        """
        with self._lock:
            return self._fatigue

    def recently_used(self, track: Track) -> bool:
        key = norm_title(track.title, track.artist)
        if not key:
            return False
        window = float(config.get("dedupe_hours", 12)) * 3600
        with self._lock:
            last = self._played_at.get(key, 0)
        return (time.time() - last) < window

    # -- likes ---------------------------------------------------------
    def is_liked(self, video_id: str) -> bool:
        return any(t.get("video_id") == video_id for t in self._liked)

    def toggle_like(self, track: Track) -> bool:
        with self._lock:
            existing = next((t for t in self._liked
                             if t.get("video_id") == track.video_id), None)
            if existing:
                self._liked.remove(existing)
                liked = False
            else:
                self._liked.append({
                    "video_id": track.video_id, "title": track.title,
                    "artist": track.artist, "album": track.album, "art": track.art})
                liked = True
        self._save_liked()
        return liked

    # -- never again -----------------------------------------------------
    def is_blocked(self, track: Track | None) -> bool:
        """Told, in so many words, not to play this.

        Different from a skip, which is a nudge the scoring weighs up
        against everything else. This is an answer, and the ranker doesn't
        get a vote.
        """
        if not track:
            return False
        with self._lock:
            if track.video_id and track.video_id in self._blocked_songs:
                return True
            who = track.primary_artist()
            return bool(who) and who in self._blocked_artists

    def block(self, track: Track | None = None, artist: str = "",
              on: bool = True) -> bool:
        """Block or unblock a recording, or everything by somebody."""
        changed = False
        with self._lock:
            if artist:
                who = Track(title="", artist=artist).primary_artist()
                if who:
                    if on and who not in self._blocked_artists:
                        self._blocked_artists.add(who); changed = True
                    elif not on and who in self._blocked_artists:
                        self._blocked_artists.discard(who); changed = True
            elif track and track.video_id:
                vid = track.video_id
                if on and vid not in self._blocked_songs:
                    self._blocked_songs[vid] = {
                        "video_id": vid, "title": track.title,
                        "artist": track.artist, "art": track.art}
                    changed = True
                elif not on and vid in self._blocked_songs:
                    self._blocked_songs.pop(vid, None); changed = True
        if changed:
            self._save_blocks()
            # Blocking something you're being played is a request to stop
            # hearing it, so it stops counting for anything as well.
            if track and track.video_id:
                self._drop_ids({track.video_id})
        return changed

    def blocks(self) -> dict:
        with self._lock:
            return {"songs": list(self._blocked_songs.values()),
                    "artists": sorted(self._blocked_artists)}

    def _save_blocks(self) -> None:
        try:
            with self._lock:
                data = {"songs": self._blocked_songs,
                        "artists": sorted(self._blocked_artists)}
            self._file("blocked_music.json", create=True).write_text(
                json.dumps(data), encoding="utf-8")
        except Exception as exc:
            log.warning("couldn't save what you blocked: %s", exc)

    def liked(self) -> list[dict]:
        return list(self._liked)

    def liked_ids(self) -> set[str]:
        return {t.get("video_id") for t in self._liked if t.get("video_id")}

    def play_counts(self) -> dict[str, int]:
        """video id -> times played through. What the cache keeps by."""
        with self._lock:
            return {vid: int(v[0]) for vid, v in self._song.items()
                    if v and len(v) > 0 and v[0]}

    def liked_seed(self) -> str | None:
        with self._lock:
            if not self._liked:
                return None
            return random.choice(self._liked).get("video_id")

    # -- reads ---------------------------------------------------------
    def history_ids(self) -> list[str]:
        with self._lock:
            return list(self._history)

    def recent(self, limit: int = 40) -> list[dict]:
        with self._lock:
            return list(reversed(self._recent_meta[-limit:]))

    def top_artists(self, limit: int = 8) -> list[dict]:
        with self._lock:
            ranked = sorted(self._artist.items(),
                            key=lambda kv: kv[1][0] - kv[1][1], reverse=True)
        return [{"artist": a, "plays": s[0]} for a, s in ranked[:limit] if s[0] > 0]

    def forget(self, video_id: str = "", artist: str = "") -> bool:
        """Take something out of the history, and out of the scoring with it.

        Dropping the row and keeping the tally would be a lie: the list
        stops mentioning them while they carry on being pushed at you, and
        the only visible effect of the button is that the evidence goes
        away. So the count goes too, and with an artist so do their tracks —
        a chip that disappears above eight rows by the same band doesn't
        look like anything has happened.

        Not their likes. Liking something is a separate thing said
        deliberately, and it isn't this button's to undo.
        """
        gone = False
        with self._lock:
            if video_id:
                gone = self._drop_ids({video_id}) or gone
            if artist:
                who = Track(title="", artist=artist).primary_artist()
                if who and self._artist.pop(who, None) is not None:
                    gone = True
                if who:
                    theirs = {m.get("video_id") for m in self._recent_meta
                              if Track(title="", artist=m.get("artist") or "")
                              .primary_artist() == who}
                    gone = self._drop_ids({v for v in theirs if v}) or gone
        if gone:
            self.save()
        return gone

    def _drop_ids(self, ids: set[str]) -> bool:
        """Forget these recordings. Caller holds the lock."""
        if not ids:
            return False
        before = len(self._recent_meta), len(self._history), len(self._song)
        # played_at is keyed by name, so the names have to come off the rows
        # before the rows go — otherwise dedupe keeps refusing to play again
        # something you have just asked it to forget.
        for m in self._recent_meta:
            if m.get("video_id") in ids:
                key = norm_title(m.get("title") or "", m.get("artist") or "")
                if key:
                    self._played_at.pop(key, None)
        self._recent_meta = [m for m in self._recent_meta
                             if m.get("video_id") not in ids]
        self._history = [v for v in self._history if v not in ids]
        for vid in ids:
            self._song.pop(vid, None)
        return before != (len(self._recent_meta), len(self._history),
                          len(self._song))

    def preferred_artists(self) -> list[str]:
        """Artists to bias search toward when a title is ambiguous."""
        out = {(t.get("artist") or "").split(",")[0].strip().lower()
               for t in self._liked}
        with self._lock:
            for artist, (plays, skips) in self._artist.items():
                if plays >= 2 and plays >= 2 * skips:
                    out.add(artist)
        return [a for a in out if a]

    # -- scoring -------------------------------------------------------
    def score(self, track: Track) -> float:
        """How much you like this, between -1 and 1.

        It used to be unbounded: half a point per play meant an artist you'd
        played twenty times scored +10, against a genre term that maxes out
        around 3. Liking something once was enough to drag it into every
        queue it didn't belong in. It's a tiebreaker, so it's bounded like one.
        """
        artist = track.primary_artist()
        s = 0.0
        with self._lock:
            liked_artists = {(t.get("artist") or "").split(",")[0].strip().lower()
                             for t in self._liked}
            if artist and artist in liked_artists:
                s += 0.4
            plays, skips = self._artist.get(artist, [0, 0])
            s += min(0.4, 0.1 * plays) - min(0.8, 0.2 * skips)
            sp, ss = self._song.get(track.video_id, [0, 0])
            s += min(0.3, 0.1 * sp) - min(0.9, 0.3 * ss)
        return max(-1.0, min(1.0, s))


class NeutralTaste:
    """A taste store for somebody who isn't you.

    Reads flat and swallows writes. A guest's queue still gets context, tags,
    era and everything else that describes *music* — what it doesn't get is
    your listening history pushing its scores around, and what you don't get
    is their evening rewriting your radio for a fortnight afterwards.

    Same surface as TasteEngine, so nothing above it knows which one it holds.
    """

    def score(self, track) -> float:
        return 0.0

    def recent(self, limit: int = 40) -> list:
        return []

    def history_ids(self) -> list:
        return []

    def liked_seed(self):
        return None

    def liked(self) -> list:
        return []

    def liked_ids(self) -> set:
        return set()

    def play_counts(self) -> dict:
        return {}

    def top_artists(self, limit: int = 8) -> list:
        return []

    def preferred_artists(self) -> list:
        return []

    def is_liked(self, video_id: str) -> bool:
        return False

    def recently_used(self, track) -> bool:
        return False

    def fatigue(self) -> float:
        return 0.0

    # -- everything that would write --
    def record(self, *a, **k) -> bool:
        return False

    def mark_queued(self, *a, **k) -> None:
        pass

    def unskip(self, *a, **k) -> None:
        pass

    def toggle_like(self, *a, **k) -> bool:
        return False

    def seed_from(self, *a, **k) -> int:
        return 0

    def save(self, *a, **k) -> None:
        pass

    def save_soon(self, *a, **k) -> None:
        pass

    def flush(self, *a, **k) -> None:
        pass

    def forget(self, *a, **k) -> bool:
        return False

    def is_blocked(self, *a, **k) -> bool:
        return False

    def block(self, *a, **k) -> bool:
        return False

    def blocks(self, *a, **k) -> dict:
        return {"songs": [], "artists": []}


taste = TasteEngine()
