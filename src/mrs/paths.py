"""Where everything lives.

One data directory, whether we're frozen or running from source. The old build
kept a second config next to the sources, the two drifted, and a key saved in
one was invisible to the other — so there is exactly one location now and
anything found in the old spots gets migrated into it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "MusicRequestServer"

FROZEN = getattr(sys, "frozen", False)


def data_dir() -> Path:
    """User data: config, state, stats, cookies. Always the same place."""
    override = os.environ.get("MRS_DATA_DIR")
    if override:
        p = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_dir() -> Path:
    """Read-only bundled assets (templates)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """Project root when running from source; exe folder when frozen."""
    if FROZEN:
        return Path(os.path.dirname(sys.executable))
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    return data_dir() / "config.json"


def cache_dir() -> Path:
    p = Path(os.environ.get("TEMP", "/tmp")) / "mrs_audio_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def pinned_dir() -> Path:
    """Downloads kept forever (offline pinning), safe from cache cleanup."""
    p = data_dir() / "pinned"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Files that used to live in src/ or next to the .exe.
_LEGACY_NAMES = [
    "config.json", "player_state.json", "liked_songs.json", "play_stats.json",
    "recent_requests.json", "playlists.json", "ui_prefs.json",
    "youtube_cookies.txt",
]


def _legacy_dirs() -> list[Path]:
    dirs = [repo_root(), repo_root() / "src"]
    if FROZEN:
        dirs.append(Path(os.path.dirname(sys.executable)))
    return [d for d in dirs if d.is_dir()]


def migrate_legacy_data() -> list[str]:
    """Pull old side-by-side data into the single data dir. Never overwrites."""
    moved = []
    target = data_dir()
    for d in _legacy_dirs():
        if d.resolve() == target.resolve():
            continue
        for name in _LEGACY_NAMES:
            src = d / name
            dst = target / name
            if src.is_file() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                    moved.append(f"{src} -> {dst}")
                except Exception:
                    pass
    return moved
