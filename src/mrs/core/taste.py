"""What you actually like, learned from what you sit through.

A song counts as "played" once you've heard `completion_ratio` of it (30% by
default); anything less is a skip. Play-throughs pull an artist up, skips push
them down, and liked songs count for more than either.
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

    def save(self) -> None:
        with self._lock:
            try:
                state_file("play_stats.json").write_text(json.dumps({
                    "songs": self._song, "artists": self._artist,
                    "history": self._history[-config.get("history_size", 200):],
                    "recent": self._recent_meta[-100:],
                    "played_at": self._played_at,
                }), encoding="utf-8")
            except Exception as exc:
                log.debug("stats save failed: %s", exc)

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
        with self._lock:
            if vid:
                self._song[vid][0 if completed else 1] += 1
            if artist:
                self._artist[artist][0 if completed else 1] += 1
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
        self.save()
        return completed

    def mark_queued(self, track: Track) -> None:
        """Remember that we've already lined this song up (name-based dedupe)."""
        key = norm_title(track.title, track.artist)
        if key:
            with self._lock:
                self._played_at[key] = time.time()

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
        """Taste-only component of a candidate's score."""
        artist = track.primary_artist()
        s = 0.0
        with self._lock:
            liked_artists = {(t.get("artist") or "").split(",")[0].strip().lower()
                             for t in self._liked}
            if artist and artist in liked_artists:
                s += float(config.get("liked_boost", 2.0))
            plays, skips = self._artist.get(artist, [0, 0])
            s += 0.5 * plays - float(config.get("skip_penalty", 0.8)) * skips
            sp, ss = self._song.get(track.video_id, [0, 0])
            s += 0.4 * sp - float(config.get("skip_penalty", 0.8)) * ss
        return s


taste = TasteEngine()
