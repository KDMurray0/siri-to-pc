"""EQ, normalise, crossfade, level metering.

Crossfade runs on a second mpv: preload, ramp both, hand back. Volume is
restored in finally so a failure can't leave you on silence.
"""

from __future__ import annotations

import threading
import time

from ..config import config
from ..events import bus
from ..logging_setup import get
from .tempo import blend_seconds as tempo_blend

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
        self.track_for = None       # set by the player; path -> Track

    # -- filter chain --------------------------------------------------
    def build_chain(self) -> str:
        parts: list[str] = []
        for freq, gain in EQ_PRESETS.get(config.get("eq", "flat"), []):
            parts.append(f"equalizer=f={freq}:width_type=o:width=1.5:g={gain}")
        if config.get("normalize"):
            # target a consistent RMS, not just peak clipping
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
            # Let the tempo change decide how long to overlap. Nothing about
            # which track is next — only how it's joined.
            if self.track_for:
                cf = tempo_blend(cf, self.track_for(pl[pos].get("filename", "")),
                                 self.track_for(nxt))
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
            #
            # "always", not "yes". keep-open=yes only holds on the *last*
            # playlist entry — mid-playlist it advances anyway, which meant
            # the primary moved to track 2 on its own during the fade and the
            # playlist-next below then landed on track 3. That's the song that
            # kept getting skipped. keep-open-pause=no holds at the end
            # without pausing, so the handoff doesn't leave a paused player.
            self.mpv.set("keep-open", "always")
            self.mpv.set("keep-open-pause", "no")
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
            self.mpv.set("pause", False)     # in case it held at EOF
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
                self.mpv.set("keep-open-pause", "yes")
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
    @staticmethod
    def _lufs_to_unit(value, floor: float = -40.0, ceiling: float = -5.0) -> float:
        try:
            lufs = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, (lufs - floor) / (ceiling - floor)))

    def read_levels(self) -> dict | None:
        """Real numbers from the ebur128 filter.

        Momentary and short-term loudness plus per-channel peaks — enough for a
        meter that reacts to transients and shows left/right separately,
        instead of one number driving every bar in lockstep.
        """
        try:
            data = self.mpv.get("af-metadata/lvl", None)
            if not isinstance(data, dict) or not data:
                return None
        except Exception:
            return None

        momentary = data.get("lavfi.r128.M")
        short = data.get("lavfi.r128.S")
        if momentary is None and short is None:
            return None
        peak_l = data.get("lavfi.r128.sample_peak.L") or data.get("lavfi.r128.true_peak.L")
        peak_r = data.get("lavfi.r128.sample_peak.R") or data.get("lavfi.r128.true_peak.R")

        def peak_unit(v):
            # peaks are dBFS-ish: -30 quiet .. 0 full scale
            try:
                return max(0.0, min(1.0, (float(v) + 30.0) / 30.0))
            except (TypeError, ValueError):
                return None

        m = self._lufs_to_unit(momentary if momentary is not None else short)
        s = self._lufs_to_unit(short if short is not None else momentary)
        pl = peak_unit(peak_l)
        pr = peak_unit(peak_r)
        return {
            "m": round(m, 3),
            "s": round(s, 3),
            "l": round(pl if pl is not None else m, 3),
            "r": round(pr if pr is not None else m, 3),
        }
