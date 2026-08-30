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
    "cast_client": "",         # the one browser acting as the speaker

    # Security. The key belongs in the X-Music-Key header; URLs that can't
    # carry one (audio elements, EventSource, links you send people) get a
    # signed token instead. Turn allow_key_in_url off once nothing you use
    # still puts the raw key in a query string.
    "allow_key_in_url": True,
    # Encrypt the connection with a self-signed certificate. The browser
    # objects once and you accept it; after that nobody on the path can read
    # the key or what you're listening to. Worth having on before this is
    # reachable from the internet.
    "https": False,
    # The player page opens without a key on the home network, as it always
    # has. Off means even your own wifi needs a link with one.
    "lan_open": True,
    # While this is on, a full-access guest asking for something on the PC
    # speakers is refused. Anyone playing on their own phone is unaffected —
    # the point is to protect the room you're in, and headphones aren't in it.
    "block_full_guests": False,
    # Party mode: no radio, and every request joins the back of the queue
    # however it was phrased. Nobody's "play this next" jumps the line.
    "party_mode": False,

    # A name that follows your home address about. A residential IP is public
    # but not permanent, and every link handed out dies quietly when it moves.
    "ddns_provider": "dynu",   # dynu | duckdns | noip | afraid
    "ddns_hostname": "",
    "ddns_user": "",
    "ddns_password": "",       # never leaves this machine; hidden from the UI
    # The master limiter: downloads in flight across everybody, and how many
    # requests one guest may make an hour before they're asked to slow down.
    "max_downloads": 4,
    "guest_requests_hour": 40,
    # Whether the first-run guide has been through once. Not "have you read
    # it" — you can reopen it whenever — just "should it open by itself".
    "setup_done": False,
    # How far ahead to build a queue for somebody playing on their own phone.
    # Deliberately much shorter than the speakers' half-hour: every track is
    # fetched, transcoded and then pushed over the network to a device that
    # may walk out of the door, and thirty minutes of that is a gigabyte
    # nobody hears. Applies only while they're playing *here* — a phone being
    # used as a remote for the PC is on the PC's queue and unaffected.
    "cast_queue_minutes": 10,
    # A guest whose browser has stopped saying hello. Pause at the first,
    # let the session go at the second — nothing closes a tab politely, so
    # this is the only signal there is.
    "guest_quiet_pause": 45,       # seconds of silence before pausing them
    "guest_quiet_close": 900,      # ...and before the session is let go
    "listen_loopback": True,   # meter reads the real output

    # reaching this server from a phone. auto = use Tailscale if it's there
    "tailscale": "auto",       # auto | on | off
    "tailscale_exe": "",       # only if it's installed somewhere unusual

    # how much downloaded music to keep. Least-played goes first, so the
    # songs on repeat survive and the one-offs don't.
    "cache_size_mb": 2000,

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
    "sleep_fade": 20,         # seconds to fade out on the sleep timer
    "lastfm_seeded": False,   # taste already given a head start from the account
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
        # Keys this process has actually changed. Only these are written back
        # over whatever is on disk; see save().
        self._dirty: set[str] = set()
        self.load()

    # -- io --
    def load(self) -> None:
        with _lock:
            if not self._path.exists():
                migrate_legacy_data()
            raw, unreadable = {}, False
            if self._path.exists():
                try:
                    # utf-8-sig: tolerate a BOM written by PowerShell
                    raw = json.loads(self._path.read_text(encoding="utf-8-sig") or "{}")
                except Exception as exc:  # keep running on a corrupt file
                    print(f"[config] unreadable ({exc}); using defaults")
                    unreadable = True
                    self._keep_wreckage()
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in raw.items() if v is not None})

            # The key is this server's identity, not a setting. Every pass is
            # signed with it, so minting a new one silently revokes every link
            # ever handed out and locks the owner out of their own player —
            # which is precisely what one unlucky read did. Recover it from
            # the shadow copy before ever generating a replacement.
            fresh_key = False
            if not merged.get("api_key") or "CHANGE" in str(merged["api_key"]).upper():
                fresh_key = True
                saved = self._shadow_key()
                if saved:
                    print("[config] recovered the api key from its shadow copy")
                    merged["api_key"] = saved
                elif unreadable:
                    # No shadow and an unreadable file: a new key is the only
                    # way to run, but say so, because everything breaks.
                    print("[config] NO KEY RECOVERABLE — issuing a new one; "
                          "old links and passes will stop working")
                    merged["api_key"] = secrets.token_urlsafe(24)
                else:
                    merged["api_key"] = secrets.token_urlsafe(24)
            self._data = merged
            # Only write when loading actually changed something — a new key,
            # a missing default, or a file that wasn't there. Saving on every
            # load means any second process that so much as *reads* the config
            # stamps its own copy over the running app's, which is how the
            # key and the port kept diverging between the two.
            added = [k for k in DEFAULTS if k not in raw]
            if fresh_key or added or not self._path.exists():
                self.save(full=True)      # nothing else is in flight yet
            self._write_shadow()

    def _shadow_path(self):
        return self._path.with_name("api_key.txt")

    def _shadow_key(self) -> str:
        """The key, kept separately so a bad config read can't lose it."""
        try:
            got = self._shadow_path().read_text(encoding="utf-8-sig").strip()
            return got if got and "CHANGE" not in got.upper() else ""
        except Exception:
            return ""

    def _write_shadow(self) -> None:
        key = str(self._data.get("api_key") or "")
        if not key or self._shadow_key() == key:
            return
        try:
            self._shadow_path().write_text(key, encoding="utf-8")
        except Exception as exc:
            print(f"[config] couldn't shadow the api key: {exc}")

    def _keep_wreckage(self) -> None:
        """Move a corrupt config aside instead of writing over it.

        Whatever was in there is the only copy of settings built up over
        months; overwriting it with defaults is the one thing you can't undo.
        """
        try:
            import time as _t
            spoiled = self._path.with_name(f"config.corrupt.{int(_t.time())}.json")
            self._path.replace(spoiled)
            print(f"[config] kept the unreadable file at {spoiled.name}")
        except Exception:
            pass

    def save(self, full: bool = False) -> None:
        """Write our changes, not our whole idea of the file.

        Two processes sharing one config each hold a snapshot taken when they
        started, and writing the whole snapshot means the last one to save
        silently discards everything the other changed since. That is not
        hypothetical: a hostname typed into the running app disappeared
        because a second process — which had never heard of it — later set
        something unrelated and wrote its stale copy back over the top.

        So a save re-reads the file and lays only the keys this process
        actually changed on top of it. `full` is for the one caller that
        genuinely owns the whole thing: the initial load filling in defaults.
        """
        with _lock:
            data = self._data
            if not full and self._dirty:
                try:
                    on_disk = json.loads(
                        self._path.read_text(encoding="utf-8-sig") or "{}")
                    if isinstance(on_disk, dict) and on_disk:
                        # The key is identity, not a preference. Adopting a
                        # different one from disk mid-run invalidates every
                        # token this process has handed out and every link
                        # anyone is holding — silently, and only until the
                        # next restart, which is the worst way to find out.
                        theirs = on_disk.get("api_key")
                        mine_key = self._data.get("api_key")
                        if theirs and mine_key and theirs != mine_key:
                            print("[config] the key on disk changed under us — "
                                  "keeping the one this process started with")
                            on_disk = dict(on_disk, api_key=mine_key)
                        data = {**on_disk, **{k: self._data[k]
                                              for k in self._dirty
                                              if k in self._data}}
                        # Keep our own view in step with what everyone else
                        # has changed, or the next save reintroduces our stale
                        # values for keys we never touched.
                        self._data.update({k: v for k, v in on_disk.items()
                                           if k not in self._dirty})
                except Exception:
                    pass          # unreadable: our copy is better than nothing
            # no BOM, and atomically: this file holds the api key and every
            # setting, and losing it to a torn write means a fresh setup
            write_atomic(self._path, json.dumps(data, indent=2))
            self._dirty.clear()

    # -- access --
    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with _lock:
            old = self._data.get(key)
            self._data[key] = value
            self._dirty.add(key)
            if save:
                self.save()
        if old != value:
            self._notify(key, value)

    def update(self, values: dict[str, Any]) -> None:
        with _lock:
            self._data.update(values)
            self._dirty.update(values)
            self.save()
        for k, v in values.items():
            self._notify(k, v)

    def public(self) -> dict[str, Any]:
        """Everything except secrets — safe to hand to the UI."""
        hidden = {"api_key", "groq_api_key", "lastfm_secret", "lastfm_session",
                  "ddns_password"}
        out = {k: v for k, v in self._data.items() if k not in hidden}
        out["groq_set"] = bool(self._data.get("groq_api_key"))
        out["ddns_set"] = bool(self._data.get("ddns_password"))
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
