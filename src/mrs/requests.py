"""Turning "play something" into music. One path for every entry point."""

from __future__ import annotations

import re
import threading
import time

from .config import config
from .core.extras import caster
from .core.library import library
from .core.playlists import playlists
from .core.taste import taste
from .events import Ev, bus
from .logging_setup import get, spawn
from .models import Track
from .player import player
from .resolve import applemusic, numbers, parser, resolver, spotify

log = get("request")


class _Stopped(Exception):
    """The X was pressed while an import was matching tracks."""

# Which request is the live one. Resolving can take tens of seconds — an LLM
# call, several searches, then a download — and asking for something else
# during that used to leave two of them racing, with whichever finished last
# winning. Each new play request takes the next number; anything older checks
# in before it touches the queue and drops out if it has been superseded.
#
# Per room, not one number for the whole house. Shared, a guest asking for a
# song silently binned whatever the owner was half way through resolving,
# and the owner did the same back — "a newer request came in" from somebody
# in another building.
_gen: dict[str, int] = {}
_gen_lock = threading.Lock()


def _claim(room: str = "") -> int:
    with _gen_lock:
        _gen[room] = _gen.get(room, 0) + 1
        return _gen[room]


def _still_wanted(mine: int, room: str = "") -> bool:
    with _gen_lock:
        return mine == _gen.get(room, 0)

def say_to(room: str, msg: str) -> None:
    """A toast for one listener, or for everyone when room is "".

    Unstamped toasts go to the owner's page and nowhere else, so every "not
    found", every "playing that" and every error a guest caused was being
    read by the wrong person — and the guest got a page that said nothing.
    """
    if not msg:
        return
    bus.publish(Ev.TOAST, {"text": msg, "session": room} if room else msg)


_COMMANDS = {
    "pause": "pause", "resume": "resume", "next": "next", "previous": "previous",
    "shuffle": "shuffle", "repeat": "repeat", "mute": "mute", "unmute": "unmute",
    "like": "like", "volume": "volume", "volume_delta": "volume_delta",
}


