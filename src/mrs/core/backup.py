"""Copy the profile out, and put one back.

Everything the program has learned lives in one folder and there is no copy
of it anywhere: the api key, the playlists, weeks of play stats, and three
caches holding thousands of lookups that took days of listening to fill. A
playlist named ".." once resolved to that folder and the delete endpoint
removed the lot, which is what prompted this.

Downloads are left out — they're gigabytes, and they come back on their own.
Cookies are left out too: they expire, they're re-grabbed in a click, and a
backup that carries them is a credential file people will email to
themselves.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

from ..logging_setup import get
from ..paths import data_dir

log = get("backup")

# Everything worth keeping, and nothing that can be worked out again cheaply.
WANTED = (
    "config.json",
    "play_stats.json", "liked_songs.json", "ui_prefs.json",
    "recent_requests.json", "player_state.json",
    "tags.json", "eras.json", "kin.json", "searches.json", "tempo.json",
)
WANTED_DIRS = ("playlists",)

# Anything holding a live credential. Named here so the exclusion is a
# decision rather than an accident of what WANTED happens to list.
NEVER = ("youtube_cookies.txt", "cookies_session.txt")

MAX_MB = 200


def _members(root: Path):
    for name in WANTED:
        f = root / name
        if f.is_file():
            yield f, name
    for folder in WANTED_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.name not in NEVER:
                yield f, str(f.relative_to(root)).replace("\\", "/")


def make_backup(into: Path | None = None) -> dict:
    root = data_dir()
    dest_dir = Path(into) if into else root / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Never reuse a name. Restoring takes a safety copy first, and if that
    # copy lands on the file being restored from it overwrites the backup
    # with the very state you were trying to replace — which is exactly what
    # happened at minute resolution, and again at second resolution, because
    # both halves run inside the same second.
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    dest = dest_dir / f"MusicRequestServer-{stamp}.zip"
    n = 2
    while dest.exists():
        dest = dest_dir / f"MusicRequestServer-{stamp}-{n}.zip"
        n += 1
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path, arc in _members(root):
            z.write(path, arc)
            count += 1
    size = dest.stat().st_size
    log.info("backed up %d files to %s", count, dest)
    return {"ok": True, "path": str(dest), "files": count,
            "mb": round(size / 1048576, 2),
            "message": f"Saved {count} files. Keep it somewhere safe — it has "
                       f"your api key in it."}


def restore(zip_path: str) -> dict:
    """Put a backup back, after copying what's there now.

    Deliberately not clever: it writes the files it recognises and leaves
    everything else alone, so a truncated or half-right zip can't take the
    profile with it.
    """
    src = Path(zip_path)
    if not src.is_file():
        return {"ok": False, "message": "No such file"}
    if src.stat().st_size > MAX_MB * 1048576:
        return {"ok": False, "message": f"That's bigger than {MAX_MB}MB"}
    root = data_dir()
    allowed = set(WANTED)
    try:
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
        # Only things we'd have written, and nothing that climbs out of the
        # folder — a zip is a file somebody else may have made.
        safe = []
        for n in names:
            if ".." in n.split("/") or Path(n).is_absolute():
                continue
            if n in allowed or n.split("/")[0] in WANTED_DIRS:
                if Path(n).name not in NEVER:
                    safe.append(n)
        if not safe:
            return {"ok": False, "message": "Nothing recognisable in there"}
        # Copy what's here now — with the source closed, so the copy can't
        # land on the file we're about to read.
        keep = make_backup()
        with zipfile.ZipFile(src) as z:
            for n in safe:
                out = root / n
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as fh:
                    out.write_bytes(fh.read())
    except zipfile.BadZipFile:
        return {"ok": False, "message": "That isn't a zip"}
    except Exception as exc:
        log.warning("restore failed: %s", exc)
        return {"ok": False, "message": f"Restore failed: {exc}"}
    log.info("restored %d files from %s", len(safe), src)
    return {"ok": True, "files": len(safe), "previous": keep.get("path"),
            "message": f"Restored {len(safe)} files. Restart to pick them up."}
