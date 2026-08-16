"""Paths that differ between source and a frozen .exe.

Frozen: config/state live in %LOCALAPPDATA%\\MusicRequestServer so they survive
replacing the .exe folder; templates come from _MEIPASS (read-only).
Source: both are src/.
"""

import os
import shutil
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = "MusicRequestServer"
_STATE_FILES = ("config.json", "player_state.json", "liked_songs.json",
                "play_stats.json", "recent_requests.json", "ui_prefs.json")


def is_frozen():
    return getattr(sys, "frozen", False)


def data_dir():
    # writable: config.json + user state
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.dirname(sys.executable)
        d = os.path.join(base, _APP_DIR)
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            return os.path.dirname(sys.executable)
    return _SRC


def resource_dir():
    # read-only bundled assets (templates)
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return _SRC


def config_path():
    return os.path.join(data_dir(), "config.json")


def migrate_legacy_data():
    # One-time: copy config/state that sat next to the .exe into the stable dir,
    # so an existing setup (key, cookies, volume) survives the move.
    if not is_frozen():
        return
    exe_dir = os.path.dirname(sys.executable)
    dst = data_dir()
    if os.path.abspath(exe_dir) == os.path.abspath(dst):
        return
    for name in _STATE_FILES:
        s, d = os.path.join(exe_dir, name), os.path.join(dst, name)
        if os.path.isfile(s) and not os.path.isfile(d):
            try:
                shutil.copy2(s, d)
            except Exception:
                pass
