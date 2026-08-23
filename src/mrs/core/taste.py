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
from ..logging_setup import get
from ..models import Track, norm_title

log = get("taste")


class TasteEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._liked: list[dict] = []
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
    def _load(self) -> None:
        try:
            d = json.loads(state_file("play_stats.json").read_text("utf-8-sig"))
            self._song.update({k: list(v) for k, v in d.get("songs", {}).items()})
            self._artist.update({k: list(v) for k, v in d.get("artists", {}).items()})
            self._history = d.get("history", [])[-config.get("history_size", 200):]
            self._recent_meta = d.get("recent", [])[-100:]
            self._played_at = d.get("played_at", {})
        except Exception:
            pass
        try:
            self._liked = json.loads(state_file("liked_songs.json").read_text("utf-8-sig"))
        except Exception:
            self._liked = []

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
                state_file("play_stats.json").write_text(json.dumps({
                    "songs": self._song, "artists": self._artist,
                    "history": self._history[-config.get("history_size", 200):],
                    "recent": self._recent_meta[-100:],
                    "played_at": self._played_at,
                }), encoding="utf-8")
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

    def _save_liked(self) -> None:
        try:
            state_file("liked_songs.json").write_text(
                json.dumps(self._liked[-500:]), encoding="utf-8")
        except Exception:
            pass

    # -- recording -----------------------------------------------------
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

    def liked(self) -> list[dict]:
        return list(self._liked)

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


taste = TasteEngine()
