"""The rules this server is supposed to enforce, written down as checks.

Every one of these started life as a bug somebody found by using the thing.
That is the whole argument for the file: the clock, the cancelled search, the
capsule, the setup page — each was found by a person noticing something wrong,
which is a slow and unreliable way to find out that a link can read your
listening history.

Run with `MusicRequestServer.exe --check`, or as part of `--selftest`.

Deliberately no mpv and no network: these go through the HTTP surface with
FastAPI's TestClient, so they run in a second, need nothing installed and
can't be broken by YouTube having a bad afternoon. What they cover is the
part that has to be *right* rather than merely working — who may do what, and
whose queue a request lands in. Playback is checked by playing something.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Result:
    passed: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


class _Checker:
    """Collects failures instead of stopping at the first one.

    A run that says "these six things are wrong" is worth six runs that each
    say "this one thing is wrong".
    """

    def __init__(self, section: str = "") -> None:
        self.section = section
        self.result = Result()

    def __call__(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.result.passed += 1
        else:
            where = f"{self.section}: " if self.section else ""
            self.result.failed.append(f"{where}{name}"
                                      + (f" ({detail})" if detail else ""))
        return bool(cond)


# Routes that deliberately have no Depends guard, and why. Anything else
# turning up here is a route that shipped without one — which is exactly how
# the setup page spent months handing the master key to anyone who asked.
#
# api.py is sixteen hundred lines. It is not realistic to notice a missing
# guard by reading it, so this notices instead.
_UNGUARDED_BY_DESIGN = {
    "/api/ping":  "health check, says only that we're alive",
    "/":          "guards itself: LAN-open or the key, checked in the body",
    "/player":    "guards itself via _serve_page, which also picks the credential",
    "/remote":    "same",
    "/welcome":   "same",
    "/api/events": "calls require_key in the body — it needs the pass row "
                   "afterwards to decide whose events to send",
    "/openapi.json": "disabled",
    "/docs": "disabled",
    "/redoc": "disabled",
}


def _unguarded_routes(app) -> list[str]:
    """Routes with neither a Depends guard nor a place on the list above."""
    out = []
    for r in getattr(app, "routes", []):
        dep = getattr(r, "dependant", None)
        path = getattr(r, "path", "")
        if dep is None or not path:
            continue
        guards = {getattr(x.call, "__name__", "") for x in dep.dependencies}
        if guards & {"require_key", "require_admin"}:
            continue
        if path in _UNGUARDED_BY_DESIGN:
            continue
        for meth in sorted(getattr(r, "methods", None) or {"GET"}):
            out.append(f"{meth} {path}")
    return sorted(set(out))


def run(verbose: bool = False) -> Result:
    """Every rule, against a real app instance. Never raises."""
    from fastapi.testclient import TestClient

    from .config import config
    from .web.api import app
    from .web.security import forget_pass, issue

    out = Result()
    key = config.get("api_key") or ""
    if not key:
        out.failed.append("no api key set — nothing to check access against")
        return out

    started_with = key
    minted: list[str] = []

    def now_key() -> str:
        """Read fresh. The key is identity — a suite that caches it would
        report every symptom of it changing and never the cause."""
        return config.get("api_key") or ""

    def link(name: str, scope: str = "full", hours: int = 1) -> str:
        got = issue(now_key(), name=name, hours=hours, scope=scope)
        minted.append(got["id"])
        return got["token"]

    def say(section: str, c: _Checker) -> None:
        out.passed += c.result.passed
        out.failed += c.result.failed
        if verbose:
            n = c.result.passed
            bad = len(c.result.failed)
            mark = "ok  " if not bad else "FAIL"
            print(f"  [{mark}] {section}: {n} passed"
                  + (f", {bad} failed" if bad else ""))
            for f in c.result.failed:
                print(f"           - {f}")

    try:
        with TestClient(app) as client:
            full = link("check-full", "full")
            phone = link("check-phone", "phone")
            here = {"X-Play-Here": "1"}

            def get(path, tok=None, extra=None):
                h = dict({"X-Music-Key": now_key()} if tok is None
                         else {"X-Music-Key": tok})
                h.update(extra or {})
                return client.get(path, headers=h)

            # -- 1. everyone's listening is their own ---------------------
            c = _Checker("history")
            owner_hist = get("/api/history").json()
            c("owner sees their own recents", owner_hist.get("mine") is True)
            owner_titles = {r.get("title") for r in owner_hist.get("history") or []}
            for who, tok in (("a temporary full link", full),
                             ("a temporary phone link", phone)):
                # No profile to keep one in, so there is nothing to show.
                c(f"{who} gets no recents",
                  not get("/api/history", tok, here).json().get("history"))
                c(f"{who} gets no liked songs",
                  not get("/api/liked", tok, here).json().get("liked"))
            # A permanent link has a history — its own, and never the owner's.
            keeper = issue(now_key(), name="check-keeper", hours=0, scope="full")
            minted.append(keeper["id"])
            kt = keeper["token"]
            khist = get("/api/history", kt, here).json()
            mine_titles = {r.get("title") for r in khist.get("history") or []}
            c("a permanent link gets a history of its own",
              khist.get("mine") is True)
            c("...which is not the owner's",
              not (mine_titles & owner_titles) if owner_titles else True,
              f"overlap={sorted(mine_titles & owner_titles)[:3]}")
            c("...and liked songs of its own, empty to start",
              get("/api/liked", kt, here).json().get("liked") == [])
            say("recents and liked", c)

            # -- 1b. liking writes somewhere, and somewhere of theirs ------
            c = _Checker("liking")
            from .core.profile import profiles as _profs
            from .models import Track as _T
            kp = _profs.find(keeper["id"])
            c("a permanent link has a real taste store",
              kp is not None and kp.permanent)
            if kp:
                song = _T(video_id="LIKECHK001", title="A Song", artist="Someone")
                kp.taste.toggle_like(song)
                c("a like is remembered", kp.taste.is_liked("LIKECHK001"))
                c("...and comes back over http",
                  any(x.get("video_id") == "LIKECHK001"
                      for x in get("/api/liked", kt, here).json().get("liked", [])))
                c("...and is not in the owner's liked songs",
                  not any(x.get("video_id") == "LIKECHK001"
                          for x in get("/api/liked").json().get("liked", [])))
                kp.taste.toggle_like(song)
                c("unliking works too", not kp.taste.is_liked("LIKECHK001"))
            say("liking", c)

            # -- 2. settings are the caller's own, never the owner's -------
            c = _Checker("settings")
            cfg = get("/api/settings", full, here).json()
            leaked = [k for k in ("api_key", "library_paths", "allowed_ips",
                                  "ddns_hostname", "ddns_user", "ddns_password",
                                  "cookies_from_browser", "cookies_file",
                                  "groq_api_key", "lastfm_session", "port",
                                  "allowed_ips", "cache_size_mb")
                      if k in cfg]
            c("a link gets nothing about the machine", not leaked,
              ", ".join(leaked))
            c("...but enough to draw itself",
              cfg.get("guest") is True and "theme" in cfg)

            # The keys a guest *does* see are theirs. Prove it by moving the
            # owner's and checking the guest's stays put — sharing a name is
            # not the same as sharing a value.
            from .config import config as _cfg
            was = _cfg.get("shuffle")
            try:
                _cfg.set("shuffle", not bool(was))
                mine = get("/api/settings", full, here).json()
                c("the owner's shuffle doesn't reach a link",
                  mine.get("shuffle") is False,
                  f"owner={_cfg.get('shuffle')} guest={mine.get('shuffle')}")
            finally:
                _cfg.set("shuffle", was)
            say("settings", c)

            # -- 2b. a guest may change their own, and only their own ------
            c = _Checker("guest settings")
            r = get("/api/setting?key=artist_cohesion&value=1.7", full, here)
            c("a guest can set one of theirs", r.status_code == 200,
              f"HTTP {r.status_code}")
            c("...and it comes back changed",
              get("/api/settings", full, here).json().get("artist_cohesion") == 1.7)
            r = get("/api/setting?key=artist_cohesion&value=99", full, here)
            c("a silly value is clamped, not taken",
              r.status_code == 200 and r.json().get("value") == 2.0,
              str(r.json().get("value")))
            for machine in ("port", "api_key", "cache_size_mb", "lan_open",
                            "guest_requests_hour"):
                r = get(f"/api/setting?key={machine}&value=1", full, here)
                c(f"a guest can't set {machine}", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = get("/api/setting?key=repeat&value=sideways", full, here)
            c("a nonsense choice is refused", r.status_code == 400,
              f"HTTP {r.status_code}")
            # An eq name that isn't a preset is accepted by any plain string
            # check and then renders as an empty dropdown, which reads as
            # "the setting is broken" rather than "that isn't a thing".
            r = get("/api/setting?key=eq&value=rock", full, here)
            c("an eq that isn't a real preset is refused", r.status_code == 400,
              f"HTTP {r.status_code}")
            r = get("/api/setting?key=eq&value=bass", full, here)
            c("a real one is taken", r.status_code == 200,
              f"HTTP {r.status_code}")
            # And none of that touched the machine.
            c("the owner's config is untouched",
              _cfg.get("artist_cohesion") != 1.7,
              f"owner cohesion={_cfg.get('artist_cohesion')}")
            # What somebody borrowing the player for a night is handed. They
            # set none of it and it isn't kept, so these three defaults are
            # the whole of what an evening looks and sounds like.
            from .core.profile import GUEST_SETTINGS
            night = link("check-night", scope="phone", hours=24)
            mine = get("/api/settings", night).json()
            for key, want in (("theme", "mono"), ("eq", "flat"),
                              ("normalize", True)):
                c(f"a link for the night starts on {key}={want}",
                  mine.get(key) == want, f"got {mine.get(key)!r}")
                c(f"...and that is the default, not a saved value",
                  GUEST_SETTINGS[key] == want)
            c("a link that expires keeps nothing",
              mine.get("persistent") is False, str(mine.get("persistent")))
            say("a guest's own settings", c)

            # -- 2c. permanent remembers, temporary doesn't ----------------
            c = _Checker("persistence")
            from .core.profile import profiles
            forever = issue(now_key(), name="check-forever", hours=0, scope="full")
            minted.append(forever["id"])
            perm_tok = forever["token"]
            _sr = get("/api/setting?key=eq&value=warm", perm_tok, here)
            p = profiles.find(forever["id"])
            c("a permanent link gets a profile that persists",
              bool(p and p.permanent),
              f"profile={p!r} set-resp={_sr.status_code} {_sr.text[:80]}")
            if p:
                c("...written to disk", (p.home() / "settings.json").is_file())
                profiles.forget(forever["id"])
                again = profiles.for_row({"id": forever["id"],
                                          "name": "check-forever", "expires": 0})
                c("...and read back after a restart",
                  again.get("eq") == "warm", str(again.get("eq")))
            temp = profiles.find(minted[0])
            c("a temporary link's profile keeps nothing",
              temp is not None and not temp.permanent
              and not temp.home().exists())
            say("permanent vs temporary", c)

            # -- 2d. playlists follow the same line ------------------------
            c = _Checker("playlists")
            r = get("/api/playlists", full, here).json()
            c("a temporary link is told playlists need a permanent one",
              r.get("temporary") is True and r.get("playlists") == [])
            r = get("/api/playlist/create?name=nope", full, here)
            c("...and can't make one", r.status_code == 403,
              f"HTTP {r.status_code}")
            r = get("/api/playlist/create?name=check-list", perm_tok, here)
            c("a permanent link can", r.status_code == 200,
              f"HTTP {r.status_code}")
            names = [x["name"] for x in
                     get("/api/playlists", perm_tok, here).json().get("playlists", [])]
            c("...and sees it", "check-list" in names, str(names))
            c("...while the owner's list is untouched",
              "check-list" not in [x["name"] for x in
                                   get("/api/playlists").json().get("playlists", [])])
            profiles.wipe(forever["id"])
            profiles.wipe(keeper["id"])
            say("playlists per profile", c)

            # -- 3. the admin surface is shut ------------------------------
            c = _Checker("admin")
            for path in ("/api/token?hours=1", "/api/passes", "/api/backup",
                         "/api/lockdown?port=0", "/api/sessions",
                         "/api/blocked", "/api/port/shuffle?to=0",
                         "/api/setting?key=volume&value=70",
                         "/api/setup/state", "/api/setup/tools"):
                code = get(path, full, here).status_code
                c(f"a link is refused {path.split('?')[0]}", code == 403,
                  f"HTTP {code}")
            say("admin surface", c)

            # -- 4. the setup page does not hand out the key ---------------
            c = _Checker("setup page")
            page = get("/", full)
            c("a link can't open the setup page", page.status_code == 403,
              f"HTTP {page.status_code}")
            c("...and the key isn't in what it does return",
              now_key() not in page.text)
            bad_key = client.get("/?key=nonsense")
            c("a wrong key can't either", bad_key.status_code == 403,
              f"HTTP {bad_key.status_code}")
            c("...and leaks nothing", now_key() not in bad_key.text)
            say("setup page", c)

            # -- 5. scope: a phone link stays on its own phone -------------
            c = _Checker("scope")
            code = get("/api/audio/device?name=auto", phone, here).status_code
            c("a phone link can't move the PC's output", code == 403,
              f"HTTP {code}")
            code = get("/api/audio/device?name=auto", full).status_code
            c("a full link can", code == 200, f"HTTP {code}")
            say("scope", c)

            # -- 6. the player page hands over the right credential --------
            c = _Checker("player page")
            p = get("/player", full)
            c("a link gets the player", p.status_code == 200)
            c("...carrying its own pass, not the key",
              now_key() not in p.text and full in p.text)
            c("...and is told it's a guest", 'const GUEST = "1"' in p.text)
            mine = get("/player")
            c("the owner's page carries the key", now_key() in mine.text,
              f"HTTP {mine.status_code} {mine.text[:90]}")
            c("...and is told so", 'const GUEST = "0"' in mine.text)
            say("player page", c)

            # -- 7. revoking a link stops it dead --------------------------
            c = _Checker("revocation")
            doomed = link("check-doomed", "full")
            _r = get("/api/status", doomed, here)
            c("it works before", _r.status_code == 200,
              f"HTTP {_r.status_code} {_r.text[:90]}")
            from .web.security import revoke
            revoke(minted[-1])
            code = get("/api/status", doomed, here).status_code
            c("and not after", code == 403, f"HTTP {code}")
            say("revocation", c)

            # -- 7b. the key must not move under a running process ---------
            c = _Checker("identity")
            c("the api key is the same one this run started with",
              now_key() == started_with,
              "it changed mid-run — every token handed out is now invalid")
            say("the key holds still", c)

            # -- 7c. an import is a job, not a whim -----------------------
            c = _Checker("imports")
            from .core.queue import QueueManager, WorkItem
            from .core.sink import ListSink
            from .core.taste import NeutralTaste
            from .models import Track as _Tk

            class _Ctx:
                def build(self, *a, **k): return []
                def quick(self, *a, **k): return []

            q = QueueManager(ListSink(), _Ctx(), taste=NeutralTaste(),
                             session_id="importcheck")
            q.enqueue([_Tk(video_id="imp1", title="From a list")], imported=True)
            q.enqueue([_Tk(video_id="ord1", title="Ordinary")])
            before = q.import_era()
            q.cancel(user=False)          # a new request came in
            left = [w.track.video_id for w in q._work]
            c("a new request drops ordinary work", "ord1" not in left, str(left))
            c("...but keeps an import running", "imp1" in left, str(left))
            c("...and doesn't stop the matching", q.import_era() == before)
            q.cancel(user=True)           # the X
            c("the X drops the import too", not q._work,
              str([w.track.video_id for w in q._work]))
            c("...and stops the matching", q.import_era() != before)
            say("imports survive being superseded", c)

            # -- 8. "inside the house" must mean inside the house ----------
            c = _Checker("home")
            from .web.security import _own_wan, is_home
            wan = _own_wan()
            c("loopback is home", is_home("127.0.0.1"))
            c("the LAN is home", is_home("192.168.1.5") and is_home("10.0.0.3"))
            c("172.16-31 is home, 172.40 isn't",
              is_home("172.20.0.1") and not is_home("172.40.0.1"))
            c("the open internet is not home", not is_home("8.8.8.8"))
            c("an unknown address is not home", not is_home(""))
            if wan:
                # The one that mattered: _is_local says yes here so that a ban
                # can't lock the household out, and the open-LAN rule was
                # reading that as "help yourself to the master key".
                c("our own public address is not home", not is_home(wan), wan)
            say("what counts as home", c)

            # -- 9. no route may quietly arrive without a guard ------------
            c = _Checker("routes")
            unguarded = _unguarded_routes(app)
            c("every route is guarded, or knowingly isn't",
              not unguarded,
              "unguarded: " + ", ".join(unguarded) if unguarded else "")
            say("route inventory", c)

            # -- 9b. a session that ends says so ---------------------------
            # Both surfaces used to go on claiming somebody was there: the
            # owner's link row from a two-minute timestamp guess, and the
            # listener's own page because every event is delivered by session
            # and theirs no longer existed to send one.
            c = _Checker("session end")
            from .core.session import blank_status, sessions
            from .events import Ev, bus

            tok = link("check-ending", scope="phone", hours=1)
            pid = tok.split(".")[0]
            # /api/status is enough to open one and asks nothing of the
            # network — the suite must not go looking up songs.
            get("/api/status", tok)
            c("asking opens a session", sessions.find(pid) is not None)
            rows = get("/api/passes").json().get("passes", [])
            mine = next((r for r in rows if r["id"] == pid), None)
            c("the link says somebody is on it",
              bool(mine) and mine.get("listening") is True,
              str(mine and mine.get("listening")))

            # The bus drops everything until uvicorn binds it a loop, and
            # under TestClient there isn't one — so lend it a loop, and only
            # if it hasn't got one, in case a real server is up alongside.
            import asyncio as _aio

            lent = bus._loop is None
            loop = _aio.new_event_loop() if lent else None
            if lent:
                bus.bind_loop(loop)
            heard: list[dict] = []
            sub = bus.subscribe()
            try:
                sessions.close(pid, "revoked")
                if lent:
                    loop.run_until_complete(_aio.sleep(0))   # drain the fanout
                else:
                    time.sleep(0.25)
                while not sub.empty():
                    heard.append(sub.get_nowait())
            finally:
                bus.unsubscribe(sub)
                if lent:
                    bus.bind_loop(None)
                    loop.close()
            shut = [e for e in heard
                    if not e.get("replay")
                    and isinstance(e.get("data"), dict)
                    and e["data"].get("session") == pid
                    and e["data"].get("closed")]
            c("closing one tells the page", bool(shut),
              "nothing stamped for that session was published")
            c("...and says why", bool(shut) and shut[0]["data"].get("reason") == "revoked")
            c("...with nothing left playing",
              bool(shut) and not shut[0]["data"]["track"]["name"])

            rows = get("/api/passes").json().get("passes", [])
            mine = next((r for r in rows if r["id"] == pid), None)
            c("the link stops saying they're listening",
              bool(mine) and mine.get("listening") is False,
              "it still claims they are, off a recent timestamp")
            c("a page with no session isn't shown the owner's",
              not blank_status(pid)["track"]["name"]
              and blank_status(pid)["closed"] is False)
            say("sessions end cleanly", c)

            # -- 9d. a listener's own listening ----------------------------
            c = _Checker("listening")
            from .core.profile import Profile
            from .models import Track as _Tr

            who = Profile("check-listener", "Listener", permanent=True)
            song = _Tr(video_id="chk1", title="One", artist="A",
                       duration=200, origin="request")
            more = _Tr(video_id="chk2", title="Two", artist="B",
                       duration=200, origin="request")
            who.taste.record(song, 199, 200)
            who.taste.record(more, 199, 200)      # inside save_soon's window
            c("both plays are remembered",
              len(who.taste.recent(10)) == 2, str(who.taste.recent(10)))
            import json as _json
            f = who.home() / "taste" / "play_stats.json"
            on_disk = _json.loads(f.read_text("utf-8-sig")) if f.exists() else {}
            c("...but only one reached disk on its own",
              len(on_disk.get("recent", [])) == 1,
              "if this fails save_soon stopped throttling, which is fine — "
              "the flush below is what matters")
            who.taste.flush()
            on_disk = _json.loads(f.read_text("utf-8-sig"))
            c("flush writes what save_soon deferred",
              len(on_disk.get("recent", [])) == 2,
              str([r["title"] for r in on_disk.get("recent", [])]))
            # Taking something back out again. Dropping the row and keeping
            # the tally would mean the only visible effect of the button is
            # that the evidence goes away while the radio carries on.
            who.taste.record(_Tr(video_id="chk3", title="Three", artist="B",
                                 duration=200, origin="request"), 199, 200)
            c("the artist is a favourite to begin with",
              any(a["artist"] == "b" for a in who.taste.top_artists()),
              str(who.taste.top_artists()))
            c("forgetting one song drops just that",
              who.taste.forget(video_id="chk1")
              and [r["video_id"] for r in who.taste.recent(10)] == ["chk3", "chk2"],
              str([r["video_id"] for r in who.taste.recent(10)]))
            c("forgetting an artist takes their tally",
              who.taste.forget(artist="B")
              and not any(a["artist"] == "b" for a in who.taste.top_artists()),
              str(who.taste.top_artists()))
            c("...and their tracks with it",
              not who.taste.recent(10), str(who.taste.recent(10)))
            c("forgetting nothing says so", not who.taste.forget(video_id="nope"))
            import shutil as _sh
            _sh.rmtree(who.home(), ignore_errors=True)
            say("a listener's own listening", c)

            # -- 9c. a search result has to be the record we asked for -----
            # The genre pool took the first hit on trust. A search always
            # answers, so for a pairing YouTube hasn't got it answered with
            # whatever shared a couple of words — and nothing downstream
            # could tell, because by then the track's artist *is* whoever
            # answered. Measured: 1% wrong across rock tags, 10% across
            # worship ones, which is how a gothic rock queue filled up with
            # AI worship uploads.
            c = _Checker("pool")
            from .models import Track as _T
            from .resolve.catalog import _same_act

            same = [("Evanescence", "Evanescence"),
                    ("Evanescence", "Evanescence feat. Paul McCoy"),
                    ("Florence + the Machine", "Florence and the Machine"),
                    ("Panic! At The Disco", "Panic at the Disco"),
                    ("Sigur Rós", "Sigur Ros")]
            for want, got in same:
                c(f"{want} is still {got}", _same_act(want, _T(artist=got)))
            other = [("Lacuna Coil", "Theresa Vandermeer"),
                     ("Within Temptation", "I Needed This Dave"),
                     ("Nightwish", "Timeless Hebrew Tunes"),
                     ("Rich Dolce", "Al Stewart"),
                     ("Vanessa Carlton", "Twenty One Two"),
                     ("Hillsong United", "Hillsong Musical")]
            for want, got in other:
                c(f"{got} is not {want}", not _same_act(want, _T(artist=got)))
            c("an empty name vouches for nobody",
              not _same_act("", _T(artist="Anyone"))
              and not _same_act("Someone", _T(artist="")))
            say("the pool gets what it asked for", c)

            # -- 9e. naming the band beats matching the title --------------
            # A cover is titled exactly what the original is, so a title-first
            # ranking hands you the tribute act every time you name the band.
            c = _Checker("search order")
            from .resolve.catalog import _named_in

            def _pick(q, rows):
                return _named_in(q, [_T(title=t, artist=a, video_id=t + a)
                                     for t, a in rows])[0].artist

            for q, rows, want in (
                ("creep radiohead",
                 [("Creep", "Vintage Tribute"), ("Creep", "Radiohead")], "Radiohead"),
                ("zombie the cranberries",
                 [("Zombie", "Bad Wolves"), ("Zombie", "The Cranberries")],
                 "The Cranberries"),
                ("hurt johnny cash",
                 [("Hurt", "Nine Inch Nails"), ("Hurt", "Johnny Cash")], "Johnny Cash"),
                ("take five dave brubeck",
                 [("Take Five", "Sax Lounge"),
                  ("Take Five", "The Dave Brubeck Quartet")],
                 "The Dave Brubeck Quartet"),
            ):
                got = _pick(q, rows)
                c(f"{q!r} finds {want}", got == want, f"got {got!r}")
            # And a band whose name merely repeats the title doesn't get
            # promoted for it — whatever order the search gave is kept.
            c("a name that is only the title isn't a name",
              _pick("sweet sacrifice",
                    [("Sweet Sacrifice", "Evanescence"),
                     ("Sweet Sacrifice", "WJ & Sweet Sacrifice")]) == "Evanescence")
            say("naming the band", c)

            # -- 9g. blocking is an answer, not a nudge --------------------
            c = _Checker("blocks")
            from .core.taste import NeutralTaste as _NeutralTaste, TasteEngine
            import tempfile as _tf, pathlib as _pl, shutil as _sh2

            home = _pl.Path(_tf.mkdtemp(prefix="mrs-block-"))
            t9 = TasteEngine(root=home)
            song = _T(video_id="blk1", title="One", artist="Someone")
            other = _T(video_id="blk2", title="Two", artist="Someone")
            c("nothing is blocked to begin with", not t9.is_blocked(song))
            t9.block(track=song)
            c("a blocked song is blocked", t9.is_blocked(song))
            c("...and only that song", not t9.is_blocked(other))
            t9.block(artist="Someone")
            c("a blocked artist takes everything they did",
              t9.is_blocked(other) and t9.is_blocked(song))
            c("it survives being reloaded",
              TasteEngine(root=home).is_blocked(other))
            t9.block(artist="Someone", on=False)
            t9.block(track=song, on=False)
            c("and unblocking gives them back",
              not t9.is_blocked(song) and not t9.is_blocked(other))
            c("a link that expires blocks nothing",
              not _NeutralTaste().is_blocked(song))
            _sh2.rmtree(home, ignore_errors=True)
            say("blocking", c)

            # -- 9h. more than one thing asked for -------------------------
            c = _Checker("several")
            from .resolve.conjunction import looks_like_genre, split_seeds
            c("two artists split", split_seeds("bon jovi and guns n roses") ==
              ["bon jovi", "guns n roses"])
            c("three do too",
              len(split_seeds("evanescence and linkin park and korn")) == 3)
            c("a band with 'and' in its name doesn't",
              split_seeds("drum and bass") == ["drum and bass"])
            c("genres are recognised as genres",
              looks_like_genre("britpop") and looks_like_genre("grunge"))
            c("...and people are not",
              not looks_like_genre("bon jovi")
              and not looks_like_genre("evanescence"))
            say("more than one thing", c)

            # -- 9i. a link's queue knows what's playing -------------------
            # ListSink didn't put `current` on its entries the way mpv does,
            # and the snapshot reads it straight through — so no row in any
            # link's queue was ever marked, and "3 already played" had no
            # current track to count back from. Every session but the
            # owner's, permanent and temporary alike.
            c = _Checker("queue marking")
            from .core.sink import ListSink

            sink = ListSink()
            for name in ("a.webm", "b.webm", "c.webm"):
                sink.load(name, "append")
            pl = sink.playlist()
            c("the sink lists what it holds", len(pl) == 3, str(len(pl)))
            c("every entry says whether it's the one playing",
              all("current" in e for e in pl))
            c("and exactly one of them is",
              sum(1 for e in pl if e.get("current")) == 1,
              str([e.get("current") for e in pl]))
            c("...the one at the sink's position",
              pl[sink.pos() or 0].get("current") is True, f"pos={sink.pos()}")
            sink.advance()
            pl = sink.playlist()
            c("and it moves with it",
              pl[1].get("current") is True and not pl[0].get("current"),
              str([e.get("current") for e in pl]))
            say("a link's queue", c)

            # -- 9j. a playlist you can ask for out loud -------------------
            c = _Checker("spoken playlists")
            from .resolve.grammar import playlist_make

            for said, want in (
                ("make a 30 minute playlist with jazz blues", ("jazz blues", 30)),
                ("make a grunge playlist which is 15 minutes long", ("grunge", 15)),
                ("create a 45 min bon jovi playlist", ("bon jovi", 45)),
                ("build me an hour of shoegaze playlist", ("shoegaze", 60)),
                ("make a half an hour metal playlist", ("metal", 30)),
                ("make a two hour playlist of nirvana and soundgarden",
                 ("nirvana and soundgarden", 120)),
                ("make a playlist of nirvana and soundgarden",
                 ("nirvana and soundgarden", None)),
                ("make a playlist like bohemian rhapsody",
                 ("bohemian rhapsody", None)),
            ):
                got = playlist_make(said)
                c(f"{said!r}", got == want, f"got {got!r}")
            for said in ("play some bon jovi", "add this to my favourites",
                         "make me a coffee", "pause"):
                c(f"{said!r} isn't one", playlist_make(said) is None)

            # And the whole way through, with the search stubbed out — this
            # is about the plumbing, not about what YouTube has today.
            from . import requests as _rq
            from .core.playlists import Playlists as _Lists
            from .resolve import resolver as _rs

            real_resolve = _rs.resolve
            home = _pl.Path(_tf.mkdtemp(prefix="mrs-make-"))
            try:
                def _stub(plan):
                    hits = [_T(video_id=f"mk{i}", title=f"{plan.query} {i}",
                               artist=plan.query.title(), duration=200)
                            for i in range(5)]
                    return _rs.Resolution(hits, f"Playing {plan.query}")

                class _Ctx:
                    def build(self, current, exclude=None, exclude_keys=None,
                              limit=40, anchor=None, theme="", **kw):
                        from .models import Candidate
                        n = len(exclude or ())
                        return [Candidate(track=_T(video_id=f"r{n}-{i}",
                                                   title=f"Radio {n}-{i}",
                                                   artist="Some Band",
                                                   duration=200))
                                for i in range(12)]

                class _Q:
                    session_id = ""
                    context = _Ctx()
                    taste = _NeutralTaste()
                    def _set_activity(self, *a, **k): pass

                _rs.resolve = _stub
                lists = _Lists(home / "lists")
                q = _Q()
                got = _rq._build_playlist("make a 30 minute jazz playlist",
                                          announce=False, queue=q, room="",
                                          lists=lists)
                c("asking for one makes one", got and got["status"] == "made",
                  str(got))
                c("named after what was asked for",
                  got.get("playlist") == "Jazz", str(got.get("playlist")))
                c("and it's the length that was asked for",
                  25 <= got.get("minutes", 0) <= 34, str(got.get("minutes")))
                c("saved, not just announced",
                  "Jazz" in lists.names() and len(lists.tracks("Jazz")) > 1)
                longer = _rq._build_playlist("make a two hour jazz playlist",
                                             announce=False, queue=q, room="",
                                             lists=lists)
                c("a longer one is topped up from the radio",
                  longer.get("minutes", 0) >= 110, str(longer.get("minutes")))
                c("and doesn't overwrite the first",
                  longer.get("playlist") == "Jazz 2", str(longer.get("playlist")))
                c("an expiring link is told, not silently given the owner's",
                  (_rq._build_playlist("make a 20 minute jazz playlist",
                                       announce=False, queue=q, room="sess",
                                       lists=None) or {}).get("status") == "error")
                # And with nobody on the player page, so there's no room id
                # to give it away. That's how a temporary link's blues
                # playlist ended up in the owner's library.
                c("...even with no session open",
                  (_rq._build_playlist("make a 20 minute jazz playlist",
                                       announce=False, queue=q, room="",
                                       lists=None) or {}).get("status") == "error")
                c("the owner, who passes no library at all, writes to theirs",
                  _rq._store_for(_rq.OWN) is _rq.playlists
                  and _rq._store_for(None) is None
                  and _rq._store_for(lists) is lists)
                # One from each act in turn, not three of the first.
                mixed = _rq._spread([_T(video_id="s1", title="a", artist="One"),
                                     _T(video_id="s2", title="b", artist="One"),
                                     _T(video_id="s3", title="c", artist="Two"),
                                     _T(video_id="s4", title="d", artist="Three")])
                c("acts are dealt out, not stacked",
                  [t.artist for t in mixed] == ["One", "Two", "Three", "One"],
                  str([t.artist for t in mixed]))
                # Every jazz result being a two hour compilation must not
                # come back as a playlist with nothing in it.
                def _all_long(plan):
                    hits = [_T(video_id=f"lg{i}", title=f"{plan.query} mix {i}",
                               artist="V/A", duration=4620) for i in range(3)]
                    return _rs.Resolution(hits, f"Playing {plan.query}")

                _rs.resolve = _all_long
                long_one = _rq._build_playlist("make a 15 minute jazz playlist",
                                               announce=False, queue=q, room="",
                                               lists=lists)
                c("all-compilation results still make a playlist",
                  long_one.get("status") == "made", str(long_one))
                _rs.resolve = lambda plan: _rs.Resolution([], "nothing", error="x")
                empty = _rq._build_playlist("make a 15 minute nonsense playlist",
                                            announce=False, queue=q, room="",
                                            lists=lists)
                c("nothing found makes no playlist at all",
                  empty.get("status") == "not_found"
                  and not [n for n in lists.names() if "nonsense" in n.lower()],
                  str(empty))
                _rs.resolve = _stub
                c("an hour-long upload isn't a song",
                  _rq._too_long(_T(video_id="lng", title="best of jazz",
                                   artist="V/A", duration=4620))
                  and not _rq._too_long(_T(video_id="ok", title="Plush",
                                           artist="STP", duration=311)))
                c("and an ordinary request still isn't one",
                  _rq._build_playlist("play some jazz", announce=False,
                                      queue=q, room="", lists=lists) is None)
            finally:
                _rs.resolve = real_resolve
                _sh2.rmtree(home, ignore_errors=True)
            say("spoken playlists", c)

            # -- 9k. what a song is, past its name -------------------------
            # No network here: the parsing is what breaks, and it broke
            # twice — once stopping at a section's own first subheading, and
            # once leaving the subheadings in the text it handed back.
            c = _Checker("about")
            from .resolve import insights as _ins

            article = (
                "\"Money for Nothing\" is a song by Dire Straits.\n\n"
                "== Composition ==\n\n=== Music ===\nKnopfler came up with "
                "the riff while improvising in the studio, and the guitar "
                "sound was found by accident during the session.\n\n"
                "=== Lyrics ===\nThe words came from a man complaining about "
                "music videos in a New York appliance shop, which Knopfler "
                "wrote down there and then on a spare piece of paper.\n\n"
                "== Charts ==\nIt reached number one in the United States.\n")
            story = _ins._story_from(article)
            c("the background section is what gets read",
              "improvising in the studio" in story, story[:60])
            c("...including its subsections",
              "appliance shop" in story, story[:60])
            c("...and not the sections after it",
              "number one" not in story)
            c("the subheadings themselves don't come with it",
              "===" not in story and "Music" not in story.split("riff")[0],
              story[:40])
            c("no background section falls back to the opening",
              "is a song by Dire Straits" in
              _ins._story_from("\"X\" is a song by Dire Straits. " + "y " * 80
                               + "\n\n== Charts ==\nIt charted.\n"))
            c("nothing in, nothing out", _ins._story_from("") == "")
            c("a record with no title has no panel",
              _ins.about(None)["ready"] is False)
            c("two spellings of the same record are one entry",
              _ins._key("Bring Me To Life", "Evanescence") ==
              _ins._key("bring me to life", "evanescence"))
            # An upload's decorations sent the search off to an article about
            # a live album that merely mentions the song.
            for raw, want in (("Money For Nothing (Remastered 1996)",
                               "Money For Nothing"),
                              ("Numb [Official Music Video]", "Numb"),
                              ("Bring Me To Life - Official Video",
                               "Bring Me To Life"),
                              ("Everlong", "Everlong")):
                c(f"{raw!r} looks up as {want!r}", _ins._plain(raw) == want,
                  repr(_ins._plain(raw)))
            c("and the remaster shares the original's entry",
              _ins._key("Money For Nothing (Remastered 1996)", "Dire Straits")
              == _ins._key("money for nothing", "dire straits"))
            say("about this song", c)

            # -- 10. usage is recorded against the link --------------------
            c = _Checker("stats")
            rows = get("/api/passes").json().get("passes", [])
            row = next((r for r in rows if r["name"] == "check-full"), None)
            c("the link is listed", row is not None)
            if row:
                c("with a stats block", isinstance(row.get("stats"), dict))
                c("and a scope", row.get("scope") == "full")
            say("link stats", c)

    except Exception as exc:            # a check suite must not be the thing
        out.failed.append(f"the checks themselves broke: {exc!r}")
    finally:
        for pid in minted:
            try:
                forget_pass(pid)
            except Exception:
                pass
    return out


def main() -> int:
    print("Music Request Server - access checks")
    got = run(verbose=True)
    print()
    if got.ok:
        print(f"all {got.passed} checks passed")
        return 0
    print(f"{got.passed} passed, {len(got.failed)} FAILED:")
    for f in got.failed:
        print(f"  - {f}")
    return 1
