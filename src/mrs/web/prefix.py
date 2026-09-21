"""Serving from a path as well as from the root.

The public address is https://host/music rather than https://host:29543/, so
that another application can sit at another path on the same address. The
server doesn't move: the front of the address is peeled off before routing and
everything underneath sees the paths it always has.

Both forms are accepted, always. The tray, the launcher's own calls, an iOS
Shortcut somebody built last month and every check in this repo talk to the
bare paths and should keep working. What changes with the prefix is what the
server *says* -- redirects, the sign-in cookie's path, links it hands out, the
BASE its pages prepend to their requests -- and that only for a request that
arrived through the prefix.
"""

from __future__ import annotations

import re

from ..config import config

_OK = re.compile(r"^/[a-z0-9][a-z0-9_-]{0,31}$")


def normalize(value) -> str:
    """A prefix as it would be used: "" or "/music". Anything else is "".

    Checked rather than trusted, because it ends up inside redirects and a
    cookie path, and a value with a scheme or a second slash in it is a
    request to redirect somewhere else.
    """
    raw = str(value or "").strip().rstrip("/")
    if raw and not raw.startswith("/"):
        raw = "/" + raw
    return raw if _OK.match(raw or "") else ""


def configured() -> str:
    """The prefix the server advertises: "" or "/music"."""
    return normalize(config.get("url_prefix"))


def base_of(request) -> str:
    """The prefix this request arrived through, or "" if it arrived bare."""
    try:
        return str(request.state.base or "")
    except Exception:
        return ""


def at(request, path: str) -> str:
    """A path on this server, spelled the way this visitor reached us."""
    path = path if path.startswith("/") else "/" + path
    return base_of(request) + path


class PrefixMiddleware:
    """Peel the prefix off the path before routing sees it."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            prefix = configured()
            path = scope.get("path", "")
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                inner = path[len(prefix):] or "/"
                scope = dict(scope)
                scope["path"] = inner
                raw = scope.get("raw_path")
                if raw:
                    pre = prefix.encode()
                    if raw == pre or raw.startswith(pre + b"/") or raw.startswith(pre + b"?"):
                        scope["raw_path"] = raw[len(pre):] or b"/"
                # `state` is what request.state reads. Copied, so a request
                # can't write into the dict every other request shares.
                scope["state"] = {**(scope.get("state") or {}), "base": prefix}
        await self.app(scope, receive, send)
