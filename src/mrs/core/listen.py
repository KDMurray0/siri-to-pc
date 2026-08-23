"""Listening to what's actually coming out of the speakers.

WASAPI loopback gives us the real output as it plays, so the meter is the
waveform rather than a guess. Bands are measured with a Goertzel filter bank —
seven single-frequency detectors instead of a whole FFT, which is cheap enough
to run in plain Python and keeps numpy out of the build.
"""

from __future__ import annotations

import math
import threading
import time
from array import array

from ..config import config
from ..events import Ev, bus
from ..logging_setup import get

log = get("listen")

BANDS = [60, 160, 400, 1000, 2500, 6000, 11000]
TARGET_RATE = 24000        # what we downsample to
WINDOW = 1024              # samples per measurement (~43ms)
PUBLISH_HZ = 14        # the page reads the track's own envelope now;
                       # this is the fallback, and 30/s of it was most
                       # of what the app pushed over the network


class Listener:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.error = ""
        self.device = ""
        self._peaks = [1e-6] * len(BANDS)

    # -- lifecycle --
    def available(self) -> bool:
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except Exception:
            return False

    def start(self) -> bool:
        if self.running or not self.available():
            return self.running
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="listen")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def restart(self) -> None:
        """Follow the output somewhere else."""
        self.stop()
        time.sleep(0.4)
        self._stop.clear()
        self.start()

    def status(self) -> dict:
        return {"running": self.running, "device": self.device,
                "error": self.error, "available": self.available()}

    # -- capture --
    def _open(self, pa):
        import pyaudiowpatch as pyaudio
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        # follow whatever mpv is playing through, not just the system default
        wanted = (config.get("audio_device_label") or "").strip()
        target = wanted or out["name"]
        loop = None
        for dev in pa.get_loopback_device_info_generator():
            if target and target[:28] in dev["name"]:
                loop = dev
                break
        if loop is None and wanted:
            for dev in pa.get_loopback_device_info_generator():
                if out["name"] in dev["name"]:
                    loop = dev
                    break
        if loop is None:
            raise RuntimeError("no loopback device for the output in use")
        out = {"name": loop["name"].replace(" [Loopback]", "")}
        rate = int(loop["defaultSampleRate"])
        channels = int(loop["maxInputChannels"]) or 2
        stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                         input=True, input_device_index=loop["index"],
                         frames_per_buffer=WINDOW)
        self.device = out["name"]
        return stream, rate, channels

    def _loop(self) -> None:
        import pyaudiowpatch as pyaudio
        while not self._stop.is_set():
            pa = None
            stream = None
            try:
                pa = pyaudio.PyAudio()
                stream, rate, channels = self._open(pa)
                self.running = True
                self.error = ""
                log.info("listening to %s (%d Hz, %d ch)", self.device, rate, channels)
                self._capture(stream, rate, channels)
            except Exception as exc:
                self.error = str(exc)
                self.running = False
                log.debug("capture stopped: %s", exc)
            finally:
                try:
                    if stream:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
                try:
                    if pa:
                        pa.terminate()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            time.sleep(3)      # device changed or went away; try again shortly

    def _capture(self, stream, rate: int, channels: int) -> None:
        step = max(1, round(rate / TARGET_RATE))       # crude downsample
        eff_rate = rate / step
        coeffs = [2.0 * math.cos(2.0 * math.pi * (f * WINDOW / eff_rate) / WINDOW)
                  for f in BANDS]
        # read exactly one publish-interval at a time, so nothing is read and
        # then thrown away
        chunk = max(WINDOW * step, int(rate / PUBLISH_HZ))

        while not self._stop.is_set():
            try:
                raw = stream.read(chunk, exception_on_overflow=False)
            except Exception:
                return
            samples = array("h")
            samples.frombytes(raw)
            mono = samples[::channels * step][-WINDOW:]   # one channel, downsampled
            if len(mono) < 256:
                continue
            self._publish(mono, coeffs)

    def _publish(self, mono, coeffs) -> None:
        n = len(mono)
        scale = 1.0 / (32768.0 * n)
        out = []
        for bi, coeff in enumerate(coeffs):
            s1 = s2 = 0.0
            for x in mono:
                s0 = x + coeff * s1 - s2
                s2 = s1
                s1 = s0
            power = (s1 * s1 + s2 * s2 - coeff * s1 * s2) * scale * scale
            mag = math.sqrt(max(0.0, power))
            # keep a slowly-falling peak per band so quiet music still fills out
            self._peaks[bi] = max(self._peaks[bi] * 0.9995, mag, 1e-6)
            out.append(max(0.0, min(1.0, mag / self._peaks[bi])))

        overall = sum(out) / len(out)
        bus.publish(Ev.LEVEL, {"bands": [round(v, 2) for v in out],
                               "m": round(overall, 2), "live": True},
                    sticky=False)


listener = Listener()
