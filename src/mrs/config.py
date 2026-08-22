"""Config: one file, typed defaults, safe writes.

Reads tolerate a BOM (PowerShell loves writing them); writes never emit one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from typing import Any

from .paths import config_path, data_dir, migrate_legacy_data

_lock = threading.RLock()

DEFAULTS: dict[str, Any] = {
    # server
    "host": "0.0.0.0",
    "port": 5000,
    "api_key": "",
    "lock_ips": False,
    "allowed_ips": [],

    # playback
    "volume": 70,
    "eq": "flat",
    "normalize": False,
    "crossfade": 0,
    "repeat": "off",
    "announce": True,
    "tts_voice": "en-US-AriaNeural",
    "theme": "default",

    # fetching
    "js_runtime": "node",
    # web_embedded returns audio-only opus (~128k) and actually downloads.
    # The old "tv" default now fails outright, and even when it worked it gave
    # itag 18 — 96k AAC muxed with 360p video, 3x the bytes for worse audio.
    "player_client": "web_embedded",
    # Tried in order when a download fails; YouTube breaks clients regularly.
    "player_client_fallbacks": ["web", "mweb", ""],
    "cookies_file": "",
    "cookies_from_browser": "",
    "cookie_auto_refresh": True,
    "cookie_check_interval": 3600,
    "cookie_close_browser_optin": False,   # ask before closing a browser
    "cookie_browsers": "",                 # "" = auto-detect
    "source": "youtube",
    "download_retries": 2,
    "download_workers": 2,
    "min_duration": 60,       # reject sub-minute results (the "30 second version" bug)

    # queue
    "queue_target": 12,       # base lookahead, grows with the session
    "queue_max": 30,
    "queue_min_ready": 3,     # downloaded tracks that must sit ahead
    "queue_pool_min": 20,     # candidate ideas kept in reserve
    "artist_run_limit": 3,    # max consecutive tracks by one artist
    "artist_cohesion": 1.0,   # 0 = pure discovery, 2 = stay on the band
    "history_size": 200,
    "dedupe_hours": 12,

    # taste
    "completion_ratio": 0.30,
    "liked_boost": 2.0,
    "skip_penalty": 0.8,

    # llm
    "groq_api_key": "",
    "groq_model": "openai/gpt-oss-20b",
    "use_groq": True,
    "groq_timeout": 4,

    # extras
    "lastfm_api_key": "",
    "lastfm_secret": "",
    "lastfm_session": "",
    "library_paths": [],
    "cast_peers": [],
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
            tmp = self._path.with_suffix(".json.tmp")
            text = json.dumps(self._data, indent=2)
            tmp.write_text(text, encoding="utf-8")   # no BOM
            os.replace(tmp, self._path)

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

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

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
