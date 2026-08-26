"""Where a queue's tracks actually go.

The queue engine — context, scoring, downloads, radio — is worth having once
and using everywhere. What was welded to mpv is only the last inch of it: the
playlist, the position, and whether it's paused. That's what lives here.

Two implementations:

  Speaker  the owner's. Playlist and position are mpv's, so nothing about the
           behaviour changes: this is a thin pass-through, not a reimplementation.

  Browser  a guest's. Playlist is a list and the position is an index. There
           is no clock — the phone holds that, and asks for the next track
           when its own audio ends. Trying to mirror a position the server
           can't see would be inventing a number and then defending it.

mpv's move semantics are the contract both sides implement: `move(a, b)`
takes the entry at a and puts it where b was.
"""

from __future__ import annotations

import threading


class Sink:
    """What a queue needs from whatever is playing its tracks."""

    kind = "sink"

    # -- reading --
    def playlist(self) -> list[dict]:
        raise NotImplementedError

    def pos(self) -> int | None:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def path(self) -> str:
        """The file playing right now."""
        raise NotImplementedError

    # -- changing --
    def load(self, path: str, mode: str = "append") -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def move(self, frm: int, to: int) -> None:
        raise NotImplementedError

    def remove(self, index: int) -> None:
        raise NotImplementedError

    def jump(self, index: int) -> None:
        raise NotImplementedError

    def set_paused(self, paused: bool) -> None:
        raise NotImplementedError

    def advance(self) -> None:
        """On to the next entry."""
        raise NotImplementedError


class MpvSink(Sink):
    """The owner's speakers. Every call is the command it always was."""

    kind = "speaker"

    def __init__(self, mpv) -> None:
        self.mpv = mpv

    def playlist(self) -> list[dict]:
        return self.mpv.command("get_property", "playlist") or []

    def pos(self) -> int | None:
        return self.mpv.get("playlist-pos", None)

    def count(self) -> int:
        return self.mpv.get("playlist-count", 0) or 0

    def path(self) -> str:
        return self.mpv.get("path", "") or ""

    def load(self, path: str, mode: str = "append") -> None:
        self.mpv.command("loadfile", path, mode, wait=False)

    def clear(self) -> None:
        self.mpv.command("playlist-clear", wait=False)

    def move(self, frm: int, to: int) -> None:
        self.mpv.command("playlist-move", int(frm), int(to), wait=False)

    def remove(self, index: int) -> None:
        self.mpv.command("playlist-remove", int(index), wait=False)

    def jump(self, index: int) -> None:
        self.mpv.set("playlist-pos", int(index))

    def set_paused(self, paused: bool) -> None:
        self.mpv.set("pause", bool(paused))

    def advance(self) -> None:
        self.mpv.command("playlist-next", wait=False)


class ListSink(Sink):
    """A guest's browser. The server keeps the list; the phone keeps the time.

    Deliberately does not track a position in seconds. The client reports when
    it has finished a track and asks for the next one, which survives the
    phone sleeping, the tab being backgrounded and the network dropping — none
    of which a server-side timer would.
    """

    kind = "browser"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: list[str] = []
        self._pos = -1
        self._paused = False

    def playlist(self) -> list[dict]:
        with self._lock:
            return [{"filename": p} for p in self._items]

    def pos(self) -> int | None:
        with self._lock:
            return self._pos if self._pos >= 0 else None

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def path(self) -> str:
        with self._lock:
            if 0 <= self._pos < len(self._items):
                return self._items[self._pos]
            return ""

    def load(self, path: str, mode: str = "append") -> None:
        with self._lock:
            if mode == "replace":
                self._items = [path]
                self._pos = 0
                self._paused = False
            else:
                self._items.append(path)
                if self._pos < 0:
                    self._pos = 0

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._pos = -1

    def move(self, frm: int, to: int) -> None:
        """Same contract as mpv: the entry at frm ends up where to was."""
        with self._lock:
            n = len(self._items)
            if not (0 <= frm < n) or not (0 <= to <= n) or frm == to:
                return
            item = self._items.pop(frm)
            # Removing frm shifts everything after it down by one.
            self._items.insert(to - 1 if to > frm else to, item)
            self._pos = self._track_index(self._pos, frm, to)

    @staticmethod
    def _track_index(pos: int, frm: int, to: int) -> int:
        """Where the playing entry ended up after a move."""
        if pos < 0:
            return pos
        dest = to - 1 if to > frm else to
        if pos == frm:
            return dest
        if frm < pos <= dest:
            return pos - 1
        if dest <= pos < frm:
            return pos + 1
        return pos

    def remove(self, index: int) -> None:
        with self._lock:
            if not (0 <= index < len(self._items)):
                return
            del self._items[index]
            if index < self._pos:
                self._pos -= 1
            elif index == self._pos and self._pos >= len(self._items):
                self._pos = len(self._items) - 1

    def jump(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._items):
                self._pos = index
                self._paused = False

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def advance(self) -> None:
        with self._lock:
            if self._pos + 1 < len(self._items):
                self._pos += 1
            else:
                self._pos = len(self._items) - 1
