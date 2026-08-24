"""Where things live. One data directory, wherever we're running from."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
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


def ensure_structure() -> dict:
    """Create every folder the app expects, on first run of a fresh install.

    The release zip is just an .exe — nothing else exists until we make it.
    """
    made = []
    for path in (data_dir(), data_dir() / "playlists", pinned_dir(), cache_dir(),
                 data_dir() / "logs"):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            made.append(str(path))
    readme = data_dir() / "README.txt"
    if not readme.exists():
        readme.write_text("\n".join([
            "Music Request Server — your data lives here.",
            "",
            "  config.json          settings (edit while the app is closed)",
            "  youtube_cookies.txt  your YouTube session, if you exported one",
            "  playlists/           one folder per playlist",
            "  pinned/              tracks kept offline",
            "  play_stats.json      what you play and skip",
            "  liked_songs.json     your likes",
            "  server.log           rotating log",
            "",
            "Deleting this folder resets the app; it is recreated on next run.",
            "",
        ]), encoding="utf-8")
        made.append(str(readme))
    return {"created": made, "root": str(data_dir())}


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


def write_atomic(path: Path, text: str) -> None:
    """Write a file so a crash can't leave half of one behind.

    Playlists and play stats were written straight over the top of
    themselves. A power cut or a full disk in the middle of that doesn't
    give you the old version back, it gives you a truncated file that won't
    parse — and the taste store is weeks of listening.

    The temp name carries the pid, because two copies of the server sharing
    a data directory would otherwise both write "x.tmp" and one would rename
    the other's half-finished file into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())     # rename is atomic; the write wasn't
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
