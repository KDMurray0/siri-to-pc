"""EQ, normalisation, crossfade and level metering.

Crossfade uses two mpv processes. The previous attempt faded by swapping an
`afade` filter in mid-song, which starts from the filter's own clock and
mis-fires, and its handoff had no error handling — the logs showed fades that
began and never finished. This version:

  * preloads the incoming track on the second mpv and ramps volumes explicitly,
    so timing never depends on filter internals;
  * holds a lock, so a skip during a fade can't start a second one;
  * restores volume and `keep-open` in `finally`, so a failure mid-fade leaves
    playback in a sane state rather than silent.
"""

from __future__ import annotations

import threading
import time

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get

log = get("audio")

EQ_PRESETS: dict[str, list[tuple[int, float]]] = {
    "flat": [],
    "bass": [(60, 6), (170, 4), (350, 2)],
    "warm": [(60, 3), (350, 2), (3500, -2)],
    "vocal": [(350, -2), (1000, 3), (3500, 4)],
    "treble": [(3500, 4), (10000, 6)],
    "loud": [(60, 5), (1000, -2), (10000, 5)],
}

# Labelled so we can read its metadata back over IPC for the visualiser.
_METER = "@lvl:ebur128=metadata=1:peak=true"


class AudioEngine:
    def __init__(self, primary, secondary=None) -> None:
        self.mpv = primary
        self.alt = secondary
        self._fading = threading.Lock()
        self._suppress_persist = False
        self._until = 0.0

    # -- filter chain --------------------------------------------------
    def build_chain(self) -> str:
        parts: list[str] = []
        for freq, gain in EQ_PRESETS.get(config.get("eq", "flat"), []):
            parts.append(f"equalizer=f={freq}:width_type=o:width=1.5:g={gain}")
        if config.get("normalize"):
            # Even out loudness across songs: target a consistent RMS rather
            # than just clipping peaks.
            parts.append("dynaudnorm=f=150:g=15:p=0.9:m=15:r=0.9")
        parts.append(_METER)
        return ",".join(parts)

    def apply(self) -> None:
        try:
            self.mpv.set("af", self.build_chain())
        except Exception as exc:
            log.debug("apply af failed: %s", exc)

    def apply_all(self) -> None:
        self.apply()
        try:
            self.mpv.set("volume", int(config.get("volume", 70)))
            mode = config.get("repeat", "off")
            self.mpv.set("loop-file", "inf" if mode == "one" else "no")
            self.mpv.set("loop-playlist", "inf" if mode == "all" else "no")
        except Exception:
            pass

    @property
    def suppress_persist(self) -> bool:
        """True while we're driving volume ourselves (don't save it as user intent)."""
        return self._suppress_persist

    # -- crossfade -----------------------------------------------------
    def busy(self) -> bool:
        return time.monotonic() < self._until

    def crossfade_to_next(self) -> bool:
        """Fade the current track into the next playlist entry."""
        cf = int(config.get("crossfade", 0) or 0)
        if cf <= 0 or not self.alt or not self.alt.alive():
            return False
        if not self._fading.acquire(blocking=False):
            return False
        self._until = time.monotonic() + cf + 3
        try:
            pl = self.mpv.command("get_property", "playlist") or []
            pos = self.mpv.get("playlist-pos", None)
            if pos is None or pos + 1 >= len(pl):
                return False
            nxt = pl[pos + 1].get("filename")
            if not nxt:
                return False
            return self._run_fade(nxt, cf)
        except Exception as exc:
            log.warning("crossfade aborted: %s", exc)
            return False
        finally:
            self._fading.release()

    def _run_fade(self, next_path: str, cf: int) -> bool:
        vol = int(config.get("volume", 70))
        log.info("crossfade start -> %s (%ss)", next_path.rsplit("\\", 1)[-1], cf)
        self._suppress_persist = True
        try:
            # Stop the primary auto-advancing while we overlap the two.
            self.mpv.set("keep-open", "yes")
            self.alt.set("volume", 0)
            self.alt.command("loadfile", next_path, "replace", wait=False)
            self.alt.set("pause", False)
            time.sleep(0.25)                      # let it actually open the file

            steps = max(8, cf * 10)
            started = time.monotonic()
            for i in range(steps):
                frac = (i + 1) / steps
                self.alt.set("volume", int(vol * frac))
                self.mpv.set("volume", int(vol * (1 - frac)))
                time.sleep(cf / steps)

            # Hand back to the primary at the point the secondary reached.
            elapsed = time.monotonic() - started
            self.mpv.set("volume", 0)
            self.mpv.command("playlist-next", wait=False)
            time.sleep(0.12)
            self.mpv.command("seek", round(elapsed + 0.15, 2), "absolute", wait=False)
            self.mpv.set("volume", vol)
            self.alt.command("stop", wait=False)
            self.alt.set("volume", 0)
            log.info("crossfade handoff done at %.1fs", elapsed)
            return True
        except Exception as exc:
            log.warning("crossfade failed mid-way: %s", exc)
            return False
        finally:
            # Never leave the user on a silent player.
            try:
                self.mpv.set("keep-open", "no")
                self.mpv.set("volume", vol)
            except Exception:
                pass
            self._suppress_persist = False
            self._until = time.monotonic() + 1.0

    def crossfade_skip(self, command: str) -> None:
        """next/previous with a fade when crossfade is on."""
        cf = int(config.get("crossfade", 0) or 0)
        if cf <= 0 or not self.alt or not self.alt.alive() or self.busy():
            self.mpv.command(command, wait=False)
            return
        if command == "playlist-next" and self.crossfade_to_next():
            return
        self.mpv.command(command, wait=False)

    # -- level metering (real audio, not a CSS loop) --------------------
    def read_level(self) -> float | None:
        """Momentary loudness 0..1 from the ebur128 filter, if available."""
        try:
            data = self.mpv.get("af-metadata/lvl", None)
            if not data:
                return None
            raw = None
            if isinstance(data, dict):
                raw = (data.get("lavfi.r128.M") or data.get("lavfi.r128.S"))
            if raw is None:
                return None
            lufs = float(raw)
        except Exception:
            return None
        # -40 LUFS (silence) .. -5 LUFS (loud) -> 0..1
        return max(0.0, min(1.0, (lufs + 40.0) / 35.0))