def handle_request(text: str, *, mode: str = "play", source: str | None = None,
                   announce: bool = True, cast: bool = False,
                   queue=None, lists=None) -> dict:
    """The one entry point. Never raises.

    `queue` is whose queue this lands in — the shared player by default, or a
    guest's own when they're playing on their own device. Everything else
    about resolving the request is identical; only the destination differs.
    """
    queue = queue if queue is not None else player.queue
    guest = queue is not player.queue
    # Whose room the announcement belongs in. A guest is not in yours, so
    # theirs goes to their phone and never near the speakers here — saying
    # nothing at all was the wrong half of that rule.
    room = getattr(queue, "session_id", "") if guest else ""
    if guest:
        queue.note_request()
    text = (text or "").strip()
    if not text:
        return {"status": "error", "message": "Nothing to play"}

    def say(msg: str) -> None:
        if announce and msg:
            player.announce(msg, room)

    try:
        if cast or config.get("cast_all"):
            threading.Thread(target=caster.broadcast, args=(text,), daemon=True).start()

        # A streaming link is a playlist import, not a search.
        if spotify.is_spotify_url(text):
            return _import_spotify(text, announce=announce, queue=queue,
                                   room=room, lists=lists)
        if applemusic.is_apple_url(text):
            return _import_apple(text, announce=announce, queue=queue,
                                 room=room, lists=lists)

        # Your own playlists win over anything YouTube might suggest. Yours,
        # though — a guest naming one would have played it out of the front
        # room, because playlist_play only ever knew the shared queue.
        if not guest:
            hit = _match_playlist(text)
            if hit:
                res = player.playlist_play(hit, shuffle="shuffle" in text.lower())
                if res.get("ok"):
                    say(res["message"])
                return {"status": "played" if res.get("ok") else "error",
                        "message": res.get("message", ""), "via": "playlist"}

            # "for twenty minutes" / "in half an hour" — strip the timing off
            # and deal with whatever's left as an ordinary request. Also
            # yours: these set the sleep timer, and a guest doesn't get to
            # decide when the house stops playing.
            timed = _timing(text)
            if timed:
                return timed

        # "play something I'd like" — build off your taste, not one seed song
        if _FOR_YOU.search(text):
            return play_for_you(announce=announce, queue=queue, room=room)

        plan = parser.parse(text, mode=mode)
        if source:
            plan.source = source

        if plan.kind == "command":
            return _run_command(plan, queue=queue if guest else None)

        if plan.kind == "none":
            return {"status": "error", "message": "I didn't catch that"}

        # Already own it? Play the local file — instant, no download.
        if plan.kind in ("song", "auto") and library.count():
            local = library.find_exact(plan.query, plan.artist)
            if local:
                queue.play_now([local])
                msg = f"Playing {local.title} from your library"
                say(msg)
                return {"status": "played", "message": msg, "via": plan.via,
                        "source": "library"}

        # From here on this is a real search. Take the ticket, and stop
        # whatever the last one had in flight — the user has moved on, and a
        # download for a song they no longer want is just bandwidth and a
        # queue slot.
        mine = _claim(room)
        if plan.mode not in ("next", "queue"):
            # Not the X — just making room for what was asked for. An import
            # already running is somebody's forty-track playlist and survives.
            queue.cancel(user=False)

        queue._set_activity("finding", plan.query)
        res = resolver.resolve(plan)
        if not _still_wanted(mine, room):
            log.info("dropped %r — a newer request came in while it resolved", text)
            return {"status": "superseded", "message": "", "via": plan.via}
        if not res:
            say_to(room, res.spoken)
            queue._set_activity("idle")
            return {"status": "not_found", "message": res.spoken, "via": plan.via}

        shuffle = bool(plan.shuffle) if plan.shuffle is not None else False
        if plan.mode == "next":
            queue.play_next(res.tracks[0])
        elif plan.mode == "queue":
            queue.enqueue(res.tracks)
        else:
            queue.play_now(res.tracks, res.alternates,
                                  anchors=res.anchors, shuffle=shuffle,
                                  hold_radio=res.hold_radio, kind=plan.kind,
                                  theme=plan.query if plan.kind == "genre" else "")

        say(res.spoken)
        log.info("%s -> %s (%s, %d tracks)", text, res.spoken, plan.via,
                 len(res.tracks))
        return {"status": "played", "message": res.spoken, "via": plan.via,
                "kind": plan.kind, "tracks": len(res.tracks)}
    except Exception as exc:
        log.exception("request failed: %s", exc)
        return {"status": "error", "message": f"Something went wrong: {exc}"}


_PLAYLIST_PHRASE = re.compile(
    r"^\s*(?:play\s+)?(?:my\s+)?(.+?)\s*(?:playlist|list)\s*$", re.I)


def _match_playlist(text: str) -> str | None:
    """"play my gym playlist" / "gym playlist" / an exact playlist name."""
    names = playlists.names()
    if not names:
        return None
    stripped = re.sub(r"^\s*play\s+", "", text, flags=re.I).strip()
    m = _PLAYLIST_PHRASE.match(text)
    for candidate in filter(None, [m.group(1).strip() if m else None, stripped]):
        for name in names:
            if name.lower() == candidate.lower():
                return name
    # only fall back to a loose match when they actually said "playlist"
    if m:
        found = playlists.find(m.group(1).strip())
        if found:
            return found[0]
    return None


