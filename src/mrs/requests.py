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

# Which request is the live one. Resolving can take tens of seconds — an LLM
# call, several searches, then a download — and asking for something else
# during that used to leave two of them racing, with whichever finished last
# winning. Each new play request takes the next number; anything older checks
# in before it touches the queue and drops out if it has been superseded.
_gen = 0
_gen_lock = threading.Lock()


def _claim() -> int:
    global _gen
    with _gen_lock:
        _gen += 1
        return _gen


def _still_wanted(mine: int) -> bool:
    with _gen_lock:
        return mine == _gen

_COMMANDS = {
    "pause": "pause", "resume": "resume", "next": "next", "previous": "previous",
    "shuffle": "shuffle", "repeat": "repeat", "mute": "mute", "unmute": "unmute",
    "like": "like", "volume": "volume", "volume_delta": "volume_delta",
}


def handle_request(text: str, *, mode: str = "play", source: str | None = None,
                   announce: bool = True, cast: bool = False) -> dict:
    """The one entry point. Never raises."""
    text = (text or "").strip()
    if not text:
        return {"status": "error", "message": "Nothing to play"}

    try:
        if cast or config.get("cast_all"):
            threading.Thread(target=caster.broadcast, args=(text,), daemon=True).start()

        # A streaming link is a playlist import, not a search.
        if spotify.is_spotify_url(text):
            return _import_spotify(text, announce=announce)
        if applemusic.is_apple_url(text):
            return _import_apple(text, announce=announce)

        # Your own playlists win over anything YouTube might suggest.
        hit = _match_playlist(text)
        if hit:
            res = player.playlist_play(hit, shuffle="shuffle" in text.lower())
            if announce and res.get("ok"):
                player.announce(res["message"])
            return {"status": "played" if res.get("ok") else "error",
                    "message": res.get("message", ""), "via": "playlist"}

        # "for twenty minutes" / "in half an hour" — strip the timing off and
        # deal with whatever's left as an ordinary request
        timed = _timing(text)
        if timed:
            return timed

        # "play something I'd like" — build off your taste, not one seed song
        if _FOR_YOU.search(text):
            return play_for_you(announce=announce)

        plan = parser.parse(text, mode=mode)
        if source:
            plan.source = source

        if plan.kind == "command":
            return _run_command(plan)

        if plan.kind == "none":
            return {"status": "error", "message": "I didn't catch that"}

        # Already own it? Play the local file — instant, no download.
        if plan.kind in ("song", "auto") and library.count():
            local = library.find_exact(plan.query, plan.artist)
            if local:
                player.queue.play_now([local])
                msg = f"Playing {local.title} from your library"
                if announce:
                    player.announce(msg)
                return {"status": "played", "message": msg, "via": plan.via,
                        "source": "library"}

        # From here on this is a real search. Take the ticket, and stop
        # whatever the last one had in flight — the user has moved on, and a
        # download for a song they no longer want is just bandwidth and a
        # queue slot.
        mine = _claim()
        if plan.mode not in ("next", "queue"):
            player.queue.cancel()

        player.queue._set_activity("finding", plan.query)
        res = resolver.resolve(plan)
        if not _still_wanted(mine):
            log.info("dropped %r — a newer request came in while it resolved", text)
            return {"status": "superseded", "message": "", "via": plan.via}
        if not res:
            bus.publish(Ev.TOAST, res.spoken)
            player.queue._set_activity("idle")
            return {"status": "not_found", "message": res.spoken, "via": plan.via}

        shuffle = bool(plan.shuffle) if plan.shuffle is not None else False
        if plan.mode == "next":
            player.queue.play_next(res.tracks[0])
        elif plan.mode == "queue":
            player.queue.enqueue(res.tracks)
        else:
            player.queue.play_now(res.tracks, res.alternates,
                                  anchors=res.anchors, shuffle=shuffle,
                                  hold_radio=res.hold_radio, kind=plan.kind,
                                  theme=plan.query if plan.kind == "genre" else "")

        if announce:
            player.announce(res.spoken)
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


def _run_command(plan) -> dict:
    cmd = (plan.command or "").lower()
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


_FOR_YOU = re.compile(
    r"\b(?:something|anything|music|songs?|stuff)\s+i(?:'?d)?\s+"
    r"(?:would\s+)?(?:like|enjoy|love)\b"
    r"|\bmy\s+(?:kind\s+of\s+music|taste|favourites?|favorites?)\b"
    r"|\b(?:surprise|shuffle)\s+me\b", re.I)


