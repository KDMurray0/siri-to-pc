"""Context-aware playback, in the dullest form that is actually true.

No microphone, no room profiling, no spatial anything. The one thing about
the listening environment this machine can know for certain is what time it
is, so that is what it acts on: late at night the volume comes down, in the
evening a little, and in the morning it goes back to whatever you had it at.

The level you set is remembered as the level you meant *for that time of
day*. Turn it up at midnight and midnight is louder from then on — the
adjustment scales what you chose, it doesn't argue with it.
"""

from __future__ import annotations

import time
from datetime import datetime

from ..config import config
from ..logging_setup import get

log = get("ambient")

DAY, EVENING, NIGHT = "day", "evening", "night"
_SAID = {DAY: "Back up for the day",
         EVENING: "Eased off for the evening",
         NIGHT: "Turned down for the night"}


def _within(hour: int, start: int, end: int) -> bool:
    """Hours wrap: 23 to 7 is a real range and 7 to 23 is the rest of it."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def band(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    night_at = int(config.get("quiet_hour", 23))
    up_at = int(config.get("wake_hour", 7))
    dusk_at = int(config.get("evening_hour", 20))
    if _within(hour, night_at, up_at):
        return NIGHT
    if _within(hour, dusk_at, night_at):
        return EVENING
    return DAY


def factor(which: str | None = None) -> float:
    which = which or band()
    if which == NIGHT:
        return max(5, min(100, int(config.get("quiet_level", 55)))) / 100
    if which == EVENING:
        return max(5, min(100, int(config.get("evening_level", 80)))) / 100
    return 1.0


class Ambient:
    """What the volume should be, and whether that's news."""

    def __init__(self) -> None:
        self._band = ""
        self._checked = 0.0

    def on(self) -> bool:
        return bool(config.get("auto_volume", True))

    def base(self) -> int:
        """The level you chose, before the time of day was applied.

        Written down the first time it's asked for. Left unrecorded, the
        level in the config is whatever the clock last set — so restarting
        at midnight would take 55% of an already-quiet 31 and call that the
        new normal, and a few restarts later the music is inaudible.
        """
        got = config.get("volume_base")
        if got is None:
            got = int(config.get("volume", 70))
            # Undo the adjustment currently in force, so the number recorded
            # is the level you'd have had in the daytime.
            got = int(round(got / (factor() or 1.0)))
            config.set("volume_base", max(0, min(150, got)))
        return max(0, min(150, int(got)))

    def note_manual(self, volume: int) -> None:
        """You moved the slider. That's the new level for this time of day."""
        if not self.on():
            config.set("volume_base", int(volume))
            return
        f = factor() or 1.0
        want = max(0, min(150, int(round(volume / f))))
        if want != self.base():
            config.set("volume_base", want)

    def wanted(self) -> int:
        return max(0, min(150, int(round(self.base() * factor()))))

    def due(self, *, force: bool = False) -> tuple[int, str] | None:
        """(level, what to say) when the time of day has moved on.

        Only on a boundary. Re-asserting the level every minute would fight
        anyone reaching for the knob, and the whole point is that this is a
        nudge you can overrule.
        """
        if not self.on():
            self._band = ""
            return None
        now = time.monotonic()
        if not force and now - self._checked < 30:
            return None
        self._checked = now
        which = band()
        if which == self._band:
            return None
        first = not self._band
        self._band = which
        if first:
            # Starting up inside a band isn't a change to announce, but the
            # level still has to be right.
            return (self.wanted(), "")
        return (self.wanted(), _SAID.get(which, ""))


ambient = Ambient()