def _run_command(plan, queue=None) -> dict:
    """"Skip this", "pause", "louder". `queue` means a guest said it.

    A guest's "skip" has to move their own list. Sent to player.control it
    skipped the owner's song instead, from a phone in another building.
    """
    cmd = (plan.command or "").lower()
    if queue is not None:
        return _guest_command(cmd, plan, queue)
    if cmd == "more_like_this":
        return {"status": "ok", **player.queue_similar()}
    if cmd == "save":
        return {"status": "ok", **player.export_current()}
    if cmd == "add_to_playlist":
        name = plan.query or "Favourites"
        match = playlists.find(name)
        return {"status": "ok", **player.playlist_add_current(match[0] if match else name)}
    action = _COMMANDS.get(cmd)
    if not action:
        return {"status": "error", "message": f"I don't know how to {cmd}"}
    value = None
    if action in ("volume", "volume_delta"):
        try:
            value = int(plan.query)
        except Exception:
            value = 10 if action == "volume_delta" else 70
    result = player.control(action, value)
    return {"status": "ok", "message": result.get("message", "OK"),
            "via": plan.via}


def _guest_command(cmd: str, plan, queue) -> dict:
    """The handful of commands that mean anything without an mpv."""
    sink = queue.sink
    if cmd in ("next", "previous", "pause", "resume", "shuffle"):
        if cmd == "next":
            sink.advance()
        elif cmd == "previous":
            sink.jump(max(0, (sink.pos() or 0) - 1))
        elif cmd == "shuffle":
            queue.shuffle_upcoming()
        else:
            sink.set_paused(cmd == "pause")
        queue.publish_queue(force=True)
        return {"status": "ok", "message": "OK", "via": plan.via}
    if cmd == "more_like_this":
        return {"status": "ok", "message": "Already doing that", "via": plan.via}
    # Volume is the phone's own, liking writes to a library a guest hasn't
    # got, and saving is the owner's. Say so rather than failing quietly.
    return {"status": "error", "via": plan.via,
            "message": "That one's the computer's to do"}


_FOR_YOU = re.compile(
    r"\b(?:something|anything|music|songs?|stuff)\s+i(?:'?d)?\s+"
    r"(?:would\s+)?(?:like|enjoy|love)\b"
    r"|\bmy\s+(?:kind\s+of\s+music|taste|favourites?|favorites?)\b"
    r"|\b(?:surprise|shuffle)\s+me\b", re.I)


def play_for_you(*, announce: bool = True, queue=None, room: str = "") -> dict:
    """A queue drawn from what you actually ask for."""
    queue = queue if queue is not None else player.queue
    from .core import foryou
    from .resolve import catalog

    queue._set_activity("finding", "Picking something you'd like")
    # Whoever's queue this is, scored by whoever's taste that queue holds.
    # Built off the global store, a guest's "play something I'd like" was
    # answered out of the owner's listening history — the one thing a shared
    # link is not supposed to reach.
    tracks = foryou.build(catalog, taste=getattr(queue, "taste", None))
    queue._set_activity("idle")
    if not tracks:
        msg = ("Ask for a song, an artist or a vibe — I don't know your taste yet"
               if room else "Play a few things first and I'll learn what you like")
        say_to(room, msg)
        return {"status": "not_found", "message": msg, "via": "foryou"}

    queue.play_now(tracks, hold_radio=True, kind="foryou")
    msg = f"Playing {len(tracks)} songs you'd like"
    if announce:
        player.announce("Here's something you'd like", room)
    say_to(room, msg)
    log.info("for-you: %d tracks", len(tracks))
    return {"status": "played", "message": msg, "via": "foryou",
            "kind": "foryou", "tracks": len(tracks)}


# "play some jazz for twenty minutes" / "...in half an hour". The tail is the
# timing; everything before it is the actual request.
_UNIT = r"(?:min|mins|minute|minutes|hr|hrs|hour|hours)"
# "an hour and a half" ends on the fraction rather than on the unit
_TAIL = r"(?:\s+and\s+a\s+(?:half|quarter))?"
_FOR_A_WHILE = re.compile(
    r"^(?P<what>.*?)\s*\bfor\s+(?P<when>(?:the\s+next\s+)?[^,]*?"
    + _UNIT + _TAIL + r")\s*$", re.I)
