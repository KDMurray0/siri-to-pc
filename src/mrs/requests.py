"""Turning "play something" into music. One path for every entry point."""

from __future__ import annotations

import re
import threading

from .config import config
from .core.extras import caster
from .core.library import library
from .core.playlists import playlists
from .core.taste import taste
from .events import Ev, bus
from .logging_setup import get
from .models import Track
from .player import player
from .resolve import parser, resolver, spotify

log = get("request")

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

        # A Spotify link is a playlist import, not a search.
        if spotify.is_spotify_url(text):
            return _import_spotify(text, announce=announce)

        # Your own playlists win over anything YouTube might suggest.
        hit = _match_playlist(text)
        if hit:
            res = player.playlist_play(hit, shuffle="shuffle" in text.lower())
            if announce and res.get("ok"):
                player.announce(res["message"])
            return {"status": "played" if res.get("ok") else "error",
                    "message": res.get("message", ""), "via": "playlist"}

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

        player.queue._set_activity("finding", plan.query)
        res = resolver.resolve(plan)
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
            player.queue.play_now(res.tracks, res.alternates, shuffle=shuffle,
                                  hold_radio=res.hold_radio, kind=plan.kind)

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


def _import_spotify(url: str, *, announce: bool = True, play: bool = True) -> dict:
    """Read a Spotify link and save it as a playlist, keeping its name.

    play=False just files it away — for when you're collecting playlists
    rather than asking for one right now.
    """
    from .core.playlists import playlists

    player.queue._set_activity("finding", "Reading Spotify link")
    bus.publish(Ev.TOAST, "Reading that Spotify link…")
    verb = "Playing" if play else "Saved"

    def work() -> None:
        names = spotify.track_names(url)
        if not names:
            bus.publish(Ev.TOAST, "Couldn't read that link — is the playlist public?")
            player.queue._set_activity("idle")
            return

        label = spotify.link_name(url) or "Spotify import"
        bus.publish(Ev.TOAST, f"{label}: {len(names)} tracks, matching them up…")

        def progress(i, total, title):
            player.queue._set_activity("finding", f"{i}/{total} {title}", i / total)

        tracks = spotify.resolve_imported(names, on_progress=progress)
        player.queue._set_activity("idle")
        if not tracks:
            bus.publish(Ev.TOAST, "None of those tracks could be found")
            return

        # keep it, so the import isn't a one-off
        playlists.create(label)
        for t in tracks:
            playlists.add(label, t)

        msg = f"{verb} {label} — {len(tracks)} tracks"
        if play:
            player.queue.play_now(tracks, hold_radio=True)
            bus.publish(Ev.TOAST, msg + " (saved as a playlist)")
            if announce:
                player.announce(msg)
        else:
            bus.publish(Ev.TOAST, msg)
            bus.publish(Ev.SETTINGS, {"playlists": True})

    threading.Thread(target=work, daemon=True).start()
    return {"status": "ok", "message": "Importing that Spotify link…",
            "via": "spotify"}


def add_spotify(url: str) -> dict:
    """Save a Spotify link as a playlist without interrupting what's on."""
    if not spotify.is_spotify_url(url):
        return {"status": "error", "message": "That isn't a Spotify link"}
    return _import_spotify(url, announce=False, play=False)


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
