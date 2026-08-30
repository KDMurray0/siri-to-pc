"""What a link remembers about the person holding it.

A permanent link is a person: the same someone, coming back. So it gets a
folder of its own — the handful of settings that are theirs to choose, the
taste their listening builds up, and the playlists they make. None of it
touches the owner's, in either direction.

A link that expires is an evening, not a person. Those get the same defaults
every time and write nothing down: there is no sense in accumulating a taste
profile for a credential that dies at midnight, and a great deal of sense in
not leaving one behind.

The settings here are deliberately a short list. A guest chooses how *their*
queue behaves and how it sounds on *their* device; they do not choose
anything about the machine — not the port, not the cookies, not the cache,
not whether the house speakers are in use. That line is what makes a link
safe to hand out.
"""

from __future__ import annotations

import json
import threading
import time

from ..logging_setup import get
from ..paths import data_dir, write_atomic
from .taste import NeutralTaste, TasteEngine

log = get("profile")


# What a guest may set, with the value they get before they set anything.
#
# Not read from the owner's config: their settings are theirs, and a guest
# inheriting "shuffle is on because the owner likes it that way" is the same
# bug as a guest inheriting the owner's history. These are chosen to be a
# reasonable evening for somebody who never opens the settings at all.
GUEST_SETTINGS: dict[str, object] = {
    # how it sounds on their device
    "theme": "default",
    "eq": "flat",
    "normalize": False,
    "crossfade": 0,
    "announce": False,        # off by default: it's a shared house
    # how their queue behaves
    "shuffle": False,
    "repeat": "off",
    "artist_cohesion": 1.0,   # 0 = wander, 2 = stay on the band
    "anchor_pull": 0.35,      # how much the song they asked for still counts
    "artist_run_limit": 3,
    "queue_minutes": 10,      # their ceiling; the cast cap still applies
    "source": "youtube",
    # theirs to switch off
    "sleep_minutes": 0,       # 0 = no timer
}

# Types, so a query string can't put a string where a float belongs.
_TYPES: dict[str, type] = {
    "theme": str, "eq": str, "normalize": bool, "crossfade": int,
    "announce": bool, "shuffle": bool, "repeat": str,
    "artist_cohesion": float, "anchor_pull": float, "artist_run_limit": int,
    "queue_minutes": int, "source": str, "sleep_minutes": int,
}

# Values that would be silly or expensive, clamped rather than refused.
_LIMITS: dict[str, tuple[float, float]] = {
    "crossfade": (0, 12), "artist_cohesion": (0.0, 2.0),
    "anchor_pull": (0.0, 1.0), "artist_run_limit": (1, 10),
    "queue_minutes": (3, 60), "sleep_minutes": (0, 480),
}
_CHOICES: dict[str, tuple[str, ...]] = {
    "repeat": ("off", "all", "one"),
    "source": ("youtube", "soundcloud", "bandcamp"),
}


def coerce(key: str, value) -> object | None:
    """A settable value, or None if it isn't one."""
    want = _TYPES.get(key)
    if want is None:
        return None
    try:
        if want is bool:
            got = str(value).strip().lower() in ("1", "true", "yes", "on")
        elif want is str:
            got = str(value).strip()[:40]
        else:
            got = want(value)
    except (TypeError, ValueError):
        return None
    if key == "eq":
        # Checked against the presets that actually exist, not a copy of the
        # list that can drift. An unknown name is accepted by a plain string
        # check and then silently does nothing — the dropdown renders blank
        # because no option matches, which looks like the setting is broken.
        from .audio import EQ_PRESETS
        if got not in EQ_PRESETS:
            return None
    if key in _CHOICES and got not in _CHOICES[key]:
        return None
    if key in _LIMITS:
        lo, hi = _LIMITS[key]
        got = want(max(lo, min(hi, got)))
    return got