_IN_A_WHILE = re.compile(
    r"^(?P<what>.*?)\s*\bin\s+(?P<when>[^,]*?" + _UNIT + _TAIL + r")\s*$", re.I)


def _timing(text: str):
    """Handle the timed forms, or return None and let the normal path run."""
    m = _FOR_A_WHILE.match(text)
    if m:
        mins = numbers.duration_minutes(m.group("when"))
        if mins and mins > 0:
            return play_for_a_while(m.group("what"), mins)
    m = _IN_A_WHILE.match(text)
    if m:
        mins = numbers.duration_minutes(m.group("when"))
        if mins and mins > 0:
            return play_later(m.group("what"), mins)
    return None


def _tidy(what: str) -> str:
    """What's left after the timing is stripped off."""
    t = re.sub(r"^\s*(?:please\s+)?(?:can you\s+)?(?:play|put on|start)\s+",
               "", (what or "").strip(), flags=re.I).strip()
    return t or "something I'd like"


def _spoken_length(mins: float) -> str:
    mins = int(round(mins))
    if mins % 60 == 0 and mins >= 60:
        hours = mins // 60
        return "an hour" if hours == 1 else f"{hours} hours"
    if mins > 60:
        return f"{mins // 60}h {mins % 60}m"
    return "a minute" if mins == 1 else f"{mins} minutes"


def play_for_a_while(what: str, minutes: float, *, announce: bool = True) -> dict:
    """Play something, then stop after a while. It's the sleep timer, asked
    for the way people actually ask for it."""
    res = handle_request(_tidy(what), announce=False)
    if res.get("status") not in ("played", "ok"):
        return res
    player.set_sleep(int(round(minutes)))
    msg = f"{res.get('message', 'Playing')} for {_spoken_length(minutes)}"
    bus.publish(Ev.TOAST, msg)
    if announce:
        player.announce(msg)
    log.info("timed session: %s for %s min", what, round(minutes))
    return {**res, "message": msg, "for_minutes": int(round(minutes))}


def play_later(what: str, minutes: float, *, announce: bool = True) -> dict:
    """Start in a while — but fetch it now, so it actually starts on time.

    The point of asking in advance is that it's ready. Resolving and
    downloading at the moment the timer fires would leave you listening to
    nothing for the first twenty seconds.
    """
    want = _tidy(what)
    msg = f"{want} in {_spoken_length(minutes)}"

    holding: dict = {"path": ""}

    def prepare() -> None:
        """Fetch it now and hold it at the start line.

        Pausing straight after the request doesn't work: the request returns
        as soon as the track is queued, and mpv starts playing when the
        download lands a few seconds later — after the pause. So wait for a
        file to actually be loaded, then pause and rewind it.
        """
        try:
            res = handle_request(want, announce=False)
            if res.get("status") not in ("played", "ok"):
                return
            for _ in range(120):                 # up to a minute to fetch
                path = player.mpv.get("path", "") or ""
                if path:
                    player.mpv.set("pause", True)
                    player.mpv.command("seek", 0, "absolute", wait=False)
                    holding["path"] = path
                    log.info("holding %r ready, starting in %s min",
                             want, round(minutes))
                    return
                time.sleep(0.5)
            log.warning("nothing loaded in time for %r", want)
        except Exception as exc:
            log.warning("couldn't prepare %r: %s", want, exc)

    def begin() -> None:
        try:
            # If something else got played in the meantime, that's what the
            # listener wanted — don't hijack it.
            now = player.mpv.get("path", "") or ""
            if holding["path"] and now and now != holding["path"]:
                log.info("skipping the timed start; something else is on")
                player._start_at = None
                return
            player._start_at = None
            player.control("resume")
            bus.publish(Ev.TOAST, f"Playing {want}")
            if announce:
                player.announce(f"Playing {want}")
        except Exception as exc:
            log.warning("timed start failed: %s", exc)

    player._start_at = time.time() + minutes * 60
    threading.Thread(target=prepare, daemon=True).start()
    timer = threading.Timer(minutes * 60, begin)
    timer.daemon = True
    timer.start()
    player.pending_start = timer

    bus.publish(Ev.TOAST, msg)
    if announce:
        player.announce(msg)
    log.info("scheduled: %s", msg)
    return {"status": "scheduled", "message": msg,
            "in_minutes": int(round(minutes)), "via": "timer"}


