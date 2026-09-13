"""Disposable, offline test process. Must be entered before app imports."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile


@contextlib.contextmanager
def isolated():
    if os.environ.get("MRS_TESTING") == "1":
        yield
        return
    if "mrs.config" in sys.modules:
        raise RuntimeError("Run checks in a fresh process, before loading the app")
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="mrs-check-") as root:
        with patch.dict(os.environ, {"MRS_TESTING": "1", "MRS_DATA_DIR": root,
                                     "MRS_CACHE_DIR": root + "/cache"}):
            yield


@contextlib.contextmanager
def offline():
    """Stub the request boundary and prohibit accidental external traffic."""
    from unittest.mock import patch
    from .web import api
    from .core.session import Session
    attempts = []
    import socket
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def blocked(*args, **kwargs):
        import traceback
        attempts.append("".join(traceback.format_stack(limit=7)))
        raise AssertionError("offline suite attempted network or player I/O")

    def fake_request(text, **kwargs):
        return {"status": "ok", "message": "Offline request accepted"}

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect(sock, address)
        return blocked(address)

    def local_connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect_ex(sock, address)
        return blocked(address)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", local_connect))
        stack.enter_context(patch("socket.socket.connect_ex", local_connect_ex))
        for target in ("urllib.request.urlopen", "requests.sessions.Session.request"):
            stack.enter_context(patch(target, side_effect=blocked))
        stack.enter_context(patch.object(api, "handle_request", fake_request))
        stack.enter_context(patch.object(Session, "start", lambda self: None))
        # The catalog's real entry points. There is no `catalog.search`:
        # patching that name crashed the whole suite, and stubbing nothing in
        # its place would have let /api/search reach YouTube and SoundCloud.
        for fn in ("related", "search_songs", "search_candidates",
                   "search_artists", "search_albums", "search_soundcloud",
                   "search_bandcamp"):
            stack.enter_context(patch(f"mrs.resolve.catalog.{fn}", return_value=[]))
        stack.enter_context(patch("mrs.core.mpv.MpvClient.command", return_value=None))
        stack.enter_context(patch("mrs.core.net.addresses", return_value={
            "addresses": [{"kind": "lan", "url": "http://127.0.0.1:7420/player"}],
            "port": 7420}))
        stack.enter_context(patch("mrs.core.tags.TagStore._ensure_worker", return_value=None))
        stack.enter_context(patch("mrs.core.era.EraStore._start", return_value=None))
        stack.enter_context(patch("mrs.core.kin.KinStore._start", return_value=None))
        yield
    if attempts:
        raise AssertionError(f"{len(attempts)} unintended network attempts: " + "\n".join(attempts))
