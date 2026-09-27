"""The Windows media overlay and the keyboard's media keys, run by us.

mpv has its own, and it can't be let go of: switching it off at runtime leaves
the session up, and its buttons call pause and next directly, past any key
binding. So with the phone as the speaker, the keyboard's pause key still
paused it. This one is a MediaPlayer's controls driven by hand -- hidden while
the sound is somewhere else, and it names the song rather than the file.

Every WinRT call happens on one thread of its own. pywebview's thread is STA
for WebView2; winrt is never imported anywhere near it.
"""

from __future__ import annotations

import importlib.util
import threading
import time

from ..logging_setup import get

log = get("smtc")

# SystemMediaTransportControlsButton
_ACTIONS = {0: "resume", 1: "pause", 2: "pause", 6: "next", 7: "previous"}

_lock = threading.Lock()
_wake = threading.Event()
_up = threading.Event()
_state = {"started": False, "ok": False, "stop": False}
_want: dict = {}


def available() -> bool:
    """Present, without importing it -- importing starts COM on this thread."""
    try:
        return importlib.util.find_spec("winrt.windows.media.playback") is not None
    except (ImportError, ValueError):
        return False


def running() -> bool:
    return _state["ok"]


def start(on_action, on_seek=None, wait: float = 5.0) -> bool:
    """Bring the session up. True once Windows has it; False and mpv keeps its own."""
    with _lock:
        if _state["started"]:
            return _state["ok"]
        _state["started"] = True
    if not available():
        log.info("no winrt -- mpv keeps the media keys")
        return False
    threading.Thread(target=_run, args=(on_action, on_seek), daemon=True,
                     name="media overlay").start()
    _up.wait(wait)
    return _state["ok"]


def follow(*, show: bool, name: str = "", artist: str = "", album: str = "",
           art: str = "", playing: bool = False, pos: float = 0.0,
           dur: float = 0.0) -> None:
    """What the overlay should say now. Cheap; only differences reach Windows."""
    if not _state["ok"]:
        return
    with _lock:
        _want.update(show=bool(show), name=name, artist=artist, album=album,
                     art=art, playing=bool(playing), pos=float(pos or 0),
                     dur=float(dur or 0), at=time.monotonic())
    _wake.set()


def stop() -> None:
    _state["stop"] = True
    _wake.set()


def _run(on_action, on_seek) -> None:
    try:
        import datetime
        from winrt.windows.foundation import Uri
        from winrt.windows.media import (MediaPlaybackStatus, MediaPlaybackType,
                                         SystemMediaTransportControlsTimelineProperties)
        from winrt.windows.media.playback import MediaPlayer
        from winrt.windows.storage.streams import RandomAccessStreamReference

        mp = MediaPlayer()
        mp.command_manager.is_enabled = False        # we say what it shows, not it
        s = mp.system_media_transport_controls
        s.is_play_enabled = s.is_pause_enabled = s.is_stop_enabled = True
        s.is_next_enabled = s.is_previous_enabled = True
        s.is_enabled = False

        def pressed(_sender, args):
            act = _ACTIONS.get(int(args.button))
            if act:
                # Off the WinRT thread pool: control() talks to mpv and can wait.
                threading.Thread(target=_safe, args=(on_action, act), daemon=True).start()

        def moved(_sender, args):
            if on_seek:
                to = args.requested_playback_position.total_seconds()
                threading.Thread(target=_safe, args=(on_seek, to), daemon=True).start()

        s.add_button_pressed(pressed)
        s.add_playback_position_change_requested(moved)
    except Exception as exc:
        log.warning("media overlay unavailable: %s", exc)
        _up.set()
        return

    _state["ok"] = True
    _up.set()
    log.info("media overlay ready -- ours, not mpv's")
    had: dict = {}
    while not _state["stop"]:
        _wake.wait(5)
        _wake.clear()
        with _lock:
            want = dict(_want)
        if not want:
            continue
        try:
            if want["show"] != had.get("show"):
                s.is_enabled = want["show"]
            if not want["show"]:
                had = {"show": False}
                continue
            face = (want["name"], want["artist"], want["album"], want["art"])
            if face != had.get("face"):
                du = s.display_updater
                du.clear_all()
                du.type = MediaPlaybackType.MUSIC
                du.music_properties.title = want["name"] or "Music Request Server"
                du.music_properties.artist = want["artist"]
                du.music_properties.album_title = want["album"]
                if want["art"].startswith("http"):
                    try:
                        du.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(want["art"]))
                    except Exception as exc:
                        log.debug("overlay art: %s", exc)
                du.update()
            if want["playing"] != had.get("playing") or face != had.get("face"):
                s.playback_status = (MediaPlaybackStatus.PLAYING if want["playing"]
                                     else MediaPlaybackStatus.PAUSED)
            # The bar: on a new song, a play/pause, or a jump the clock can't explain.
            guess = had.get("pos", 0.0) + ((want["at"] - had.get("at", want["at"]))
                                          if had.get("playing") else 0.0)
            if (face != had.get("face") or want["playing"] != had.get("playing")
                    or abs(want["pos"] - guess) > 3 or want["dur"] != had.get("dur")):
                if want["dur"] > 0:
                    tl = SystemMediaTransportControlsTimelineProperties()
                    tl.start_time = tl.min_seek_time = datetime.timedelta(0)
                    tl.end_time = tl.max_seek_time = datetime.timedelta(seconds=want["dur"])
                    tl.position = datetime.timedelta(seconds=min(want["pos"], want["dur"]))
                    s.update_timeline_properties(tl)
                had.update(pos=want["pos"], at=want["at"], dur=want["dur"])
            had.update(show=True, face=face, playing=want["playing"])
        except Exception as exc:
            log.debug("overlay update: %s", exc)
    try:
        s.is_enabled = False
    except Exception:
        pass


def _safe(fn, arg) -> None:
    try:
        fn(arg)
    except Exception as exc:
        log.warning("media key: %s", exc)