class Profile:
    """One link's settings, taste and playlists."""

    def __init__(self, pass_id: str, name: str = "", permanent: bool = False) -> None:
        self.id = pass_id
        self.name = name or "guest"
        self.permanent = bool(permanent)
        self._lock = threading.RLock()
        self._settings: dict[str, object] = dict(GUEST_SETTINGS)
        self.created = time.time()

        if self.permanent:
            self.taste = TasteEngine(root=self.home(create=True) / "taste")
            from .playlists import Playlists          # late: it imports paths
            self.lists = Playlists(home=self.home(create=True))
            self._load()
        else:
            # An evening, not a person. Reads flat, writes nowhere.
            self.taste = NeutralTaste()
            self.lists = None

    # -- where it lives -------------------------------------------------
    def home(self, create: bool = False):
        """Where this profile's things live.

        Doesn't create the folder unless something is about to be written
        into it. Creating on read means a temporary link — which by design
        keeps nothing — leaves an empty folder behind simply for having been
        looked at, and a data directory slowly fills with the ghosts of
        evenings.
        """
        p = data_dir() / "profiles" / _safe(self.id)
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def _file(self, create: bool = False):
        return self.home(create) / "settings.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("%s has an unreadable settings file: %s", self.name, exc)
            return
        if not isinstance(raw, dict):
            return
        with self._lock:
            for k, v in raw.items():
                got = coerce(k, v)
                if got is not None:
                    self._settings[k] = got

    def save(self) -> None:
        if not self.permanent:
            return          # nothing to remember about a link that expires
        try:
            with self._lock:
                data = dict(self._settings)
            write_atomic(self._file(create=True), json.dumps(data, indent=2))
        except Exception as exc:
            log.warning("couldn't save %s's settings: %s", self.name, exc)

    # -- settings -------------------------------------------------------
    def get(self, key: str, default=None):
        """Their setting if it's one of theirs, the machine's otherwise.

        Anything not on the guest list is a tuning knob rather than a
        preference — how deep the candidate pool goes, how long a download may
        take — and those are the machine's business, shared by everyone and
        private to nobody. Falling through to config for those keeps a
        session behaving like a session instead of like a stripped-down one.
        """
        with self._lock:
            if key in self._settings:
                return self._settings[key]
        if key in GUEST_SETTINGS:
            return GUEST_SETTINGS[key]
        from ..config import config
        return config.get(key, default)

    def set(self, key: str, value) -> object | None:
        """Returns what was actually stored, or None if it wasn't allowed."""
        got = coerce(key, value)
        if got is None:
            return None
        with self._lock:
            self._settings[key] = got
        self.save()
        return got

    def all(self) -> dict:
        with self._lock:
            return dict(self._settings)

    # -- the fun bit ----------------------------------------------------
    def describe(self) -> dict:
        """A line about this person for the owner's list.

        Their top artists and how much they've listened to, and nothing else
        — not what they played last night, not their history. Enough to be
        pleased about somebody's taste, not enough to read over their
        shoulder.
        """
        top: list[dict] = []
        played = 0
        try:
            top = [{"artist": a.get("artist", ""), "plays": a.get("plays", 0)}
                   for a in self.taste.top_artists(5) if a.get("artist")]
            played = len(self.taste.history_ids())
        except Exception as exc:
            log.debug("no taste summary for %s: %s", self.name, exc)
        lists = 0
        if self.lists is not None:
            try:
                lists = len(self.lists.summary())
            except Exception:
                lists = 0
        return {"id": self.id, "name": self.name, "permanent": self.permanent,
                "top_artists": top, "tracks_played": played,
                "playlists": lists}


def _safe(pass_id: str) -> str:
    keep = "-_"
    out = "".join(c for c in (pass_id or "") if c.isalnum() or c in keep)
    return out[:40] or "unknown"


class Profiles:
    """Every link that has one, kept warm."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, Profile] = {}

    def for_row(self, row: dict) -> Profile:
        """The profile for a pass row from security.read_token()."""
        pid = row.get("id", "")
        with self._lock:
            got = self._by_id.get(pid)
            if got is None:
                # expires == 0 means it never does, which is what makes this
                # a person rather than an evening.
                got = Profile(pid, row.get("name", ""),
                              permanent=not row.get("expires"))
                self._by_id[pid] = got
                log.info("profile for %r (%s)", got.name,
                         "permanent" if got.permanent else "temporary")
        return got

    def find(self, pass_id: str) -> Profile | None:
        with self._lock:
            return self._by_id.get(pass_id)

    def forget(self, pass_id: str) -> None:
        with self._lock:
            self._by_id.pop(pass_id, None)

    def listing(self) -> list[dict]:
        """For the owner's Sharing tab. Only links that have one."""
        with self._lock:
            got = list(self._by_id.values())
        return [p.describe() for p in got if p.permanent]

    def wipe(self, pass_id: str) -> bool:
        """Forget a person entirely — settings, taste, playlists.

        Used when a permanent link is deleted for good: leaving somebody's
        listening history on disk after you've taken their link away is not
        what anyone means by revoking it.
        """
        import shutil

        self.forget(pass_id)
        home = data_dir() / "profiles" / _safe(pass_id)
        if not home.exists():
            return False
        try:
            shutil.rmtree(home)
            log.info("wiped the profile for %s", pass_id)
            return True
        except OSError as exc:
            log.warning("couldn't wipe %s: %s", pass_id, exc)
            return False


profiles = Profiles()