def _import_link(reader, service: str, url: str, *,
                 announce: bool = True, play: bool = True, queue=None,
                 room: str = "", lists=None) -> dict:
    """Turn a streaming link into a playlist, keeping its name.

    `reader` is whichever module knows how to read that service — it needs
    track_names(url) and link_name(url). Everything after that is the same
    for all of them: match the names, start playing the moment the first one
    lands, and keep the finished list as a playlist.

    play=False just files it away — for when you're collecting playlists
    rather than asking for one right now.
    """
    queue = queue if queue is not None else player.queue
    from .core.playlists import playlists
    # Whose library the result is filed in. A guest's own, if they have one;
    # theirs is the only place it should go, and it used to go into the
    # owner's — a stranger's Spotify link filing itself in your collection.
    store = lists if lists is not None else (None if room else playlists)

    # Saving a list is a background errand, not a performance. It used to
    # take over the progress bar and narrate every step, which is fine when
    # you're waiting to hear it and noise when you're filing it away.
    quiet = not play
    if not quiet:
        queue._set_activity("finding", f"Reading {service} link")
        say_to(room, f"Reading that {service} link…")
    verb = "Playing" if play else "Saved"
    guard = getattr(queue, "import_era", lambda: 0)()

    def work() -> None:
        names = reader.track_names(url)
        if not names:
            say_to(room, "Couldn't read that link — is the playlist public?")
            if not quiet:
                queue._set_activity("idle")
            return

        label = reader.link_name(url) or f"{service} import"
        if not quiet:
            say_to(room, f"{label}: {len(names)} tracks, matching them up…")

        def tell(busy: bool, done: int = 0) -> None:
            """Nudge whoever owns this library to redraw.

            The list appears the moment the name is known and fills in as the
            matching runs, so you can watch it happen. It used to appear only
            once every track had been found, and only if you happened to
            leave the tab and come back.
            """
            evt = {"playlists": True, "importing": label if busy else "",
                   "found": done, "total": len(names)}
            if room:
                evt["session"] = room
            bus.publish(Ev.SETTINGS, evt)

        # The list exists straight away, empty, with the right name on it.
        if store is not None:
            store.create(label)
            tell(True, 0)

        def progress(i, total, title):
            # Only the X stops this, and only the bar it was already using.
            if getattr(queue, "import_era", lambda: guard)() != guard:
                raise _Stopped()
            if not quiet:
                queue._set_activity("finding", f"{i}/{total} {title}", i / total)

        # Start on the first match instead of the last. The rest are appended
        # as they turn up, so the downloader is working the whole time the
        # remaining names are still being looked up.
        started, saved = [False], [0]

        def arrived(track, count):
            # Into the list as it's found, so the count climbs while you
            # watch rather than jumping from nothing to forty at the end.
            if store is not None:
                try:
                    store.add(label, track)
                    saved[0] += 1
                    # Every few, not every one: this redraws a panel.
                    if saved[0] < 4 or saved[0] % 5 == 0:
                        tell(True, saved[0])
                except Exception as exc:
                    log.debug("couldn't file %s: %s", track.title, exc)
            if not play:
                return
            if not started[0]:
                started[0] = True
                queue.play_now([track], hold_radio=True, kind="playlist")
                if announce:
                    player.announce(f"{verb} {label}", room)
            else:
                queue.enqueue([track], imported=True)

        try:
            tracks = spotify.resolve_imported(names, on_progress=progress,
                                              on_track=arrived)  # service-agnostic
        except _Stopped:
            log.info("%s import stopped by the user", service)
            if not quiet:
                queue._set_activity("idle")
            tell(False, saved[0])      # spinner off; keep what was found
            return
        if not quiet:
            queue._set_activity("idle")
        if not tracks:
            say_to(room, "None of those tracks could be found")
            if store is not None:
                store.delete(label)    # an empty list nobody asked for
                tell(False, 0)
            return

        # Anything the incremental pass missed, and the spinner off.
        if store is not None:
            store.add_many(label, tracks)
            tell(False, len(tracks))

        msg = f"{verb} {label} — {len(tracks)} tracks"
        if play:
            if not started[0]:          # nothing matched early enough to start
                queue.play_now(tracks, hold_radio=True, kind="playlist")
            say_to(room, msg + (" (saved as a playlist)" if store is not None else ""))
        else:
            say_to(room, msg)

    def unstick(_exc):
        # Whatever went wrong, the spinner has to come back down — the line
        # that lowers it sits three statements past the one that threw.
        if not quiet:
            queue._set_activity("idle")
        say_to(room, "That import failed — see the log")

    spawn(work, name=f"{service.lower()} import", on_error=unstick)
    return {"status": "ok", "message": f"Importing that {service} link…",
            "via": service.lower()}


