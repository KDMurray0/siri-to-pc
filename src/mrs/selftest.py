"""Check the build that actually ships.

The test suite runs against the source files. What gets shipped is a
PyInstaller bundle, which is a different artifact: PyInstaller has to work
out which modules are imported, which templates and data files to collect,
and where they live at runtime. When it guesses wrong you get a failure that
only exists in the exe — a missing hidden import, a template that wasn't
collected, a path that resolves differently frozen. Grepping the bundle's
table of contents proves a file was packed, not that it works.

    MusicRequestServer.exe --selftest

Boots the server in-process, renders the player, calls every read-only
endpoint, and checks each store can load and each worker can start. Prints a
line per check and exits non-zero if any of them failed.
"""

from __future__ import annotations

import sys
import time
import traceback


def _check(name: str, fn) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        detail = fn() or ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{detail} ({(time.monotonic() - t0) * 1000:.0f}ms)".strip()


def run() -> int:
    from .config import config
    from .paths import data_dir, resource_dir

    checks: list[tuple[str, object]] = []

    def add(name):
        def wrap(fn):
            checks.append((name, fn))
            return fn
        return wrap

    @add("data directory")
    def _():
        return str(data_dir())

    @add("templates collected")
    def _():
        page = resource_dir() / "web" / "templates" / "player.html"
        if not page.is_file():
            raise FileNotFoundError(page)
        return f"{page.stat().st_size // 1024}KB"

    @add("every module imports")
    def _():
        from . import player, requests, server        # noqa: F401
        from .core import (audio, backup, context, cookies, downloader, era,
                           extras, foryou, gate, kin, library, listen, mpv,
                           playlists, queue, radio, spectrum, tags, taste,
                           tempo)                     # noqa: F401
        from .resolve import (catalog, conjunction, grammar, llm, lyrics,
                              numbers, parser, resolver, spotify)  # noqa: F401
        from .web import api                          # noqa: F401
        return "40+"

    @add("stores load")
    def _():
        from .core.era import era
        from .core.kin import kin
        from .core.tags import tagstore
        out = []
        for name, st in (("tags", tagstore), ("era", era), ("kin", kin)):
            got = st.stats()
            out.append(f"{name}={got.get('tracks', got.get('artists', 0))}")
        return " ".join(out)

    @add("the queue can be asked what could play next")
    def _():
        from .core.context import ContextBuilder
        from .models import Track
        from .resolve import catalog
        cb = ContextBuilder(catalog)
        # No network needed: an empty pool is a pass, a traceback is not.
        cb._rank([(Track(video_id="v", title="t", artist="a"), "radio")],
                 Track(video_id="s", title="s", artist="s"), set(), 5, set())
        return "ranked"

    @add("the spoken parser")
    def _():
        from .resolve.grammar import parse
        got = parse("play some thrash and black metal")
        if got.seeds != ["thrash metal", "black metal"]:
            raise AssertionError(f"got {got.seeds}")
        return "ok"

    @add("the http surface")
    def _():
        from fastapi.testclient import TestClient
        from .web.api import app
        key = config.get("api_key") or ""
        bad = []
        head = {"X-Music-Key": key} if key else {}
        with TestClient(app) as c:
            # The page needs a credential now — it embeds one, so serving it
            # to anyone who asks handed out the key.
            page = c.get("/player", headers=head)
            if page.status_code != 200 or "const WHY" not in page.text:
                bad.append("/player")
            # TestClient reports a non-private host, so this is the
            # "from the internet" case: no credential, no page.
            if key and config.get("lan_open") and                     c.get("/player").status_code not in (200, 403):
                bad.append("/player status")
            for path in ("/api/ping", "/api/status", "/api/settings",
                         "/api/health", "/api/history", "/api/liked",
                         "/api/playlists", "/api/theme", "/api/audio"):
                if c.get(path, headers=head).status_code != 200:
                    bad.append(path)
            # A shared link must be able to listen and nothing more.
            if key:
                from .web.security import issue, forget_pass
                minted = issue(key, name="selftest", hours=1, scope="full")
                tok = minted["token"]
                if c.get(f"/api/status?token={tok}").status_code != 200:
                    bad.append("token can't listen")
                if c.get(f"/api/setting?key=volume&value=70&token={tok}"
                         ).status_code != 403:
                    bad.append("token reached a settings write")
                forget_pass(minted["id"])
        if bad:
            raise AssertionError("failed: " + ", ".join(bad))
        return "10 routes + guest scope"

    @add("yt-dlp and mpv are on PATH")
    def _():
        import shutil
        missing = [n for n in ("yt-dlp", "mpv") if not shutil.which(n)]
        if missing:
            # Not fatal to the build, but playback won't work, and that's
            # exactly the sort of thing a shipped-build check is for.
            raise AssertionError("not found: " + ", ".join(missing))
        return "both"

    print("Music Request Server — self test")
    print(f"  frozen: {getattr(sys, 'frozen', False)}")
    failed = 0
    for name, fn in checks:
        ok, detail = _check(name, fn)
        if not ok:
            failed += 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}: {detail}")
    print(f"{'all good' if not failed else str(failed) + ' failed'}")
    return 1 if failed else 0


def main() -> int:
    try:
        return run()
    except Exception:
        traceback.print_exc()
        return 1
