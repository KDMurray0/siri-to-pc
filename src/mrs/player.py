"""The player service: mpv, queue, audio and taste, wired together."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from .config import config, state_file
from .core.audio import EQ_PRESETS, AudioEngine
from .core.context import ContextBuilder
from .core.downloader import downloader
from .core.extras import AlarmClock, caster, scrobbler
from .core.library import library
from .core.playlists import playlists
from .core.mpv import MpvClient, PIPE_ALT, PIPE_MAIN, kill_stray_mpv
from .core.queue import QueueManager
from .core.taste import taste
from .events import Ev, bus
from .logging_setup import get
from .models import Track
from .resolve import catalog

log = get("player")

CREATE_NO_WINDOW = 0x08000000


class PlayerService:
    def __init__(self) -> None:
        self.mpv = MpvClient(PIPE_MAIN, primary=True)
        self.alt = MpvClient(PIPE_ALT, primary=False)
        self.audio = AudioEngine(self.mpv, self.alt)
        self.queue = QueueManager(self.mpv, ContextBuilder(catalog))
        self._stop = threading.Event()
        self._restarting = threading.Lock()
        self._watch: dict = {}
        self._sleep_timer: threading.Timer | None = None
        self._sleep_at: float | None = None
        self._ducking = False
        self._alarms: AlarmClock | None = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        kill_stray_mpv()
        time.sleep(0.4)
        vol = int(config.get("volume", 70))
        # gapless=yes so albums run together properly
        self.mpv.spawn(vol, extra_args=["--gapless-audio=yes"])
        try:
            self.alt.spawn(0)
        except Exception as exc:
            log.warning("crossfade engine unavailable: %s", exc)
        self.audio.apply_all()
        self.queue.start()
        catalog.set_preferences(taste.preferred_artists())
        threading.Thread(target=self._monitor, daemon=True, name="monitor").start()
        threading.Thread(target=self._levels, daemon=True, name="levels").start()
        self._alarms = AlarmClock(self._on_alarm)
        log.info("player ready")

    def stop(self) -> None:
        self._stop.set()
        self.queue.stop()
        taste.save()
        for m in (self.mpv, self.alt):
            try:
                m.close()
            except Exception:
                pass
        kill_stray_mpv()

    def restart(self) -> None:
        """Full restart of the playback engine (tray/right-click 'Restart')."""
        log.info("restarting playback engine")
        try:
            self.mpv.close()
            self.alt.close()
        except Exception:
            pass
        kill_stray_mpv()
        time.sleep(0.8)
        self.mpv = MpvClient(PIPE_MAIN, primary=True)
        self.alt = MpvClient(PIPE_ALT, primary=False)
        self.mpv.spawn(int(config.get("volume", 70)),
                       extra_args=["--gapless-audio=yes"])
        try:
            self.alt.spawn(0)
        except Exception:
            pass
        self.audio.mpv = self.mpv
        self.audio.alt = self.alt
        self.queue.mpv = self.mpv
        self.audio.apply_all()
        bus.publish(Ev.TOAST, "Player restarted")

    # -- monitor loop --------------------------------------------------
    def _monitor(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                if not self.mpv.alive():
                    self._recover()
                    continue
                self._watch_track()
                self._persist_volume()
                if int(config.get("crossfade", 0)) > 0:
                    self._maybe_crossfade()
                bus.publish(Ev.STATUS, self.status())
                self.queue.publish_queue()
            except Exception as exc:
                log.debug("monitor: %s", exc)

    def _recover(self) -> None:
        if not self._restarting.acquire(blocking=False):
            return
        try:
            log.warning("mpv died — restarting")
            self.restart()
        finally:
            self._restarting.release()

    def _watch_track(self) -> None:
        props = self.mpv.get_many(["path", "time-pos", "duration", "pause"])
        path = props.get("path") or ""
        pos = props.get("time-pos") or 0
        dur = props.get("duration") or 0

        prev = self._watch.get("path")
        if path != prev:
            if prev:
                track = self.queue.track_for(prev)
                if track:
                    played = self._watch.get("pos", 0)
                    length = self._watch.get("dur", 0)
                    if taste.record(track, played, length):
                        scrobbler.scrobble(track, played)
                    catalog.set_preferences(taste.preferred_artists())
            self._watch = {"path": path, "pos": pos, "dur": dur}
            cur = self.queue.track_for(path)
            if cur:
                scrobbler.now_playing(cur)
                log.info("now playing: %s — %s", cur.artist, cur.title)
        else:
            self._watch["pos"] = max(self._watch.get("pos", 0), pos)
            self._watch["dur"] = dur or self._watch.get("dur", 0)

    def _revive_alt(self) -> bool:
        """The crossfade engine is a second mpv; bring it back if it died."""
        if self.alt.alive():
            return True
        try:
            self.alt.close()
        except Exception:
            pass
        try:
            self.alt = MpvClient(PIPE_ALT, primary=False)
            self.alt.spawn(0)
            self.audio.alt = self.alt
            log.info("crossfade engine restarted")
            return True
        except Exception as exc:
            log.warning("could not restart the crossfade engine: %s", exc)
            return False

    def _maybe_crossfade(self) -> None:
        cf = int(config.get("crossfade", 0))
        if cf <= 0 or self.audio.busy():
            return
        if not self._revive_alt():
            return
        props = self.mpv.get_many(["time-pos", "duration", "pause",
                                   "playlist-pos", "playlist-count"])
        if props.get("pause"):
            return
        pos, dur = props.get("time-pos"), props.get("duration")
        ppos, count = props.get("playlist-pos"), props.get("playlist-count") or 0
        if pos is None or not dur or ppos is None or ppos + 1 >= count:
            return
        if pos >= dur - cf:
            threading.Thread(target=self.audio.crossfade_to_next, daemon=True).start()

    def _persist_volume(self) -> None:
        if self._ducking or self.audio.suppress_persist:
            return
        v = self.mpv.get("volume", None)
        if v is None:
            return
        v = int(round(v))
        if abs(v - int(config.get("volume", 70))) >= 1:
            config.set("volume", v)

    def _levels(self) -> None:
        """Publish real audio levels for the visualiser."""
        while not self._stop.is_set():
            time.sleep(0.07)
            try:
                if self.mpv.get("pause", True):
                    continue
                levels = self.audio.read_levels()
                if levels:
                    bus.publish(Ev.LEVEL, levels, sticky=False)
            except Exception:
                pass

    # -- status --------------------------------------------------------
    def status(self) -> dict:
        props = self.mpv.get_many(["path", "time-pos", "duration", "pause",
                                   "volume", "playlist-pos", "playlist-count",
                                   "idle-active"])
        path = props.get("path") or ""
        track = self.queue.track_for(path)
        state = "idle"
        if path:
            state = "paused" if props.get("pause") else "playing"
        return {
            "state": state,
            "position": props.get("time-pos") or 0,
            "volume": int(props.get("volume") or config.get("volume", 70)),
            "repeat": config.get("repeat", "off"),
            "crossfade": int(config.get("crossfade", 0)),
            "playlist_pos": props.get("playlist-pos"),
            "playlist_count": props.get("playlist-count") or 0,
            "activity": self.queue.activity.to_dict(),
            "track": {
                "name": track.title if track else "",
                "artist": track.artist if track else "",
                "album": track.album if track else "",
                "art": track.art if track else "",
                "video_id": track.video_id if track else "",
                "duration": props.get("duration") or (track.duration if track else 0),
                "liked": taste.is_liked(track.video_id) if track else False,
            } if track else {"name": "", "artist": "", "art": "", "duration": 0},
        }

    # -- transport -----------------------------------------------------
    def control(self, action: str, value=None) -> dict:
        a = (action or "").lower()
        if a in ("pause", "stop"):
            self.mpv.set("pause", True)
            return {"message": "Paused"}
        if a in ("resume", "unpause"):
            self.mpv.set("pause", False)
            return {"message": "Playing"}
        if a in ("playpause", "toggle"):
            self.mpv.command("cycle", "pause", wait=False)
            return {"message": "Toggled"}
        if a in ("next", "skip"):
            self.audio.crossfade_skip("playlist-next")
            return {"message": "Skipped"}
        if a in ("previous", "prev", "back"):
            self.audio.crossfade_skip("playlist-prev")
            return {"message": "Previous"}
        if a == "volume":
            vol = max(0, min(150, int(value or 0)))
            self.mpv.set("volume", vol)
            config.set("volume", vol)
            return {"message": f"Volume {vol}", "volume": vol}
        if a == "volume_delta":
            cur = int(self.mpv.get("volume", config.get("volume", 70)))
            return self.control("volume", cur + int(value or 0))
        if a == "mute":
            self.mpv.set("mute", True)
            return {"message": "Muted"}
        if a == "unmute":
            self.mpv.set("mute", False)
            return {"message": "Unmuted"}
        if a == "shuffle":
            self.mpv.command("playlist-shuffle", wait=False)
            self.queue.publish_queue()
            return {"message": "Shuffled"}
        if a == "repeat":
            order = ["off", "all", "one"]
            cur = config.get("repeat", "off")
            nxt = order[(order.index(cur) + 1) % 3] if cur in order else "all"
            config.set("repeat", nxt)
            self.mpv.set("loop-file", "inf" if nxt == "one" else "no")
            self.mpv.set("loop-playlist", "inf" if nxt == "all" else "no")
            return {"message": f"Repeat {nxt}", "repeat": nxt}
        if a == "like":
            return self.like_current()
        if a == "restart":
            self.restart()
            return {"message": "Player restarted"}
        return {"message": f"Unknown action {action}"}

    def seek(self, position: float) -> dict:
        self.mpv.command("seek", float(position), "absolute", wait=False)
        return {"ok": True}

    # -- likes / similar -----------------------------------------------
    def like_current(self) -> dict:
        track = self.queue.current_track()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        liked = taste.toggle_like(track)
        if liked:
            try:
                similar = catalog.related(track.video_id, limit=3)
                self.queue.enqueue([t for t in similar])
            except Exception:
                pass
        return {"ok": True, "liked": liked,
                "message": "Liked" if liked else "Unliked"}

    def queue_similar(self, count: int = 5) -> dict:
        track = self.queue.current_track()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        similar = catalog.related(track.video_id, limit=count)
        self.queue.enqueue(similar)
        return {"ok": True, "added": len(similar),
                "message": f"Queued {len(similar)} more like this"}

    # -- sleep timer ---------------------------------------------------
    def set_sleep(self, minutes: int) -> dict:
        if self._sleep_timer:
            self._sleep_timer.cancel()
            self._sleep_timer = None
            self._sleep_at = None
        if minutes and minutes > 0:
            self._sleep_at = time.time() + minutes * 60
            self._sleep_timer = threading.Timer(minutes * 60,
                                                lambda: self.mpv.set("pause", True))
            self._sleep_timer.daemon = True
            self._sleep_timer.start()
            return {"sleep_minutes": minutes, "message": f"Sleeping in {minutes} min"}
        return {"sleep_minutes": 0, "message": "Sleep timer off"}

    def sleep_remaining(self) -> int:
        if not self._sleep_at:
            return 0
        return max(0, int(self._sleep_at - time.time()))

    # -- export / pin --------------------------------------------------
    def export_current(self) -> dict:
        track = self.queue.current_track()
        if not track or not track.path:
            return {"ok": False, "message": "Nothing playing"}
        dest_dir = os.path.join(os.path.expanduser("~"), "Music", "MusicRequest")
        os.makedirs(dest_dir, exist_ok=True)
        import re
        safe = re.sub(r'[<>:"/\\|?*]', "_",
                      f"{track.artist} - {track.title}".strip(" -"))
        dest = os.path.join(dest_dir, safe + os.path.splitext(track.path)[1])
        try:
            shutil.copyfile(track.path, dest)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "path": dest, "message": f"Saved to {dest}"}

    def pin_current(self) -> dict:
        track = self.queue.current_track()
        if not track or not track.path:
            return {"ok": False, "message": "Nothing playing"}
        path = downloader.pin(track.path)
        return {"ok": bool(path),
                "message": "Kept offline" if path else "Couldn't pin that"}

    # -- playlists -----------------------------------------------------
    def playlists(self) -> list[dict]:
        return playlists.summary()

    def playlist_add_current(self, name: str) -> dict:
        track = self.queue.current_track()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        return playlists.add(name, track)

    def playlist_add_track(self, name: str, track: Track) -> dict:
        return playlists.add(name, track)

    def playlist_delete(self, name: str) -> dict:
        return playlists.delete(name)

    def playlist_play(self, name: str, shuffle: bool = False) -> dict:
        tracks = playlists.tracks(name)
        if not tracks:
            return {"ok": False, "message": f"{name} is empty"}
        self.queue.play_now(tracks, shuffle=shuffle, hold_radio=True)
        return {"ok": True, "message": f"Playing {name}"}

    # -- announce ------------------------------------------------------
    def announce(self, text: str) -> None:
        if not text or not config.get("announce", True):
            return
        threading.Thread(target=self._speak, args=(text,), daemon=True).start()

    def _speak(self, text: str) -> None:
        try:
            import asyncio
            import tempfile

            import edge_tts
            path = os.path.join(tempfile.gettempdir(), "mrs_tts.mp3")

            async def go():
                tts = edge_tts.Communicate(text, config.get("tts_voice",
                                                            "en-US-AriaNeural"))
                await tts.save(path)

            asyncio.run(go())
            self._ducking = True
            original = int(self.mpv.get("volume", config.get("volume", 70)))
            self.mpv.set("volume", max(8, int(original * 0.25)))
            subprocess.run([shutil.which("mpv") or "mpv", "--no-video",
                            "--really-quiet", path], timeout=30,
                           creationflags=CREATE_NO_WINDOW)
            self.mpv.set("volume", original)
        except Exception as exc:
            log.debug("announce failed: %s", exc)
        finally:
            self._ducking = False

    # -- alarms --------------------------------------------------------
    def _on_alarm(self, alarm: dict) -> None:
        from .requests import handle_request
        query = alarm.get("query") or "my liked songs"
        if alarm.get("volume"):
            self.control("volume", alarm["volume"])
        handle_request(query, announce=False)

    # -- settings ------------------------------------------------------
    def settings(self) -> dict:
        return {
            **config.public(),
            "eq_presets": list(EQ_PRESETS.keys()),
            "sleep_remaining": self.sleep_remaining(),
            "library_count": library.count(),
            "queue_stats": self.queue.stats(),
        }


player = PlayerService()
