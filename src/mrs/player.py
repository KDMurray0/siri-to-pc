"""Ties mpv, the queue, audio and taste together."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from .config import config
from .core.audio import EQ_PRESETS, AudioEngine
from .core.context import ContextBuilder
from .core.downloader import downloader
from .core.extras import AlarmClock, scrobbler
from .core.library import library
from .core.listen import listener
from .core.playlists import playlists
from .core.mpv import MpvClient, PIPE_ALT, PIPE_MAIN, kill_stray_mpv
from .core.queue import QueueManager
from .core import radio
from .core.taste import taste
from .events import Ev, bus
from .logging_setup import get
from .models import Track
from .resolve import catalog

log = get("player")


def _song_on_air(track) -> str:
    """The song a station says it's playing, if it's a station and it says."""
    if not radio.is_station(track):
        return ""
    return radio.now_playing.title or ""


CREATE_NO_WINDOW = 0x08000000


class PlayerService:
    def __init__(self) -> None:
        self.mpv = MpvClient(PIPE_MAIN, primary=True)
        self.alt = MpvClient(PIPE_ALT, primary=False)
        self.audio = AudioEngine(self.mpv, self.alt)
        self.queue = QueueManager(self.mpv, ContextBuilder(catalog))
        self.audio.track_for = self.queue.track_for
        self._stop = threading.Event()
        self._restarting = threading.Lock()
        self._revives = 0            # consecutive failed restarts
        self._revive_at = 0.0        # monotonic; don't try again before this
        self._watch: dict = {}
        self._sleep_timer: threading.Timer | None = None
        self._sleep_at: float | None = None
        self._start_at: float | None = None      # a "play in ten minutes" job
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
        dev = config.get("audio_device", "auto")
        if dev and dev != "auto":
            self.mpv.set("audio-device", dev)
        self.queue.start()
        catalog.set_preferences(taste.preferred_artists())
        threading.Thread(target=self._monitor, daemon=True, name="monitor").start()
        if config.get("listen_loopback", True) and listener.available():
            listener.start()
        threading.Thread(target=self._levels, daemon=True, name="levels").start()
        self._alarms = AlarmClock(self._on_alarm)
        log.info("player ready")

    def stop(self) -> None:
        self._stop.set()
        listener.stop()
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
        self.audio.track_for = self.queue.track_for
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
                # cheap when it's already there, and the audio capture keeps
                # undoing it
                from .server import be_polite
                be_polite(quiet=True)
                if int(config.get("crossfade", 0)) > 0:
                    self._maybe_crossfade()
                bus.publish(Ev.STATUS, self.status())
                self.queue.publish_queue()
            except Exception as exc:
                log.debug("monitor: %s", exc)

    def _recover(self) -> None:
        """Bring mpv back, but slow down if it won't stay up.

        The monitor checks once a second, so a player that can't start at all
        — no mpv binary, no audio device, a bad argument — used to mean a
        restart every second for as long as the program ran: a process spawn,
        a warning and a toast, all of it once a second, forever.
        """
        now = time.monotonic()
        if now < self._revive_at:
            return
        if not self._restarting.acquire(blocking=False):
            return
        try:
            log.warning("mpv died — restarting (attempt %d)", self._revives + 1)
            self.restart()
        except Exception as exc:
            log.warning("restart failed: %s", exc)
        finally:
            self._restarting.release()
        if self.mpv.alive():
            if self._revives:
                log.info("mpv is back after %d attempts", self._revives + 1)
                bus.publish(Ev.TOAST, "Player recovered")
            self._revives = 0
            self._revive_at = 0.0
            return
        # Still down. Back off, and stop shouting about it — one toast when
        # it first goes, not one a second while it's gone.
        self._revives += 1
        wait = min(60.0, 2.0 ** min(self._revives, 6))
        self._revive_at = time.monotonic() + wait
        if self._revives == 1:
            bus.publish(Ev.TOAST, "The player won't start — check the log")
        log.warning("mpv still down after %d attempts, waiting %.0fs",
                    self._revives, wait)

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
        """Loudness fallback, only when we can't hear the output directly."""
        while not self._stop.is_set():
            time.sleep(0.1)
            if listener.running:
                continue
            try:
                if self.mpv.get("pause", True):
                    continue
                levels = self.audio.read_levels()
                if levels:
                    bus.publish(Ev.LEVEL, levels, sticky=False)
            except Exception:
                pass

    def audio_devices(self) -> dict:
        rows = self.mpv.command("get_property", "audio-device-list") or []
        current = config.get("audio_device", "auto")
        out = [{"name": "auto", "label": "System default",
                "active": current in ("", "auto")}]
        for d in rows:
            name = d.get("name", "")
            if name in ("auto", ""):
                continue
            out.append({"name": name,
                        "label": d.get("description") or name,
                        "active": name == current})
        return {"devices": out, "current": current}

    def set_audio_device(self, name: str) -> dict:
        name = (name or "auto").strip()
        config.set("audio_device", name)
        try:
            self.mpv.set("audio-device", name)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        label = name
        for d in (self.mpv.command("get_property", "audio-device-list") or []):
            if d.get("name") == name:
                label = d.get("description") or name
        config.set("audio_device_label", "" if name == "auto" else label)
        if listener.running:
            listener.restart()      # the meter should follow the sound
        log.info("audio output -> %s", label)
        return {"ok": True, "device": name,
                "message": f"Playing through {label if name != 'auto' else 'the system default'}"}

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
            "shuffle": bool(config.get("shuffle")),
            "position": props.get("time-pos") or 0,
            "volume": int(props.get("volume") or config.get("volume", 70)),
            "repeat": config.get("repeat", "off"),
            "crossfade": int(config.get("crossfade", 0)),
            "playlist_pos": props.get("playlist-pos"),
            "playlist_count": props.get("playlist-count") or 0,
            "activity": self.queue.activity.to_dict(),
            "timer": self.timer_state(),
            # A station shows the song it's playing with its own name
            # underneath. When it doesn't say — speech radio never does — the
            # station name is the answer.
            "track": {
                "name": (_song_on_air(track) or track.title) if track else "",
                "artist": (track.title if _song_on_air(track)
                           else track.artist) if track else "",
                "album": track.album if track else "",
                "art": track.art if track else "",
                "video_id": track.video_id if track else "",
                # mpv reports a minute-ish "duration" for a stream — its own
                # buffer window — and a progress bar built on that sits at 75%
                # and creeps, as though the song were about to end.
                "duration": 0 if radio.is_station(track)
                            else (props.get("duration")
                                  or (track.duration if track else 0)),
                "live": radio.is_station(track),
                # a station mid-song can be liked and saved; mid-news it can't
                "song_known": bool(radio.is_station(track)
                                   and radio.now_playing.song),
                "liked": taste.is_liked(track.video_id) if track else False,
            } if track else {"name": "", "artist": "", "art": "", "duration": 0,
                             "live": False, "song_known": False},
        }

    # -- transport -----------------------------------------------------
    # Liking, saving and adding all work on a station — the song announced
    # itself. What can't work is anything about order or position.
    ON_AIR_BLOCKED = {"next", "skip", "previous", "prev", "back", "shuffle",
                      "repeat", "seek"}

    def control(self, action: str, value=None) -> dict:
        a = (action or "").lower()
        # Live radio has no next track, nothing to shuffle and nothing to like.
        # The buttons are hidden, but Siri, the media keys and the API can all
        # still ask, so refuse here rather than in the page.
        if a in self.ON_AIR_BLOCKED and radio.is_station(self.queue.current_track()):
            station = (self.queue.current_track() or Track()).title
            return {"message": f"{station} is live", "ignored": True}
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
            # A mode, not a one-shot: the radio used to bury a one-off shuffle
            # under new tracks a second later.
            on = not bool(config.get("shuffle"))
            config.set("shuffle", on)
            if on:
                self.queue.shuffle_upcoming()
            return {"message": "Shuffle on" if on else "Shuffle off",
                    "shuffle": on}
        if a == "repeat":
            order = ["off", "all", "one"]
            cur = config.get("repeat", "off")
            nxt = order[(order.index(cur) + 1) % 3] if cur in order else "all"
            config.set("repeat", nxt)
            self.apply_repeat()
            label = {"off": "Repeat off", "all": "Repeating the queue",
                     "one": "Repeating this song"}[nxt]
            return {"message": label, "repeat": nxt}
        if a == "like":
            return self.like_current()
        if a == "restart":
            self.restart()
            return {"message": "Player restarted"}
        return {"message": f"Unknown action {action}"}

    def apply_repeat(self) -> None:
        mode = config.get("repeat", "off")
        self.mpv.set("loop-file", "inf" if mode == "one" else "no")
        self.mpv.set("loop-playlist", "inf" if mode == "all" else "no")

    def seek(self, position: float) -> dict:
        self.mpv.command("seek", float(position), "absolute", wait=False)
        return {"ok": True}

    # -- likes / similar -----------------------------------------------
    def _acting_on(self) -> Track | None:
        """The track a like/save/add should apply to.

        On a station that's the song it just announced, found on YouTube —
        we know the name, so there's no reason those buttons shouldn't work.
        Everywhere else it's simply what's playing.
        """
        track = self.queue.current_track()
        if not radio.is_station(track):
            return track
        artist, song = radio.now_playing.artist, radio.now_playing.song
        if not song:
            return None
        hits = catalog.search_songs(f"{song} {artist}".strip(), limit=1)
        return hits[0] if hits else None

    def like_current(self) -> dict:
        track = self._acting_on()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        liked = taste.toggle_like(track)
        # Liking normally lines up a few similar tracks. On a station there's
        # nothing to line up behind — you're listening to a stream, not a
        # queue — and doing it anyway downloaded three songs and stacked them
        # behind the radio.
        on_air = radio.is_station(self.queue.current_track())
        if liked and not on_air:
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

    def timer_state(self) -> dict:
        """Whatever clock is running, for the countdown in the corner."""
        waiting = max(0, int(self._start_at - time.time())) if self._start_at else 0
        if waiting:
            return {"kind": "start", "seconds": waiting}
        left = self.sleep_remaining()
        if left:
            return {"kind": "stop", "seconds": left}
        return {"kind": "", "seconds": 0}

    # -- export / pin --------------------------------------------------
    def export_current(self) -> dict:
        track = self._acting_on()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        if not track.path:
            # off the radio it hasn't been fetched yet, so fetch it
            track.path = downloader.fetch(track) or ""
        if not track.path:
            return {"ok": False, "message": f"Couldn't get {track.title}"}
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
        track = self._acting_on()
        if not track:
            return {"ok": False, "message": "Nothing playing"}
        return playlists.add(name, track)

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
            "listener": listener.status(),
            "queue_stats": self.queue.stats(),
        }


player = PlayerService()
