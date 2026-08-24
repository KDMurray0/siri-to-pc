"""Config: one file, typed defaults, no BOM on write."""

from __future__ import annotations

import json
import os
import secrets
import threading
from typing import Any

from .paths import (config_path, data_dir, migrate_legacy_data,
                    write_atomic)

_lock = threading.RLock()

DEFAULTS: dict[str, Any] = {
    # server
    "host": "0.0.0.0",
    "port": 7420,          # 5000 is crowded
    "api_key": "",
    "lock_ips": False,
    "allowed_ips": [],

    # playback
    "volume": 70,
    "eq": "flat",
    "normalize": False,
    "crossfade": 0,
    "repeat": "off",          # off | all | one
    "shuffle": False,
    "announce": True,
    "tts_voice": "en-US-AriaNeural",
    "theme": "default",
    "audio_device": "auto",
    "audio_device_label": "",
    "listen_loopback": True,   # meter reads the real output

    # fetching
    "js_runtime": "node",
    # web_embedded gives audio-only opus and actually downloads; "tv" is dead
    "player_client": "web_embedded",
    "player_client_fallbacks": ["web", "mweb", ""],
    "cookies_file": "",
    "cookies_from_browser": "",
    "cookie_auto_refresh": True,
    "cookie_check_interval": 3600,
    "cookie_close_browser_optin": False,   # ask before closing a browser
    "cookie_browsers": "",                 # "" = auto-detect
    "unreadable_browsers": [],             # learned: cookies we can never decrypt
    "source": "youtube",
    "download_retries": 2,
    "download_workers": 3,      # parallel fetches; YouTube tolerates a few
    "download_timeout": 240,    # seconds before a single fetch is abandoned
    "min_duration": 60,       # reject sub-minute results (the "30 second version" bug)

    # queue
    # depth in minutes, not songs
    "queue_minutes": 30,
    "queue_minutes_max": 60,
    "queue_target": 12,       # fallback when durations are unknown
    "queue_max": 30,
    "queue_min_ready": 3,     # downloaded tracks that must sit ahead
    "queue_pool_min": 20,     # candidate ideas kept in reserve
    "artist_run_limit": 3,    # max consecutive tracks by one artist
    "artist_gap": 4,          # prefer not to repeat an artist within this many
    "artist_gap_slip": 0.15,  # ...but let it through this often anyway
    "artist_cohesion": 1.0,   # 0 = pure discovery, 2 = stay on the band
    "anchor_pull": 0.35,      # how much the song you asked for still counts
    "show_visualiser": True,
    "taste_from_requests": True,   # radio plays don't count as liking it
    "use_tags": True,         # Last.fm genre tags steer the radio
    "history_size": 200,
    "dedupe_hours": 12,

    # taste
    "completion_ratio": 0.30,

    # llm
    "groq_api_key": "",
    "groq_model": "openai/gpt-oss-20b",
    "use_groq": True,
    "groq_timeout": 4,

    # extras
    "lastfm_api_key": "",
    "lastfm_secret": "",
    "lastfm_session": "",
    "lastfm_token": "",
    "lastfm_user": "",
    "library_paths": [],
    "playlist_download": False,   # keep playlist audio on disk
    "artist_track_count": 60,
    "cast_peers": [],
    "cast_all": False,     # mirror every request to the peers
    "alarms": [],
    "start_on_boot": False,
}


class Config:
    """Dict-like config with defaults, atomic saves and change callbacks."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._path = config_path()
        self._subs: list = []
        self.load()

    # -- io --
    def load(self) -> None:
        with _lock:
            if not self._path.exists():
                migrate_legacy_data()
            raw = {}
            if self._path.exists():
                try:
                    # utf-8-sig: tolerate a BOM written by PowerShell
                    raw = json.loads(self._path.read_text(encoding="utf-8-sig") or "{}")
                except Exception as exc:  # keep running on a corrupt file
                    print(f"[config] unreadable ({exc}); using defaults")
                    raw = {}
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in raw.items() if v is not None})
            if not merged.get("api_key") or "CHANGE" in str(merged["api_key"]).upper():
                merged["api_key"] = secrets.token_urlsafe(24)
            self._data = merged
            self.save()

    def save(self) -> None:
        with _lock:
            # no BOM, and atomically: this file holds the api key and every
            # setting, and losing it to a torn write means a fresh setup
            write_atomic(self._path, json.dumps(self._data, indent=2))

    # -- access --
    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with _lock:
            old = self._data.get(key)
            self._data[key] = value
            if save:
                self.save()
        if old != value:
            self._notify(key, value)

    def update(self, values: dict[str, Any]) -> None:
        with _lock:
            self._data.update(values)
            self.save()
        for k, v in values.items():
            self._notify(k, v)

    def public(self) -> dict[str, Any]:
        """Everything except secrets — safe to hand to the UI."""
        hidden = {"api_key", "groq_api_key", "lastfm_secret", "lastfm_session"}
        out = {k: v for k, v in self._data.items() if k not in hidden}
        out["groq_set"] = bool(self._data.get("groq_api_key"))
        out["lastfm_set"] = bool(self._data.get("lastfm_session"))
        return out

    # -- change notifications --
    def subscribe(self, fn) -> None:
        self._subs.append(fn)

    def _notify(self, key: str, value: Any) -> None:
        for fn in list(self._subs):
            try:
                fn(key, value)
            except Exception:
                pass


config = Config()


def state_file(name: str):
    return data_dir() / name
