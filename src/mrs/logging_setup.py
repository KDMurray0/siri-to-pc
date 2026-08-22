"""Logging with rotation.

The old build appended to server.log forever; it reached 3.8 MB of mostly
per-second status requests. Rotate, and drop the access-log noise.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

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


def setup(api_key: str = "", level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        _LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_RedactKey(api_key))
    root.addHandler(fh)

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