def _import_spotify(url: str, *, announce: bool = True, play: bool = True,
                    queue=None, room: str = "", lists=None) -> dict:
    return _import_link(spotify, "Spotify", url, announce=announce, play=play,
                        queue=queue, room=room, lists=lists)


def _import_apple(url: str, *, announce: bool = True, play: bool = True,
                  queue=None, room: str = "", lists=None) -> dict:
    return _import_link(applemusic, "Apple Music", url, announce=announce,
                        play=play, queue=queue, room=room, lists=lists)


def add_spotify(url: str, queue=None, room: str = "", lists=None) -> dict:
    """Save a streaming link as a playlist without interrupting what's on."""
    common = dict(announce=False, play=False, queue=queue, room=room, lists=lists)
    if spotify.is_spotify_url(url):
        return _import_spotify(url, **common)
    if applemusic.is_apple_url(url):
        return _import_apple(url, **common)
    return {"status": "error", "message": "That isn't a Spotify or Apple Music link"}


def play_station(url: str, name: str = "", art: str = "", queue=None) -> dict:
    """Tune a live station picked from the search results."""
    queue = queue if queue is not None else player.queue
    if not url:
        return {"status": "error", "message": "No station"}
    track = Track(title=name or "Radio", artist="Radio", art=art, url=url,
                  source="radio", origin="request", reason="asked")
    queue.play_now([track], kind="radio")
    msg = f"Tuned to {track.title}"
    say_to(getattr(queue, "session_id", ""), msg)
    log.info("%s", msg)
    return {"status": "played", "message": msg, "via": "radio"}


def play_video(video_id: str, *, title: str = "", artist: str = "", art: str = "",
               mode: str = "play", queue=None) -> dict:
    """Play one exact track chosen from the search dropdown."""
    queue = queue if queue is not None else player.queue
    room = getattr(queue, "session_id", "") if queue is not player.queue else ""
    if room:
        # Counted the same as a typed request — this is the path most people
        # actually use, and it was going on nobody's tab.
        queue.note_request()
    track = Track(video_id=video_id, title=title, artist=artist, art=art,
                  origin="request")
    if mode == "next":
        queue.play_next(track)
        msg = f"Playing {title} next"
    elif mode == "queue":
        queue.enqueue([track])
        msg = f"Added {title}"
    else:
        queue.play_now([track])
        msg = f"Playing {title}" + (f" by {artist}" if artist else "")
        player.announce(msg, room)
    return {"status": "played", "message": msg}