def play_for_you(*, announce: bool = True) -> dict:
    """A queue drawn from what you actually ask for."""
    from .core import foryou
    from .resolve import catalog

    player.queue._set_activity("finding", "Picking something you'd like")
    tracks = foryou.build(catalog)
    player.queue._set_activity("idle")
    if not tracks:
        msg = "Play a few things first and I'll learn what you like"
        bus.publish(Ev.TOAST, msg)
        return {"status": "not_found", "message": msg, "via": "foryou"}

    player.queue.play_now(tracks, hold_radio=True, kind="foryou")
    msg = f"Playing {len(tracks)} songs you'd like"
    if announce:
        player.announce("Here's something you'd like")
    bus.publish(Ev.TOAST, msg)
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
                 announce: bool = True, play: bool = True) -> dict:
    """Turn a streaming link into a playlist, keeping its name.

    `reader` is whichever module knows how to read that service — it needs
    track_names(url) and link_name(url). Everything after that is the same
    for all of them: match the names, start playing the moment the first one
    lands, and keep the finished list as a playlist.

    play=False just files it away — for when you're collecting playlists
    rather than asking for one right now.
    """
    from .core.playlists import playlists

    player.queue._set_activity("finding", f"Reading {service} link")
    bus.publish(Ev.TOAST, f"Reading that {service} link…")
    verb = "Playing" if play else "Saved"

    def work() -> None:
        names = reader.track_names(url)
        if not names:
            bus.publish(Ev.TOAST, "Couldn't read that link — is the playlist public?")
            player.queue._set_activity("idle")
            return

        label = reader.link_name(url) or f"{service} import"
        bus.publish(Ev.TOAST, f"{label}: {len(names)} tracks, matching them up…")

        def progress(i, total, title):
            player.queue._set_activity("finding", f"{i}/{total} {title}", i / total)

        # Start on the first match instead of the last. The rest are appended
        # as they turn up, so the downloader is working the whole time the
        # remaining names are still being looked up.
        started = [False]

        def arrived(track, count):
            if not play:
                return
            if not started[0]:
                started[0] = True
                player.queue.play_now([track], hold_radio=True, kind="playlist")
                if announce:
                    player.announce(f"{verb} {label}")
            else:
                player.queue.enqueue([track])

        tracks = spotify.resolve_imported(names, on_progress=progress,
                                          on_track=arrived)   # service-agnostic
        player.queue._set_activity("idle")
        if not tracks:
            bus.publish(Ev.TOAST, "None of those tracks could be found")
            return

        # keep it, so the import isn't a one-off — one write, not one per track
        playlists.add_many(label, tracks)

        msg = f"{verb} {label} — {len(tracks)} tracks"
        if play:
            if not started[0]:          # nothing matched early enough to start
                player.queue.play_now(tracks, hold_radio=True, kind="playlist")
            bus.publish(Ev.TOAST, msg + " (saved as a playlist)")
        else:
            bus.publish(Ev.TOAST, msg)
            bus.publish(Ev.SETTINGS, {"playlists": True})

    def unstick(_exc):
        # Whatever went wrong, the spinner has to come back down — the line
        # that lowers it sits three statements past the one that threw.
        player.queue._set_activity("idle")
        bus.publish(Ev.TOAST, "That import failed — see the log")

    spawn(work, name=f"{service.lower()} import", on_error=unstick)
    return {"status": "ok", "message": f"Importing that {service} link…",
            "via": service.lower()}


def _import_spotify(url: str, *, announce: bool = True, play: bool = True) -> dict:
    return _import_link(spotify, "Spotify", url, announce=announce, play=play)


def _import_apple(url: str, *, announce: bool = True, play: bool = True) -> dict:
    return _import_link(applemusic, "Apple Music", url,
                        announce=announce, play=play)


def add_spotify(url: str) -> dict:
    """Save a streaming link as a playlist without interrupting what's on."""
    if spotify.is_spotify_url(url):
        return _import_spotify(url, announce=False, play=False)
    if applemusic.is_apple_url(url):
        return _import_apple(url, announce=False, play=False)
    return {"status": "error", "message": "That isn't a Spotify or Apple Music link"}


def play_station(url: str, name: str = "", art: str = "") -> dict:
    """Tune a live station picked from the search results."""
    if not url:
        return {"status": "error", "message": "No station"}
    track = Track(title=name or "Radio", artist="Radio", art=art, url=url,
                  source="radio", origin="request", reason="asked")
    player.queue.play_now([track], kind="radio")
    msg = f"Tuned to {track.title}"
    bus.publish(Ev.TOAST, msg)
    log.info("%s", msg)
    return {"status": "played", "message": msg, "via": "radio"}


def play_video(video_id: str, *, title: str = "", artist: str = "", art: str = "",
               mode: str = "play") -> dict:
    """Play one exact track chosen from the search dropdown."""
    track = Track(video_id=video_id, title=title, artist=artist, art=art,
                  origin="request")
    if mode == "next":
        player.queue.play_next(track)
        msg = f"Playing {title} next"
    elif mode == "queue":
        player.queue.enqueue([track])
        msg = f"Added {title}"
    else:
        player.queue.play_now([track])
        msg = f"Playing {title}" + (f" by {artist}" if artist else "")
        player.announce(msg)
    return {"status": "played", "message": msg}
