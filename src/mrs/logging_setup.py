"""Logging, with rotation so the log can't grow forever."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time

from .paths import data_dir

_LOG = data_dir() / "server.log"


class _RedactKey(logging.Filter):
    """Keep the API key out of the log."""

    def __init__(self, key: str = "") -> None:
        super().__init__()
        self.key = key

    def filter(self, record: logging.LogRecord) -> bool:
        if self.key:
            if isinstance(record.msg, str) and self.key in record.msg:
                record.msg = record.msg.replace(self.key, "[REDACTED]")
            if record.args:
                try:
                    record.args = tuple(
                        a.replace(self.key, "[REDACTED]") if isinstance(a, str) else a
                        for a in record.args)
                except Exception:
                    pass
        return True


class _SafeRotating(logging.handlers.RotatingFileHandler):
    """Rotation that cannot take the log down with it.

    doRollover() closes the stream and then renames the file. On Windows
    the rename fails if anything else has that file open — a second copy of
    this program, mark()'s plain appends, the launcher's trace, a tail from
    the error dialog — and the exception leaves the stream at None with the
    rotation half done. Every later emit tries the rollover again, fails
    again, and hands the error to handleError, which this module had stubbed
    out to keep a windowed build from writing to a stderr it hasn't got.

    The result is a log that stops dead mid-run and never comes back, while
    the program carries on perfectly well and completely invisibly. That is
    not hypothetical: the log went quiet at 15:52:30 with the process still
    starting mpv engines at 15:55:44, and the same shape — log stops, program
    lives on, no explanation available afterwards — is how every crash this
    week presented.

    So rotation is best-effort and the log is not. If the rename fails, keep
    the stream open and stop trying: a log that grows past a megabyte is a
    far smaller problem than a log that stops saying anything.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except Exception as exc:
            # Half-rotated: super() closes the stream before the rename.
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    return
            # Once is enough. Retrying per line means one failed rename
            # turns into a rollover attempt for every message logged.
            self.maxBytes = 0
            try:
                self.stream.write(
                    f"{time.strftime('%H:%M:%S')} ----    log"
                    f"            couldn't rotate ({exc}); "
                    f"still writing, rotation off for this run\n")
                self.stream.flush()
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        # A stream lost to a failed rotation, or to a disk that went away
        # and came back, is worth one attempt to reopen before the line is
        # dropped. Silence is the expensive outcome here.
        if self.stream is None:
            try:
                self.stream = self._open()
            except Exception:
                return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """Never raise, never write to a stderr that isn't there — but do
        leave a mark somewhere, so a log that stops can be told apart from
        a program that stopped."""
        try:
            mark(f"log handler failed on a {record.levelname} line")
        except Exception:
            pass


def setup(api_key: str = "", level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                            datefmt="%H:%M:%S")
    # If the usual file can't be opened — another copy holding it, a rotation
    # that couldn't rename, a permissions problem — fall back to one nobody
    # else is using rather than starting up with no log at all. A boot that
    # fails silently is a boot nobody can fix, and that is exactly the boot
    # you most need to read about afterwards.
    fh = None
    for path in (_LOG, _LOG.with_name(f"server-{os.getpid()}.log")):
        try:
            fh = _SafeRotating(path, maxBytes=1_000_000, backupCount=3,
                               encoding="utf-8")
            break
        except Exception:
            continue
    if fh is not None:
        fh.setFormatter(fmt)
        fh.addFilter(_RedactKey(api_key))
        root.addHandler(fh)
    else:
        # Both files refused to open. Say so in the one place that doesn't
        # need a handler, or this run is invisible from its first line.
        mark("could not open any log file — running with no log")

    # A frozen windowed build has no console; only add one when it works.
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.addFilter(_RedactKey(api_key))
        root.addHandler(sh)

    # uvicorn's per-request access log is pure noise at 1 poll/sec.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_path():
    return _LOG


def mark(note: str) -> None:
    """Write a line straight to the log, without the logging machinery.

    Everything before setup() runs — and everything after it if the handler
    ever breaks — is invisible. A boot that fails there leaves not one line
    behind, which is how "it wouldn't start" ends up with an empty log and
    nothing to go on. This is a plain append: no handler, no formatter, no
    rotation, nothing that can be in a bad state.
    """
    line = (f"{time.strftime('%H:%M:%S')} ----    boot"
            f"           {note} (pid {os.getpid()})\n")
    # Three places, and the third is the point. The first two are the same
    # folder, which is no fallback at all when the folder is what's wrong —
    # and a copy started before sign-in bound the port and wrote nothing to
    # either of them, so the one boot that most needed explaining produced
    # no evidence anywhere. ProgramData is a genuinely different directory,
    # writable by everyone, and resolved from an environment variable this
    # one doesn't otherwise depend on.
    import pathlib
    spare = pathlib.Path(os.environ.get("ProgramData") or os.environ.get("TEMP")
                         or ".") / f"mrs-boot-{os.getpid()}.log"
    for path in (_LOG, _LOG.with_name(f"boot-{os.getpid()}.log"), spare):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            return
        except Exception:
            continue


def tail(lines: int = 12) -> str:
    """The last of the log, for saying what went wrong where it'll be read."""
    try:
        with open(_LOG, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except Exception:
        return ""


def spawn(fn, *args, name: str = "", on_error=None, **kw) -> threading.Thread:
    """Start a daemon thread whose crash gets written down.

    A bare `Thread(target=...)` that raises prints to stderr and vanishes, and
    a windowed build has no stderr — so a background worker dying is silent
    and permanent. Every symptom of it looks like something else: the queue
    stops refilling, cookies never arrive, the activity spinner sticks on
    "finding" because the line that clears it was three statements below the
    one that threw.

    `on_error` runs afterwards, for whatever state the thread was holding.
    """
    label = name or getattr(fn, "__name__", "thread")

    def run():
        try:
            fn(*args, **kw)
        except Exception as exc:
            get("tasks").exception("%s died: %s", label, exc)
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    get("tasks").debug("%s cleanup failed too", label)

    t = threading.Thread(target=run, daemon=True, name=label[:24])
    t.start()
    return t
