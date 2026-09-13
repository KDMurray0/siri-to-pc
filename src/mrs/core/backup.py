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
import stat
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
MAX_UNPACK_MB = 200
MAX_MEMBER_MB = 25
MAX_MEMBERS = 5000


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
        root_resolved = root.resolve()
        safe = []
        total_unpacked = 0
        seen: set[str] = set()
        with zipfile.ZipFile(src) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_MEMBERS:
                return {"ok": False, "message": "Too many files in that backup"}
            for info in infos:
                # ZIP permits both slash styles even on Windows. Normalize
                # before checking traversal, drives, and the final resolved
                # path; checking only '/' allowed `..\\config.json` out.
                name = (info.filename or "").replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if (not parts or name.startswith("/") or name.startswith("//")
                        or (len(parts[0]) >= 2 and parts[0][1] == ":")
                        or any(p == ".." for p in parts)):
                    continue
                norm = "/".join(parts)
                if norm in seen:
                    return {"ok": False, "message": "Duplicate file in backup"}
                seen.add(norm)
                if norm not in allowed and parts[0] not in WANTED_DIRS:
                    continue
                if parts[-1] in NEVER:
                    continue
                if info.file_size > MAX_MEMBER_MB * 1048576:
                    return {"ok": False, "message": "A file in that backup is too large"}
                total_unpacked += info.file_size
                if total_unpacked > MAX_UNPACK_MB * 1048576:
                    return {"ok": False, "message": "That backup expands too far"}
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    return {"ok": False, "message": "Symlinks are not allowed"}
                candidate = (root / Path(*parts)).resolve()
                try:
                    candidate.relative_to(root_resolved)
                except ValueError:
                    continue
                safe.append((info, norm, parts))
        if not safe:
            return {"ok": False, "message": "Nothing recognisable in there"}
        # Copy what's here now — with the source closed, so the copy can't
        # land on the file we're about to read.
        keep = make_backup()
        with zipfile.ZipFile(src) as z:
            for info, norm, parts in safe:
                out = root.joinpath(*parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fh:
                    out.write_bytes(fh.read())
    except zipfile.BadZipFile:
        return {"ok": False, "message": "That isn't a zip"}
    except Exception as exc:
        log.warning("restore failed: %s", exc)
        return {"ok": False, "message": f"Restore failed: {exc}"}
    log.info("restored %d files from %s", len(safe), src)
    return {"ok": True, "files": len(safe), "previous": keep.get("path"),
            "message": f"Restored {len(safe)} files. Restart to pick them up."}
