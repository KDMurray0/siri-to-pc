"""Ties mpv, the queue, audio and taste together."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from .config import config
from .core.ambient import ambient
from .core.audio import EQ_PRESETS, AudioEngine
from .core.context import ContextBuilder
from .core.downloader import downloader
from .core.extras import AlarmClock, scrobbler
from .core.library import library
from .core.listen import listener
from .core.playlists import playlists
from .core.mpv import (MpvClient, PIPE_ALT, PIPE_MAIN, fresh_pipes,
                       kill_orphan_mpv,
                       kill_stray_mpv)
from .core.queue import QueueManager
from .core.sink import MpvSink
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

# Not an mpv device. Selecting it drops mpv onto the null output — it keeps
# decoding, so the clock and the queue carry on, but it lets go of the sound
# card and the browser becomes the speaker.
CAST_DEVICE = "cast:browser"

# How close together two crashes on one file have to be before we
# stop believing it's a coincidence and bin the file.
CRASH_WINDOW = 900.0


class PlayerService:
    def __init__(self) -> None:
        self.mpv = MpvClient(PIPE_MAIN, primary=True)
        self.alt = MpvClient(PIPE_ALT, primary=False)
        self.audio = AudioEngine(self.mpv, self.alt)
        self.queue = QueueManager(MpvSink(self.mpv), ContextBuilder(catalog))
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
        self._auto_volume = False
        self._alarms: AlarmClock | None = None
        self._announce_seq = 0
        self._announce_files: dict[str, str] = {}
        self._announce_lock = threading.Lock()
        # How many times each file has taken the player down, and when it
        # last did. Two in quick succession is the file, not bad luck.
        self._crashes: dict[str, tuple[int, float]] = {}

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        # Ours from a previous run in this process (there aren't any on a
        # cold start), then anything left behind by an app that died without
        # cleaning up. The pipe names are unique per process now, so neither
        # of these can block us — this is housekeeping, not a prerequisite.
        kill_stray_mpv()
        kill_orphan_mpv()
        vol = int(config.get("volume", 70))
        # gapless=yes so albums run together properly
        self.mpv.spawn(vol, extra_args=["--gapless-audio=yes"])
        try:
            self.alt.spawn(0)
        except Exception as exc:
            log.warning("crossfade engine unavailable: %s", exc)
        self.audio.apply_all()
        dev = config.get("audio_device", "auto")
        if dev == CAST_DEVICE:
            # Booting straight back into "the phone is the speaker" sounds
            # right and is wrong: the tab that claimed it is identified by a
            # per-tab name that cannot survive the browser closing, so nothing
            # can claim the sound and the machine comes up silent with the
            # speakers deliberately let go. Start on the speakers; the phone
            # takes it back with one tap, which is the cheaper mistake.
            log.info("last output was a browser — starting on the speakers")
            config.set("audio_device", "auto")
            config.set("cast_client", "")
            dev = "auto"
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
        # a week of search answers, and whatever the tag worker has picked
        # up since its last flush, are worth the writes on the way out
        from .resolve.catalog import save_cache
        from .core.tags import tagstore
        save_cache()
        tagstore.save()
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
        # Fresh pipe names. If the mpv we're replacing wedged while holding
        # its pipe — and a wedged mpv can refuse taskkill /F — reusing the
        # name means the replacement can never have it, and the restart fails
        # for exactly the reason it was needed.
        main, alt = fresh_pipes()
        self.mpv = MpvClient(main, primary=True)
        self.alt = MpvClient(alt, primary=False)
        self.mpv.spawn(int(config.get("volume", 70)),
                       extra_args=["--gapless-audio=yes"])
        try:
            self.alt.spawn(0)
        except Exception:
            pass
        self.audio.mpv = self.mpv
        self.audio.alt = self.alt
        self.audio.track_for = self.queue.track_for
        self.queue.sink = MpvSink(self.mpv)     # fresh process, fresh sink
        self.audio.apply_all()
        # Fresh processes come up on the real sound card; if the phone is the
        # output they have to be put back on null or the PC starts playing.
        dev = config.get("audio_device", "auto")
        if dev == CAST_DEVICE:
            self._release_sound_card(True)
        elif dev and dev != "auto":
            self.mpv.set("audio-device", dev)
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
                self._follow_the_clock()
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
        # What it was in the middle of when it went. Read before the restart,
        # because a fresh mpv knows nothing about any of it.
        was = self._watch.get("path") or ""
        at = float(self._watch.get("pos") or 0)
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
            self._revives = 0
            self._revive_at = 0.0
            self._resume_after_crash(was, at)
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

    def _resume_after_crash(self, path: str, at: float) -> None:
        """Carry on where it stopped, and give the song that stopped it a
        second chance before blaming it.

        A restart used to mean a fresh mpv with an empty playlist and nothing
        to put in it: the file that killed it was gone, the queue behind it
        was gone, and the refill loop stayed quiet because it only tops up a
        queue that already has something in it. The evening simply ended.

        Once is bad luck — a decode that tripped over, a device that went away
        mid-track. Twice on the same file is the file, so it gets dropped and
        deleted rather than taking the player down for a third time.
        """
        try:
            deaths = 0
            if path:
                had, when = self._crashes.get(path, (0, 0.0))
                # Two strikes, but only if they're close together. A file that
                # tripped over once this morning and once tonight is not a bad
                # file — it's two unrelated bits of bad luck, and deleting it
                # for that would be wrong.
                if time.monotonic() - when > CRASH_WINDOW:
                    had = 0
                deaths = had + 1
                self._crashes[path] = (deaths, time.monotonic())
                # Only the last few matter; this shouldn't grow all evening.
                if len(self._crashes) > 40:
                    self._crashes.pop(next(iter(self._crashes)), None)

            give_up = deaths >= 2
            got = self.queue.restore_order(resume="" if give_up else path,
                                           drop=path if give_up else "")
            if give_up:
                track = self.queue.track_for(path)
                name = f"{track.title} — {track.artist}" if track else "That track"
                self.queue.forget_file(path)
                self._crashes.pop(path, None)
                log.warning("skipping %s after two player crashes", name)
                bus.publish(Ev.TOAST, f"Skipping {name} — it keeps stopping the player")
            elif got["restored"]:
                # Back to roughly where it was. A couple of seconds early on
                # purpose: landing exactly on the frame that just crashed is
                # asking for the same crash.
                back = max(0.0, at - 3.0)
                if back > 1:
                    self._seek_soon(back)
                log.info("resumed %s at %.0fs", got["resumed"] or path, back)
                if self._revives or deaths:
                    bus.publish(Ev.TOAST, f"Player recovered — {got['resumed']}"
                                if got["resumed"] else "Player recovered")
            else:
                bus.publish(Ev.TOAST, "Player recovered")
        except Exception as exc:
            log.warning("couldn't resume after the crash: %s", exc)

    def _seek_soon(self, at: float, tries: int = 30) -> None:
        """mpv won't take a position for a file it hasn't opened yet."""
        def go() -> None:
            for _ in range(tries):
                time.sleep(0.2)
                try:
                    if (self.mpv.get("time-pos", None)) is not None:
                        self.mpv.command("seek", float(at), "absolute+exact",
                                         wait=False)
                        return
                except Exception:
                    pass
        threading.Thread(target=go, daemon=True, name="resume seek").start()

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
                    else:
                        # Didn't get far enough to count as played, so it was
                        # skipped — remember it in case that was the media key
                        # going off in a pocket.
                        self.queue.note_skip(track, played)
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
            # Turned with the keyboard or from mpv itself rather than
            # through control() — still you choosing, so it still counts.
            if not self._auto_volume:
                ambient.note_manual(v)

    def _follow_the_clock(self) -> None:
        """Quieter late, back up in the morning. See core/ambient."""
        due = ambient.due()
        if not due:
            return
        want, say = due
        if abs(want - int(config.get("volume", 70))) < 1:
            return
        # Flagged so _persist_volume doesn't read our own change back as a
        # decision you made and rebase the level on it.
        self._auto_volume = True
        try:
            self.mpv.set("volume", want)
            config.set("volume", want)
        finally:
            self._auto_volume = False
        log.info("volume %d for the %s", want, ambient.band())
        if say:
            bus.publish(Ev.TOAST, f"{say} — volume {want}")

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
        # The browser is deliberately not offered here any more. Playing on
        # your own device is a mode — it decides which queue you're in — and a
        # mode buried in a device list is how somebody ends up wondering why
        # the volume slider stopped doing anything. It's a toggle in the UI.
        return {"devices": out, "current": current, "casting": self.casting(),
                "cast_client": config.get("cast_client", "")}

    def set_audio_device(self, name: str, client: str = "") -> dict:
        name = (name or "auto").strip()
        config.set("audio_device", name)
        # Which browser is the speaker. Without this every open copy of the
        # player starts playing at once — pick the phone with the page also
        # open on the PC and you get both, which is exactly what it sounded
        # like. Only the tab that asked for it plays.
        if name == CAST_DEVICE:
            config.set("cast_client", (client or "").strip())
        else:
            config.set("cast_client", "")

        if name == CAST_DEVICE:
            try:
                # Only when we're actually coming from a real output. Picking
                # the phone twice used to record "null" as the way back, and
                # then returning to a speaker restored null — PC silent, with
                # the device list insisting it was on the headphones.
                if self.current_ao() != "null":
                    config.set("ao_before_cast", self.current_ao())
                self._release_sound_card(True)
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
            config.set("audio_device_label", "This phone or browser")
            self._announce_output(name, True, client=config.get("cast_client", ""))
            log.info("audio output -> cast (PC released)")
            return {"ok": True, "device": name, "cast": True,
                    "message": "Playing on this phone — the PC is off the sound card"}

        try:
            # Coming back from cast: hand mpv its output driver back first,
            # or setting a device on the null ao does nothing.
            if self.current_ao() == "null":
                self._release_sound_card(False)
            self.mpv.set("mute", False)
            self.mpv.set("audio-device", name)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        label = name
        for d in (self.mpv.command("get_property", "audio-device-list") or []):
            if d.get("name") == name:
                label = d.get("description") or name
        config.set("audio_device_label", "" if name == "auto" else label)
        self._announce_output(name, False, label)
        if listener.running:
            listener.restart()      # the meter should follow the sound
        log.info("audio output -> %s", label)
        return {"ok": True, "device": name,
                "message": f"Playing through {label if name != 'auto' else 'the system default'}"}

    def _announce_output(self, name: str, casting: bool, label: str = "",
                         client: str = "") -> None:
        """Tell every open client where the sound is going.

        Picking the phone from the PC has to reach the phone: it's the thing
        that has to start playing, and it has no way of knowing otherwise
        until someone reloads it.
        """
        bus.publish(Ev.OUTPUT, {
            "device": name, "casting": casting, "client": client,
            "label": label or config.get("audio_device_label", "")})

    def _release_sound_card(self, release: bool) -> None:
        """Put both mpv instances on (or off) the null output.

        Both, not just the primary. The crossfade engine is a second mpv that
        plays the incoming track out loud for the length of the overlap — so
        casting to the phone while it faded still came out of the PC speakers
        every time a song changed. Muting it isn't enough either: the point is
        to let go of the device, not to play silence into it.
        """
        back = config.get("ao_before_cast", "") or "wasapi"
        if back == "null":
            back = "wasapi"       # never restore into silence
        want = "null" if release else back
        for m in (self.mpv, self.alt):
            try:
                m.set("ao", want)
                m.set("mute", False)
            except Exception as exc:
                log.debug("ao switch failed on one engine: %s", exc)
        # Check it took, and make the current file actually come out of it.
        #
        # Changing `ao` reinitialises mpv's audio chain, and on the file
        # that's already open that reinit doesn't always happen by itself —
        # which is why coming back from the phone left the PC silent until
        # you searched for something new. A zero-distance exact seek forces
        # the chain to be rebuilt without moving the song.
        if not release:
            got = self.current_ao()
            if got and got != want:
                try:
                    self.mpv.set("ao", want)
                except Exception as exc:
                    log.debug("second ao attempt: %s", exc)
            try:
                at = self.mpv.get("time-pos", None)
                if at is not None:
                    self.mpv.command("seek", float(at), "absolute+exact",
                                     wait=False)
            except Exception as exc:
                log.debug("audio chain nudge: %s", exc)
        # The Windows media overlay and the media keys belong to whatever is
        # actually making the sound. While that's the phone, mpv answering
        # them means two things fighting over one play/pause.
        try:
            self.mpv.set("media-controls", "no" if release else "yes")
            self.mpv.set("input-media-keys", "no" if release else "yes")
        except Exception as exc:
            log.debug("media-controls switch failed: %s", exc)

    def current_ao(self, alt: bool = False) -> str:
        """mpv reports `ao` as a list of driver entries, empty when default."""
        try:
            cur = (self.alt if alt else self.mpv).get("ao", None)
        except Exception:
            return ""
        if isinstance(cur, list) and cur:
            return str(cur[0].get("name") or "")
        return str(cur or "")

    def casting(self) -> bool:
        return config.get("audio_device", "auto") == CAST_DEVICE

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
            ambient.note_manual(vol)
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
            self._sleep_timer = threading.Timer(minutes * 60, self._sleep_now)
            self._sleep_timer.daemon = True
            self._sleep_timer.start()
            return {"sleep_minutes": minutes, "message": f"Sleeping in {minutes} min"}
        return {"sleep_minutes": 0, "message": "Sleep timer off"}

    def _sleep_now(self) -> None:
        """Take it down gently rather than stopping mid-bar."""
        vol = int(config.get("volume", 70))
        secs = float(config.get("sleep_fade", 20) or 0)
        try:
            steps = max(4, int(secs * 2))
            for i in range(steps if secs > 0 else 0):
                if self._sleep_at is None:
                    # cancelled while it was fading — put it back
                    self.mpv.set("volume", vol)
                    return
                self.mpv.set("volume", int(vol * (1 - (i + 1) / steps)))
                time.sleep(secs / steps)
            self.mpv.set("pause", True)
        finally:
            # Never leave it silent for next time, whatever happened.
            self.mpv.set("volume", vol)
            self._sleep_at = None

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

    def playlist_play(self, name: str, shuffle: bool = False,
                      start: int = 0) -> dict:
        tracks = playlists.tracks(name)
        if not tracks:
            return {"ok": False, "message": f"{name} is empty"}
        where = ""
        if start:
            start = max(0, min(int(start), len(tracks) - 1))
            if start:
                where = f" from {tracks[start].title}"
                tracks = tracks[start:]
        self.queue.play_now(tracks, shuffle=shuffle, hold_radio=True,
                            kind="playlist")
        return {"ok": True, "message": f"Playing {name}{where}"}

    # -- announce ------------------------------------------------------
    def announce(self, text: str, session: str = "") -> None:
        """Say what's coming. `session` sends it to one guest instead.

        A guest listening on their own phone is in their own room, so the
        clip goes to them and nowhere near the speakers here — which is why
        their requests used to arrive silently while the owner's were read out.
        """
        if not text or not config.get("announce", True):
            return
        threading.Thread(target=self._speak, args=(text, session), daemon=True,
                         name="announce").start()

    def announce_file(self, aid: str) -> str | None:
        """Where a spoken clip lives, for the client that has to play it."""
        with self._announce_lock:
            return self._announce_files.get(aid)

    def _speak(self, text: str, session: str = "") -> None:
        try:
            import asyncio
            import tempfile

            import edge_tts
            # A fresh name per clip. One reused filename meant the phone
            # played whatever its cache still had, which was the last song's
            # name, and there was no way to tell the two apart.
            with self._announce_lock:
                self._announce_seq += 1
                aid = str(self._announce_seq)
            path = os.path.join(tempfile.gettempdir(), f"mrs_tts_{aid}.mp3")

            async def go():
                tts = edge_tts.Communicate(text, config.get("tts_voice",
                                                            "en-US-AriaNeural"))
                await tts.save(path)

            asyncio.run(go())

            if session or self.casting():
                # The phone is the speaker, so it does the talking too. mpv
                # is on the null output; playing this locally would announce
                # the song to an empty room. Same for a guest, except the
                # room is theirs and mpv was never involved.
                self._offer_announcement(aid, path, text, session)
                return

            self._ducking = True
            original = int(self.mpv.get("volume", config.get("volume", 70)))
            self.mpv.set("volume", max(8, int(original * 0.25)))
            # Out of the speaker the music is using, not whatever Windows
            # calls default — picking a second sound card moved the songs and
            # left the announcements behind on the first one.
            cmd = [shutil.which("mpv") or "mpv", "--no-video", "--really-quiet",
                   # At the volume the music is at. This is a second mpv, and
                   # without being told it starts at full — so turning the
                   # player down turned the songs down and left the voice
                   # announcing them at whatever the machine could manage.
                   f"--volume={max(0, min(150, original))}"]
            dev = config.get("audio_device", "auto")
            if dev and dev not in ("auto", CAST_DEVICE):
                cmd.append(f"--audio-device={dev}")
            cmd.append(path)
            subprocess.run(cmd, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.mpv.set("volume", original)
        except Exception as exc:
            log.debug("announce failed: %s", exc)
        finally:
            self._ducking = False

    def _offer_announcement(self, aid: str, path: str, text: str,
                            session: str = "") -> None:
        """Hand the clip to whichever browser is acting as the speaker."""
        with self._announce_lock:
            self._announce_files[aid] = path
            # Keep a few so a client fetching a moment late still finds its
            # clip, and bin the rest rather than filling temp all day. Wider
            # than it was: several guests can each be owed one at once.
            for old in sorted(self._announce_files, key=int)[:-12]:
                stale = self._announce_files.pop(old, None)
                try:
                    if stale:
                        os.remove(stale)
                except OSError:
                    pass
        evt = {"id": aid, "text": text,
               "client": config.get("cast_client", "")}
        if session:
            evt["session"] = session      # so the stream hands it to them only
        bus.publish(Ev.ANNOUNCE, evt)

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
