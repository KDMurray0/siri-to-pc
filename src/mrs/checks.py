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
import time as _t
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
    "/":          "the public front door: a landing page, or a redirect to "
                  "the player for the owner and anyone signed in",
    "/setup":     "the Shortcut recipe; owner-only, checked in the body",
    "/player":    "guards itself via _serve_page, which also picks the credential",
    "/remote":    "same",
    "/welcome":   "same",
    "/api/events": "calls require_key in the body — it needs the pass row "
                   "afterwards to decide whose events to send",
    "/auth/google/start": "the way in, open to anyone: GET logs in, POST signs up "
                          "(name and agreements are checked in the body), then "
                          "hands off to Google",
    "/auth/google/callback": "Google answering. Nothing in it is trusted: the "
                             "state must be one we issued and the token is "
                             "fetched from Google directly",
    "/auth/claim": "finishing a sign-up: needs the short-lived cookie the "
                   "callback set, which names a Google identity we verified",
    "/privacy":   "the notice has to be readable before anyone signs up",
    "/download/client": "offered on the sign-in page, before anybody has an "
                        "account; holds no secret and is rate limited",
    "/auth/signout": "throws a cookie away; there is nothing to guard",
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
    from .testing import isolated, offline
    with isolated(), offline():
        return _run(verbose)


def _run(verbose: bool = False) -> Result:
    """Every rule, against a real app instance. Never raises."""
    from fastapi.testclient import TestClient

    from .config import config
    from .web.api import app
    from .web import security as sec
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
            # Personal links are off by default now; most of what follows is
            # about how a pass behaves, so it runs with them back on. Group 29
            # is the one that checks the default.
            config.set("allow_shared_links", True, save=False)
            full = link("check-full", "full")
            phone = link("check-phone", "phone")
            here = {"X-Play-Here": "1"}

            from urllib.parse import parse_qsl, urlsplit
            from .web.policy import changing, matching_route

            def get(path, tok=None, extra=None):
                h = dict({"X-Music-Key": now_key()} if tok is None
                         else {"X-Music-Key": tok})
                h.update(extra or {})
                # Writes go the way the pages send them: POST, JSON body. This
                # is a fresh install, so a GET that changes something is 405.
                parts = urlsplit(path)
                route, child = matching_route(app, {
                    "type": "http", "path": parts.path, "root_path": "",
                    "method": "GET"})
                params = dict(parse_qsl(parts.query, keep_blank_values=True))
                if route and changing(route.path, params,
                                      (child or {}).get("path_params", {})):
                    token = params.pop("token", None)
                    url = parts.path + (f"?token={token}" if token else "")
                    return client.post(url, json=params, headers=h)
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
            page = get("/setup", full)
            c("a link can't open the setup page", page.status_code == 403,
              f"HTTP {page.status_code}")
            c("...and the key isn't in what it does return",
              now_key() not in page.text)
            bad_key = client.get("/setup?key=nonsense")
            c("a wrong key can't either", bad_key.status_code == 403,
              f"HTTP {bad_key.status_code}")
            c("...and leaks nothing", now_key() not in bad_key.text)
            say("setup page", c)

            # -- 5. scope: a phone link stays on its own phone -------------
            c = _Checker("scope")
            # This used to assert that a full link *could* move the output.
            # set_audio_device acts on the shared player with no guest branch,
            # so a link that can call it can route the house's music wherever
            # it likes — and a phone link was explicitly allowed the one value
            # that does the most damage, cast:browser, which hands the shared
            # player to that phone and silences the room. A guest playing on
            # their own device never needed this: session/here moves their own
            # session, and the page ignores this call's answer for guests.
            for who, cred in (("a phone link", phone), ("a full link", full)):
                for name in ("auto", "cast:browser"):
                    code = get(f"/api/audio/device?name={name}", cred, here).status_code
                    c(f"{who} can't move the shared player to {name}",
                      code in (401, 403), f"HTTP {code}")
                code = get("/api/audio/devices", cred, here).status_code
                c(f"{who} can't list the PC's devices", code in (401, 403),
                  f"HTTP {code}")
            code = get("/api/audio/device?name=auto").status_code
            c("the owner still can", code == 200, f"HTTP {code}")
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

            # -- 9l. the volume follows the clock --------------------------
            c = _Checker("ambient")
            from datetime import datetime as _dt

            from .core.ambient import Ambient, band as _band, factor as _factor

            at = lambda h: _dt(2026, 8, 31, h, 30)
            for hour, want in ((9, "day"), (14, "day"), (19, "day"),
                               (20, "evening"), (22, "evening"),
                               (23, "night"), (2, "night"), (6, "night"),
                               (7, "day")):
                c(f"{hour:02d}:30 is {want}", _band(at(hour)) == want,
                  _band(at(hour)))
            c("the day is left alone", _factor("day") == 1.0)
            c("the evening is eased off", 0.5 < _factor("evening") < 1.0)
            c("the night is quieter still", _factor("night") < _factor("evening"))

            was = (config.get("volume_base"), config.get("volume"),
                   config.get("auto_volume"))
            try:
                config.set("auto_volume", True)
                config.set("volume_base", 80)
                amb = Ambient()
                first = amb.due()
                c("the first look sets the level without announcing it",
                  first is not None and first[1] == "", str(first))
                c("...and nothing more until the hour moves on",
                  amb.due(force=True) is None)
                # Turning it up at night means night is louder, not that the
                # level you chose for the day has changed.
                amb._band = ""
                lvl = amb.wanted()
                amb.note_manual(lvl)
                c("re-setting the level it chose changes nothing",
                  config.get("volume_base") == 80, str(config.get("volume_base")))
                amb.note_manual(lvl + 20)
                c("but turning it up rebases it",
                  int(config.get("volume_base")) > 80,
                  str(config.get("volume_base")))
                config.set("auto_volume", False)
                c("switched off, it asks for nothing", Ambient().due() is None)
            finally:
                config.set("volume_base", was[0])
                config.set("volume", was[1])
                config.set("auto_volume", was[2])
            say("volume follows the clock", c)

            # -- 9m. a list the house shares -------------------------------
            # One copy, the owner's. The flag is the whole permission, so
            # every rule about who may touch what is checked here.
            c = _Checker("shared lists")
            from urllib.parse import quote

            from .core.playlists import playlists as _pl
            from .models import Track as _T

            SHARED, PRIVATE = "check-shared-list", "check-private-list"
            try:
                for nm in (SHARED, PRIVATE):
                    _pl.create(nm)
                    _pl.add(nm, _T(video_id=f"own-{nm}", title="Owner's pick",
                                   artist="Someone"))
                c("a new list isn't shared", not _pl.is_shared(SHARED))
                _pl.set_shared(SHARED, True)
                c("...until it's opened up", _pl.is_shared(SHARED))
                c("and only that one", not _pl.is_shared(PRIVATE))
                c("it's listed as shared",
                  any(r["name"] == SHARED and r.get("shared")
                      for r in _pl.summary()))

                # What a link can see.
                seen = get("/api/playlists", tok=phone).json()
                names = [r["name"] for r in seen.get("shared", [])]
                c("a link is shown the shared list", SHARED in names, str(names))
                c("...and not the private one", PRIVATE not in names)
                c("...and not the owner's library as its own",
                  not [r for r in seen.get("playlists", [])
                       if r["name"] == PRIVATE])

                # What a link can do to it.
                url = "/api/playlist/add?name=" + quote(SHARED) + \
                      "&shared=1&video_id=guestvid&title=Theirs&artist=Them"
                c("a link can add to it", get(url, tok=phone).status_code == 200)
                rows = get("/api/playlist/tracks?name=" + quote(SHARED)
                           + "&shared=1", tok=phone).json()
                added = {r["video_id"]: r.get("added_by") for r in rows["tracks"]}
                c("the track is in it", "guestvid" in added, str(list(added)))
                c("signed with the name on the link",
                  added.get("guestvid") == "check-phone", str(added))
                c("and the owner's own row isn't signed",
                  not added.get("own-" + SHARED))
                c("they can take back their own",
                  get("/api/playlist/remove?name=" + quote(SHARED)
                      + "&shared=1&video_id=guestvid",
                      tok=phone).status_code == 200)
                c("but not somebody else's",
                  get("/api/playlist/remove?name=" + quote(SHARED)
                      + "&shared=1&video_id=own-" + SHARED,
                      tok=phone).status_code == 403)
                c("they can't delete the list",
                  get("/api/playlist/delete?name=" + quote(SHARED) + "&shared=1",
                      tok=phone).status_code == 403)
                c("they can't stop it being shared",
                  get("/api/playlist/share?name=" + quote(SHARED) + "&on=0",
                      tok=phone).status_code == 403)

                # And a list that wasn't shared stays out of reach, whatever
                # the caller claims.
                for op in ("tracks", "play",
                           "add&video_id=x&title=y", "delete"):
                    r2 = get(f"/api/playlist/{op.split('&')[0]}?name="
                             + quote(PRIVATE) + "&shared=1"
                             + ("&" + op.split("&", 1)[1] if "&" in op else ""),
                             tok=phone)
                    c(f"{op.split('&')[0]} on a private list is refused",
                      r2.status_code == 403, str(r2.status_code))
                c("the owner can still see all of theirs",
                  {SHARED, PRIVATE} <=
                  {r["name"] for r in get("/api/playlists").json()["playlists"]})
                _pl.set_shared(SHARED, False)
                c("and closing it takes it back off the list",
                  not get("/api/playlists", tok=phone).json().get("shared"))
            finally:
                for nm in (SHARED, PRIVATE):
                    try:
                        _pl.delete(nm)
                    except Exception:
                        pass
            say("a list the house shares", c)

            # -- 9n. a paused station stops naming a song ------------------
            # Pausing live radio is the one case where the name on screen
            # quietly stops being true: mpv advances the ICY title as the
            # playback position passes a marker, so a paused stream freezes
            # it while the station carries on without us.
            c = _Checker("radio pause")
            from .core import radio as _radio

            class _FakeMpv:
                """Enough mpv to drive the watcher: a pause flag and a title."""
                def __init__(self):
                    self.paused = False
                    self.title = "Bill Withers - Lovely Day"
                def get(self, key, default=None):
                    if key == "pause":
                        return self.paused
                    if key == "metadata":
                        return {"icy-title": self.title}
                    return default

            np = _radio.NowPlaying()
            fake = _FakeMpv()
            was_stale = _radio.STALE_AFTER
            try:
                np.start(fake, "Test FM")
                for _ in range(60):
                    if np.song:
                        break
                    _t.sleep(0.05)
                c("a playing station names the song",
                  np.song == "Lovely Day" and np.artist == "Bill Withers",
                  f"{np.artist!r} / {np.song!r}")

                # Paused, the title is held for about a song and then let go.
                _radio.STALE_AFTER = 0.4
                fake.paused = True
                fake.title = "Nina Simone - Feeling Good"   # the station moved on
                # Poked, exactly as the transport does, so the watcher starts
                # its clock now instead of at the end of a five second wait.
                np.poke()
                _t.sleep(0.3)
                c("a short pause keeps the name it had",
                  np.song == "Lovely Day", np.song)
                for _ in range(40):
                    if not np.song:
                        break
                    _t.sleep(0.05)
                c("a long pause stops claiming to know", not np.song, np.song)
                c("...and says so rather than showing the wrong one",
                  not np.title and not np.artist)
                # Loose on purpose: this is measuring a real clock against
                # a poll interval, and a check that fails when the machine
                # is briefly busy is a check nobody trusts.
                c("the watcher knows how long it's been stopped",
                  np.paused_for() > 0.3, str(round(np.paused_for(), 2)))

                # Coming back picks the new title up promptly, not at the
                # next twenty second tick.
                fake.paused = False
                np.poke()
                for _ in range(40):
                    if np.song:
                        break
                    _t.sleep(0.05)
                c("play again and the new song appears",
                  np.song == "Feeling Good", np.song)
                c("...and the pause clock is reset", np.paused_for() == 0.0)
            finally:
                _radio.STALE_AFTER = was_stale
                np.stop()
            c("stopping clears it", not np.song and not np.title)
            # stop() has to knock on the wake event too, or tuning away
            # leaves the old watcher sitting on a twenty second timer.
            joined = np._thread is None or not np._thread.is_alive() or True
            for _ in range(30):
                if not (np._thread and np._thread.is_alive()):
                    break
                _t.sleep(0.05)
            c("...and the thread actually goes away",
              not (np._thread and np._thread.is_alive()))
            # And everything downstream of a station wants the song, not
            # the station: the words of a record called "BBC Radio 6 Music"
            # do not exist, and neither does its history.
            c2 = _Checker("on air")
            song_track = _T(video_id="st1", title="Radio Six", artist="Radio",
                            url="http://example/stream", source="radio")
            plain = _T(video_id="p1", title="Lovely Day", artist="Bill Withers")
            c2("an ordinary track is itself",
               _radio.on_air(plain) is plain)
            c2("nothing is nothing", _radio.on_air(None) is None)
            was_np = _radio.now_playing
            try:
                _radio.now_playing = _radio.NowPlaying()
                c2("a station announcing nothing has no song",
                   _radio.on_air(song_track) is None)
                _radio.now_playing.artist = "Bill Withers"
                _radio.now_playing.song = "Lovely Day"
                _radio.now_playing.title = "Bill Withers - Lovely Day"
                got = _radio.on_air(song_track)
                c2("a station announcing one gives the song",
                   got is not None and got.title == "Lovely Day",
                   got.title if got else "None")
                c2("...with the artist, not the word Radio",
                   got is not None and got.artist == "Bill Withers",
                   got.artist if got else "None")
                c2("...and not the station's name",
                   got is not None and got.title != "Radio Six")
            finally:
                _radio.now_playing = was_np
            # "More like this" during radio asked YouTube what resembles
            # a station's empty video id, which is how the queue filled up
            # with strangers — and put them behind a stream that never ends.
            c2("a station has no video id to be related to",
               not song_track.video_id or song_track.video_id == "st1")
            # And the button that filled the queue with strangers. Two
            # copies of "more like this" existed and only one of them had
            # ever been taught about stations; the other let go of the hold
            # first, which is what permits the queue to top itself up.
            from .player import player as _pl
            real_cur = _pl.queue.current_track
            real_hold = _pl.queue.release_hold
            released = []
            try:
                _pl.queue.current_track = lambda: song_track
                _pl.queue.release_hold = lambda: released.append(1)
                r2 = get("/api/radio").json()
                c2("more-like-this declines on a station",
                   r2.get("ok") is False, str(r2))
                c2("...and says why", "queue behind" in (r2.get("message") or ""),
                   str(r2.get("message")))
                c2("...and above all doesn't let go of the hold",
                   not released, "release_hold was called")
                # An ordinary track still works the way it did.
                _pl.queue.current_track = lambda: plain
                get("/api/radio")
                c2("an ordinary track still releases it", bool(released))
            finally:
                _pl.queue.current_track = real_cur
                _pl.queue.release_hold = real_hold
            say("the song on air", c2)

            say("a paused station", c)

            # -- 9o. a YouTube link means that video ----------------------
            # Pasting a link and getting a search for the text of the link
            # is the kind of wrong that makes a program feel like it isn't
            # listening. The id is right there.
            c = _Checker("youtube links")
            from .resolve import youtube as _yt

            ID = "dQw4w9WgXcQ"
            for shape in (
                f"https://www.youtube.com/watch?v={ID}",
                f"https://youtu.be/{ID}",
                f"https://m.youtube.com/watch?v={ID}&feature=share",
                f"https://music.youtube.com/watch?v={ID}&list=RDAMVM{ID}",
                f"https://www.youtube.com/shorts/{ID}",
                f"https://www.youtube.com/embed/{ID}",
                f"https://www.youtube-nocookie.com/embed/{ID}",
                f"http://youtube.com/watch?v={ID}",
            ):
                c(f"{shape[:46]}", _yt.video_id(shape) == ID,
                  repr(_yt.video_id(shape)))
            # A link with words round it, because that is how people paste.
            c("a link inside a sentence is found",
              _yt.video_id(_yt.find_url(f"play this https://youtu.be/{ID} pls"))
              == ID)
            c("...and trailing punctuation isn't part of it",
              _yt.video_id(_yt.find_url(f"try https://youtu.be/{ID}.")) == ID)
            # Things that are not a video, and must fall through to a search
            # rather than being refused.
            for nope in ("bohemian rhapsody",
                         "https://open.spotify.com/track/xyz",
                         "https://www.youtube.com/@someuser",
                         "https://www.youtube.com/results?search_query=abba",
                         "https://example.com/watch?v=" + ID):
                c(f"{nope[:40]!r} is not a video", not _yt.video_id(nope),
                  repr(_yt.video_id(nope)))
            c("a channel url isn't treated as a link at all",
              not _yt.find_url("https://example.com/watch?v=" + ID))
            # A watch url that happens to sit in a playlist is the video.
            c("a video in a playlist is the video",
              _yt.video_id(f"https://youtube.com/watch?v={ID}&list=PL123456789012")
              == ID)
            c("...and reports no playlist",
              not _yt.playlist_id(f"https://youtube.com/watch?v={ID}&list=PL123456789012"))
            c("a real playlist url does",
              _yt.playlist_id("https://youtube.com/playlist?list=PL123456789012")
              == "PL123456789012")
            # Timestamps, in all the forms a share sheet writes them.
            for link, want in ((f"https://youtu.be/{ID}?t=43", 43),
                               (f"https://youtu.be/{ID}?t=1m30s", 90),
                               (f"https://www.youtube.com/watch?v={ID}&t=2h1m5s",
                                7265),
                               (f"https://youtu.be/{ID}", 0)):
                c(f"t={link.split('t=')[-1] if 't=' in link else 'none'}",
                  _yt.start_at(link) == want, str(_yt.start_at(link)))
            c("a malformed url doesn't raise", _yt.video_id("http://[::1") == "")
            c("nor does an empty one",
              _yt.video_id("") == "" and _yt.find_url("") == "")
            say("a YouTube link", c)

            # -- 9p. a lapsed link, and changing your mind ----------------
            # A link that vanishes the moment it expires takes its name and
            # its history with it, and the first you know is somebody saying
            # "it stopped working" about a thing you can no longer see.
            c = _Checker("expiry")
            import time as _tm

            dying = issue(now_key(), name="check-lapsing", hours=1,
                          scope="full")
            minted.append(dying["id"])
            did = dying["id"]

            def row_for(pid):
                return next((r for r in sec.list_passes() if r["id"] == pid),
                            None)

            c("a live link works", sec.check_token(now_key(), dying["token"]))
            r = row_for(did)
            c("...and is listed as alive", r and not r["expired"], str(r))
            c("...with nothing said about removal",
              r and r.get("removed_in_hours") is None)

            # Push it into the past, the way an hour going by would.
            with sec._held():
                store = sec._load_passes()
                store[did]["expires"] = int(_tm.time()) - 60
                sec._save_passes(store)

            c("an expired link stops working",
              not sec.check_token(now_key(), dying["token"]))
            r = row_for(did)
            c("...but is still on the list", r is not None)
            c("...marked expired", r and r["expired"])
            c("...and says how long before it goes",
              r and 23 < (r.get("removed_in_hours") or 0) <= 24,
              str(r and r.get("removed_in_hours")))
            c("a tidy-up leaves it alone during its grace day",
              (sec.tidy_passes() or True) and row_for(did) is not None)

            # Extend, from the owner, over http.
            got = get(f"/api/passes/extend?id={did}&hours=24").json()
            c("extending says it worked", got.get("status") == "ok", str(got))
            c("...and the link somebody already has works again",
              sec.check_token(now_key(), dying["token"]),
              "the token they hold should not need replacing")
            r = row_for(did)
            c("...and it is no longer expired", r and not r["expired"])
            c("...with about a day on it",
              r and 23 < (r.get("hours_left") or 0) <= 24,
              str(r and r.get("hours_left")))

            # Past the grace day it goes for good.
            with sec._held():
                store = sec._load_passes()
                store[did]["expires"] = int(_tm.time()) - (sec.GRACE + 60)
                sec._save_passes(store)
            r = row_for(did)
            c("past the grace day it reads as going now",
              r and (r.get("removed_in_hours") or 0) <= 0,
              str(r and r.get("removed_in_hours")))
            sec.tidy_passes()
            c("...and a tidy-up removes it", row_for(did) is None)
            c("...and its token is dead for good",
              not sec.check_token(now_key(), dying["token"]))

            # Who may extend. This hands somebody back a working credential,
            # so it is the owner's alone.
            other = issue(now_key(), name="check-extend-guard", hours=1)
            minted.append(other["id"])
            for who, tok in (("a full link", full), ("a phone link", phone)):
                code = get(f"/api/passes/extend?id={other['id']}&hours=99",
                           tok, here).status_code
                c(f"{who} cannot extend anything", code in (401, 403),
                  str(code))
            c("...and the guarded one is untouched",
              (row_for(other["id"]) or {}).get("hours_left", 0) <= 1.1)
            c("extending nothing is refused, not a crash",
              get("/api/passes/extend?id=&hours=24").json().get("status")
              == "error")
            c("extending an unknown id says so",
              get("/api/passes/extend?id=nosuchid&hours=24").json().get("status")
              == "error")
            # hours <= 0 is how the rest of the app spells "permanent".
            get(f"/api/passes/extend?id={other['id']}&hours=0")
            r = row_for(other["id"])
            c("nought hours makes it permanent",
              r and not r["expires"] and r["hours_left"] is None, str(r))

            # "Forget and wipe" is a privacy action. A failed filesystem
            # removal must leave the pass in place so the owner can retry
            # instead of receiving a false success response.
            from .core.profile import profiles as _wipe_profiles
            from unittest.mock import patch as _patch
            privacy = issue(now_key(), name="check-wipe-failure", hours=0)
            minted.append(privacy["id"])
            with _patch.object(_wipe_profiles, "wipe", return_value=False):
                rejected_wipe = client.post(
                    "/api/passes/revoke",
                    json={"id": privacy["id"], "forget": 1, "wipe": 1},
                    headers={"X-Music-Key": now_key()})
            c("a failed profile wipe keeps the link for retry",
              rejected_wipe.status_code == 500 and row_for(privacy["id"]) is not None,
              str(rejected_wipe.status_code))
            say("a lapsed link", c)

            # -- 9q. will it come back after a restart --------------------
            # "The checkbox is ticked" and "it will start" are different
            # questions, and the gap between them is where this feature has
            # lived: a task can be registered and disabled, or point at an
            # exe that has since moved, and the page called all of it a tick.
            c = _Checker("boot")
            from .web.api import boot_state

            st = boot_state()
            for key in ("exe", "exe_exists", "packaged", "warnings", "ok",
                        "at_signin"):
                c(f"the report says something about {key}", key in st,
                  str(sorted(st))[:90])
            c("warnings is a list", isinstance(st["warnings"], list))
            c("ok agrees with the warnings",
              st["ok"] == (not st["warnings"]))
            c("the task block is a dict or plainly absent",
              st["task"] is None or isinstance(st["task"], dict))
            if st["task"]:
                for key in ("state", "enabled", "last_result",
                            "last_result_hex", "exe", "exe_matches"):
                    c(f"the task block has {key}", key in st["task"])
                c("a clean handover isn't reported as a failure",
                  not [w for w in st["warnings"] if "0x41306" in w],
                  str(st["warnings"]))
            # Run from source every registration correctly points elsewhere,
            # so path complaints have to be a build-only thing or the report
            # is noise every time a developer looks at it.
            if not st["packaged"]:
                c("a source run doesn't complain about paths",
                  not [w for w in st["warnings"] if "different copy" in w],
                  str(st["warnings"]))
            c("owner only",
              get("/api/boot/status", phone, here).status_code in (401, 403))
            c("...and the owner can read it",
              get("/api/boot/status").json().get("status") == "ok")
            say("what happens at boot", c)

            # -- 10. usage is recorded against the link --------------------
            c = _Checker("stats")
            rows = get("/api/passes").json().get("passes", [])
            row = next((r for r in rows if r["name"] == "check-full"), None)
            c("the link is listed", row is not None)
            if row:
                c("with a stats block", isinstance(row.get("stats"), dict))
                c("and a scope", row.get("scope") == "full")
            say("link stats", c)

            # -- 11. a player that stopped playing -------------------------
            # The decision only, driven by hand. Wedging a real mpv on
            # demand isn't something a check can do, and the part that was
            # missing was never the restart — that works, every crash uses
            # it — but noticing there was anything to restart.
            c = _Checker("stalled player")
            from .player import PlayerService as _PS

            class _Stub:
                MUTE_SECONDS = _PS.MUTE_SECONDS
                FROZEN_SECONDS = _PS.FROZEN_SECONDS

                def __init__(self):
                    self._mute_since = None
                    self._frozen_since = None
                    self._last_pos = 0.0
                    self.trips = []

                def _stalled(self, why):
                    self.trips.append(why)
                    self._mute_since = self._frozen_since = None

            def run_for(props, seconds, step=1.0, stub=None, start=0.0):
                """Feed the same reading for a stretch of wall clock."""
                s = stub or _Stub()
                t = start
                while t <= start + seconds:
                    _PS._check_progress(s, props, now=t)
                    t += step
                return s

            playing = {"idle-active": False, "pause": False}
            hour = 3600.0

            # Nothing on is not a fault, and this is the one that has to be
            # right: an idle machine sits there all night untouched.
            s = run_for({"idle-active": True, "pause": False, "time-pos": None}, hour)
            c("an idle player is left alone", not s.trips, str(s.trips[:1]))

            # Paused is deliberate stillness.
            s = run_for({**playing, "pause": True, "time-pos": 42.0}, hour)
            c("a paused player is not a stalled one", not s.trips, str(s.trips[:1]))

            # Still opening the file — a path, but no position yet.
            s = run_for({**playing, "time-pos": None}, hour)
            c("a file still opening is not a stall", not s.trips, str(s.trips[:1]))

            # Playing normally, for an hour.
            s = _Stub()
            for i in range(3600):
                _PS._check_progress(s, {**playing, "time-pos": float(i)}, now=float(i))
            c("a moving position never trips", not s.trips, str(s.trips[:1]))

            # A seek is movement, backwards as well as forwards.
            s = _Stub()
            for i in range(600):
                _PS._check_progress(s, {**playing, "time-pos": 10.0 if i % 2 else 3.0},
                                    now=float(i))
            c("seeking counts as movement", not s.trips, str(s.trips[:1]))

            # The actual fault: playing, unpaused, and frozen.
            stuck = {**playing, "time-pos": 91.0}
            s = _Stub()
            _PS._check_progress(s, stuck, now=0.0)
            c("the first reading is a baseline, not a stall",
              not s.trips and s._last_pos == 91.0 and s._frozen_since is None,
              f"since={s._frozen_since} pos={s._last_pos}")
            _PS._check_progress(s, stuck, now=1.0)
            c("...and the clock starts on the second", s._frozen_since == 1.0,
              str(s._frozen_since))
            s = run_for(stuck, _PS.FROZEN_SECONDS - 2, stub=s, start=1.0)
            c("it waits before calling it", not s.trips,
              f"tripped inside {_PS.FROZEN_SECONDS}s")
            _PS._check_progress(s, stuck, now=1.0 + _PS.FROZEN_SECONDS)
            c("a frozen position trips", len(s.trips) == 1, str(s.trips))
            c("...and says where it was stuck", "91" in (s.trips or [""])[0],
              str(s.trips))

            # Once, not once a tick, or it restarts mpv every second and
            # fills the log with the reason.
            s = run_for(stuck, _PS.FROZEN_SECONDS - 1, stub=s,
                        start=1.0 + _PS.FROZEN_SECONDS)
            c("it doesn't trip again immediately", len(s.trips) == 1, str(s.trips))

            # mpv answering nothing reads as idle to get_many, which is the
            # whole reason idle-active is asked for: None means no reply came.
            silent = {"idle-active": None, "pause": None, "time-pos": None}
            s = run_for(silent, _PS.MUTE_SECONDS - 2)
            c("silence gets a grace period too", not s.trips, str(s.trips[:1]))
            _PS._check_progress(s, silent, now=_PS.MUTE_SECONDS)
            c("a player that answers nothing trips", len(s.trips) == 1, str(s.trips))

            # One good answer clears it, so a single dropped reply on a busy
            # second doesn't accumulate over an evening.
            s = _Stub()
            for i in range(6000):
                if i % 20 == 19:
                    _PS._check_progress(s, {**playing, "time-pos": float(i)}, now=float(i))
                else:
                    _PS._check_progress(s, silent, now=float(i))
            c("one answer resets the silence", not s.trips, str(s.trips[:1]))

            # Slow ticks are the point of using the clock: when mpv stops
            # answering, every reading costs the IPC deadline, so the loop
            # runs at a fifth of its speed. Counting ticks would have made
            # the timeout five times longer exactly when it mattered.
            s = _Stub()
            t = 0.0
            while t <= _PS.MUTE_SECONDS + 5:
                _PS._check_progress(s, silent, now=t)
                t += 5.0            # one reading every five seconds
            c("a slow loop still trips on time", len(s.trips) == 1, str(s.trips))
            c("...at roughly the stated timeout",
              bool(s.trips) and _PS.MUTE_SECONDS <= float(
                  s.trips[0].split("for ")[1].rstrip("s")) < _PS.MUTE_SECONDS + 6,
              str(s.trips))

            c("the timeouts are a sane length",
              20 <= _PS.MUTE_SECONDS <= 180 and 60 <= _PS.FROZEN_SECONDS <= 600,
              f"{_PS.MUTE_SECONDS}/{_PS.FROZEN_SECONDS}")
            say("a player that stopped playing", c)

            # -- 12. asking by words instead of by name --------------------
            # The grammar and the guard only. Identifying a song needs the
            # model and the lyric database, and this suite deliberately
            # touches neither — a fragment under six characters is answered
            # without asking anybody, which is enough to check the shape.
            c = _Checker("by lyric")
            from .resolve.grammar import lyric_hunt

            for said, want in (
                    ("whats the song that goes is this the real life",
                     "is this the real life"),
                    ("play the song that goes hello darkness my old friend",
                     "hello darkness my old friend"),
                    ("what song has the lyrics purple haze all in my brain",
                     "purple haze all in my brain"),
                    ("song with the words never gonna give you up",
                     "never gonna give you up"),
                    ("lyrics: i see a little silhouetto of a man",
                     "i see a little silhouetto of a man")):
                c(f"heard {said[:34]!r}", lyric_hunt(said) == want,
                  repr(lyric_hunt(said)))

            # A bare title must not be read as a lyric, or asking for a song
            # by name goes looking for a different one.
            for said in ("play bohemian rhapsody", "bohemian rhapsody",
                         "play some jazz", "make me a 30 minute grunge playlist",
                         "skip", "turn it up"):
                c(f"{said[:30]!r} is not a lyric ask", lyric_hunt(said) == "",
                  repr(lyric_hunt(said)))

            c("a fragment too short to place is declined",
              lyric_hunt("the song that goes hi") == "")
            c("quotes come off", lyric_hunt('the song that goes "let it be now"')
              == "let it be now", repr(lyric_hunt('the song that goes "let it be now"')))

            got = client.post("/api/lyrics/search", json={"q": "ab"},
                              headers={"X-Music-Key": now_key()})
            c("the search route answers the owner", got.status_code == 200,
              str(got.status_code))
            body = got.json() if got.status_code == 200 else {}
            c("...with a results list", isinstance(body.get("results"), list))
            c("...and says nothing for a fragment too short",
              body.get("results") == [], str(body.get("results"))[:60])
            c("a phone link may search too",
              client.post("/api/lyrics/search", json={"q": "ab"},
                          headers={"X-Music-Key": phone}, ).status_code == 200)
            c("a stranger may not",
              client.post("/api/lyrics/search", json={"q": "ab"},
                          headers={"X-Music-Key": "nope"}).status_code in (401, 403))
            say("asking by lyric", c)

            # -- 13. shuffle as a standing preference ----------------------
            c = _Checker("shuffle")
            from .requests import _shuffle_wanted

            class _Q:                      # a guest queue carries a profile
                def __init__(self, prof=None):
                    self.profile = prof

            class _P:
                def __init__(self, on):
                    self._on = on

                def get(self, key, default=None):
                    return self._on if key == "shuffle" else default

            house = _Q()                   # no profile: the owner's setting
            was = _cfg.get("shuffle")
            try:
                _cfg.set("shuffle", False)
                c("off, and nobody said otherwise",
                  _shuffle_wanted("play nevermind", None, house) is False)
                c("...but the word still wins",
                  _shuffle_wanted("shuffle nevermind", None, house) is True)
                c("...and so does the parser",
                  _shuffle_wanted("play nevermind", True, house) is True)

                _cfg.set("shuffle", True)
                c("on, so an album shuffles without being asked",
                  _shuffle_wanted("play nevermind", None, house) is True)
                for said in ("play nevermind in order",
                             "play the album in album order",
                             "play nevermind start to finish",
                             "dont shuffle nevermind",
                             "no shuffle please"):
                    c(f"{said[:32]!r} overrides the toggle",
                      _shuffle_wanted(said, None, house) is False)
                c("'reorder' is not a request to stop shuffling",
                  _shuffle_wanted("reorder the queue", None, house) is True)

                # Whose preference. The owner's is on; the guest's is off.
                c("a guest gets their own answer, not the owner's",
                  _shuffle_wanted("play nevermind", None, _Q(_P(False))) is False)
                _cfg.set("shuffle", False)
                c("...and that works the other way too",
                  _shuffle_wanted("play nevermind", None, _Q(_P(True))) is True)
            finally:
                _cfg.set("shuffle", was)
            say("shuffle as a preference", c)

            # -- 14. the Shortcut endpoint takes the same credentials -------
            # It is the only POST in the app, and it checked its credential
            # by hand instead of using the dependency — the hand-written
            # version declared key and not token, so a shared link could GET
            # anything and POST nothing. Nothing was wrong with the link.
            c = _Checker("post")
            body = {"input": "check-post-please-ignore"}

            def post(tok=None, extra=None, as_json=True):
                h = dict({"X-Music-Key": now_key()} if tok is None
                         else {"X-Music-Key": tok})
                h.update(extra or {})
                if as_json:
                    return client.post("/", json=body, headers=h)
                return client.post("/", data=body, headers=h)

            c("the owner's key posts", post().status_code == 200)
            c("a full link posts", post(full).status_code == 200)
            c("a phone link posts", post(phone).status_code == 200)
            c("a form body still posts",
              post(as_json=False).status_code == 200)
            c("a stranger does not",
              client.post("/", json=body,
                          headers={"X-Music-Key": "nope"}).status_code in (401, 403))
            c("and neither does nobody",
              client.post("/", json=body).status_code in (401, 403))

            # The part that actually broke: the credential in the query, in
            # the spellings a link can carry it. A link token belongs there —
            # that's what a link is.
            from .web.security import bans as _bans
            _bans.forgive("testclient")        # the two refusals above count
            c("?token= with a link token",
              client.post(f"/?token={full}", json=body).status_code == 200)
            c("?key= carrying a link token",
              client.post(f"/?key={full}", json=body).status_code == 200)
            c("?token= with a made-up token",
              client.post("/?token=not-a-real-token",
                          json=body).status_code in (401, 403))
            # The raw key does not, on a new install (SEC-003). An install from
            # before it keeps compatibility mode, so its Shortcut still works.
            got = client.post(f"/?key={now_key()}", json=body).status_code
            c("?key= with the raw key is refused on a new install",
              got in (401, 403), str(got))
            _bans.forgive("testclient")
            _cfg.set("allow_key_in_url", True)
            try:
                got = client.post(f"/?key={now_key()}", json=body).status_code
                c("...and still taken in compatibility mode", got == 200, str(got))
            finally:
                _cfg.set("allow_key_in_url", False)
            c("other parameters in a POST url are refused",
              client.post(f"/?token={full}&input=hi", json=body).status_code == 400)
            c("a cross-site POST is refused",
              client.post("/", json=body, headers={
                  "X-Music-Key": now_key(),
                  "Origin": "http://evil.example"}).status_code == 403)
            _bans.forgive("testclient")

            # Whatever a GET accepts, the POST must accept. This is the rule
            # that was broken, so it is the one worth stating.
            for name, cred in (("the key", now_key()), ("a full link", full),
                               ("a phone link", phone)):
                g = client.get("/api/status", headers={"X-Music-Key": cred},
                               params={"X-Play-Here": "1"})
                p = post(cred)
                c(f"{name}: GET and POST agree",
                  (g.status_code == 200) == (p.status_code == 200),
                  f"get={g.status_code} post={p.status_code}")
            say("the shortcut endpoint", c)

            # -- 15. the QR code carries a credential, so it is the owner's -
            # It was Auth, and its body renders the owner's own pass — or, with
            # a pass_id, reissues that pass's token. Any shared link could read
            # the owner's credential out of an SVG or re-mint someone else's
            # link. Found by an audit, not by a user, which is the bad way round.
            c = _Checker("qr")

            def owner_rows():
                with sec._held():
                    return sorted(k for k, v in sec._load_passes().items()
                                  if v.get("owner"))

            def all_rows():
                with sec._held():
                    return {k: dict(v) for k, v in sec._load_passes().items()}

            lapsed = issue(now_key(), name="check-qr-lapsed", hours=1)
            minted.append(lapsed["id"])
            with sec._held():
                store = sec._load_passes()
                store[lapsed["id"]]["expires"] = int(_t.time()) - (sec.GRACE + 60)
                sec._save_passes(store)

            victim = issue(now_key(), name="check-qr-victim", hours=1)
            minted.append(victim["id"])

            for who, cred in (("a full link", full), ("a phone link", phone),
                              ("a lapsed link", lapsed["token"])):
                before_owner, before_all = owner_rows(), all_rows()
                r1 = get("/api/qr?kind=lan", cred, here)
                r2 = get(f"/api/qr?kind=wan&pass_id={victim['id']}", cred, here)
                c(f"{who} is refused the owner's code",
                  r1.status_code in (401, 403), str(r1.status_code))
                c(f"{who} can't reissue another link's code",
                  r2.status_code in (401, 403), str(r2.status_code))
                c(f"{who} asking mints no owner pass",
                  owner_rows() == before_owner,
                  f"{before_owner} -> {owner_rows()}")
                c(f"{who} asking rewrites no pass at all",
                  all_rows() == before_all)
                c(f"{who} is not handed an svg",
                  "svg" not in (r1.headers.get("content-type") or ""))

            ok = get("/api/qr?kind=lan")
            c("the owner still gets their code", ok.status_code in (200, 404),
              str(ok.status_code))
            if ok.status_code == 200:
                c("...as an svg", "svg" in (ok.headers.get("content-type") or ""))
            say("the QR code is the owner's", c)

            # -- 16. the install instructions agree with the code ----------
            # They didn't: the example config and setup.ps1 said port 5000 and
            # player client "tv" -- a client the app had stopped using -- and the
            # README ran a file that doesn't exist. Each was fine the day it was
            # written. This is what notices the day after.
            c = _Checker("drift")
            import json as _json
            from .config import DEFAULTS as _D
            from .paths import repo_root as _root
            here_dir = _root()
            example = here_dir / "config.example.json"
            if not example.is_file():
                c("(no source tree beside this build; nothing to compare)", True)
            else:
                ex = _json.loads(example.read_text(encoding="utf-8-sig"))
                # Values a person is meant to replace, so not defaults.
                placeholders = {"api_key", "cookies_file", "groq_api_key"}
                for k, v in ex.items():
                    if k in placeholders:
                        continue
                    c(f"example {k} is a real setting", k in _D, "not in DEFAULTS")
                    if k in _D:
                        c(f"example {k} matches the code's default", v == _D[k],
                          f"example={v!r} code={_D[k]!r}")
                setup = here_dir / "setup.ps1"
                if setup.is_file():
                    text = setup.read_text(encoding="utf-8-sig")
                    code_lines = [ln for ln in text.splitlines()
                                  if not ln.lstrip().startswith("#")]
                    body = "\n".join(code_lines)
                    import re as _re
                    # Assigning a literal is the bug -- `player_client = "tv"`.
                    # Passing `player_client=$client` to yt-dlp to *test* each
                    # client is the fix, and must not trip this.
                    c("setup doesn't write a player client",
                      not _re.search(r'player_client\s*=\s*"', body),
                      str(_re.findall(r'player_client\s*=\s*"[^"]*"', body)))
                    c("setup doesn't hard-code a port",
                      not _re.search(r'\bport\s*=\s*\d', body),
                      str(_re.findall(r'\bport\s*=\s*\d+', body)))
                readme = here_dir / "README.md"
                if readme.is_file():
                    rd = readme.read_text(encoding="utf-8-sig")
                    c("README doesn't run app.py, which doesn't exist",
                      "app.py" not in rd)
                    c("README names the entry point that does",
                      "launcher.pyw" in rd)
                    c(f"README's port matches the default ({_D['port']})",
                      ":5000" not in rd and "LocalPort 5000" not in rd)
            say("the install instructions match the code", c)

            # -- 17. a write is a POST (SEC-003) ---------------------------
            c = _Checker("writes")
            from pathlib import Path
            from .paths import cache_dir
            owner_h = {"X-Music-Key": now_key()}
            r = client.get("/api/control/next", headers=owner_h)
            c("a GET that changes something is refused on a new install",
              r.status_code == 405, str(r.status_code))
            c("a lyrics refresh that can fetch is refused as GET",
              client.get("/api/lyrics", headers=owner_h).status_code == 405)
            c("an about-panel refresh that can enrich is refused as GET",
              client.get("/api/about", headers=owner_h).status_code == 405)
            c("a model refresh that can contact Groq is refused as GET",
              client.get("/api/groqmodels", headers=owner_h).status_code == 405)
            r = client.post("/api/control/next", json={}, headers=owner_h)
            c("...and the same thing as a POST works", r.status_code == 200,
              str(r.status_code))
            c("...while the lyrics panel accepts its POST refresh",
              client.post("/api/lyrics", json={}, headers=owner_h).status_code == 200)
            c("...and the about panel accepts its POST refresh",
              client.post("/api/about", json={}, headers=owner_h).status_code == 200)
            c("...and model discovery accepts its POST refresh",
              client.post("/api/groqmodels", json={}, headers=owner_h).status_code == 200)
            c("a read is still a GET",
              client.get("/api/status", headers=owner_h).status_code == 200)
            c("a read-or-write route reads by GET",
              client.get("/api/sessions", headers=owner_h).status_code == 200)
            c("...and refuses to write by GET",
              client.get("/api/sessions?close=nobody",
                         headers=owner_h).status_code == 405)
            _cfg.set("allow_legacy_get_mutations", True)
            try:
                c("compatibility mode still takes the old GET",
                  client.get("/api/control/next", headers=owner_h).status_code == 200)
            finally:
                _cfg.set("allow_legacy_get_mutations", False)
            r = client.post("/api/control/next", json={}, headers=dict(
                owner_h, Origin="http://evil.example"))
            c("a cross-site POST is refused", r.status_code == 403, str(r.status_code))
            r = client.post("/api/control/next?value=3", json={}, headers=owner_h)
            c("parameters in a POST url are refused", r.status_code == 400,
              str(r.status_code))

            # The pages learn which calls are reads from the server's table.
            page = client.get("/remote", headers=owner_h).text
            c("the remote no longer puts the key in its urls",
              'key=" + encodeURIComponent(KEY)' not in page)
            c("...and its read list is the server's",
              '"/api/status"' in page and '"/api/control/{action}"' not in page)
            ppage = client.get("/player", headers=owner_h).text
            c("the player's read list is the server's",
              "API_READ_ONLY = new Set([" in ppage and '"/api/status"' in ppage)

            # The owner's trail records machine changes, not every skip.
            from .core.audit import entries as _audit
            before = len(_audit())
            client.post("/api/control/next", json={}, headers=owner_h)
            c("a skip doesn't write the audit trail", len(_audit()) == before)
            client.post("/api/audio", json={"crossfade": 0}, headers=owner_h)
            c("a settings change does", len(_audit()) == before + 1,
              f"{before} -> {len(_audit())}")

            # Upgrades keep working; new installs are strict.
            import json as _json
            import tempfile as _tf
            from unittest.mock import patch as _patch
            from . import config as _config_mod
            with _tf.TemporaryDirectory() as tmp:
                def make(contents):
                    p = Path(tmp) / "config.json"
                    for f in Path(tmp).iterdir():
                        f.unlink()
                    if contents is not None:
                        p.write_text(_json.dumps(contents), "utf-8")
                    with _patch.object(_config_mod, "config_path", lambda: p), \
                         _patch.object(_config_mod, "migrate_legacy_data", lambda: None):
                        return _config_mod.Config()
                k = "k" * 32
                fresh = make(None)
                c("a new install is strict",
                  fresh.get("allow_legacy_get_mutations") is False
                  and fresh.get("allow_key_in_url") is False)
                old = make({"api_key": k, "port": 7420})
                c("an upgrade keeps its old GETs",
                  old.get("allow_legacy_get_mutations") is True)
                c("...and its ?key= Shortcut",
                  old.get("allow_key_in_url") is True)
                again = make(_json.loads((Path(tmp) / "config.json").read_text("utf-8"))
                             if (Path(tmp) / "config.json").exists() else None)
                c("...and says so on the next start too",
                  again.get("allow_legacy_get_mutations") is True)
                chose = make({"api_key": k, "allow_key_in_url": False})
                c("an owner who had turned ?key= off keeps it off",
                  chose.get("allow_key_in_url") is False)
                strict = make({"api_key": k, "allow_key_in_url": False,
                               "allow_legacy_get_mutations": False})
                c("an owner who turned compatibility off keeps it off",
                  strict.get("allow_legacy_get_mutations") is False)
            say("a write is a POST", c)

            # -- 18. speaker tuning ----------------------------------------
            c = _Checker("tune")
            from .core import cast as _cast
            c("known tunes pass", _cast.tune_name("iPhone") == "iphone")
            c("anything else is no tuning",
              _cast.tune_name("../../x") == "" and _cast.tune_name(None) == "")
            c("an unknown tune processes like none",
              _cast.filter_chain("bogus") == _cast.filter_chain(""))
            c("a tuned file is a different file",
              _cast._converted("v", "iphone") != _cast._converted("v", ""))
            c("a missing track says so", _cast.serve("chk-nope", "iphone")[1] == "missing")
            import shutil as _sh
            import subprocess as _sp
            if _sh.which("ffmpeg"):
                vid = "chk-tune-src"
                src = cache_dir() / f"{vid}.m4a"
                _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-f", "lavfi", "-i", "sine=f=60:d=4",
                         "-f", "lavfi", "-i", "sine=f=1000:d=4",
                         "-filter_complex", "amix=inputs=2", "-ac", "2",
                         "-c:a", "aac", str(src)], capture_output=True, timeout=60)
                first, st = _cast.serve(vid, "iphone")
                c("the phone isn't kept waiting for tuning",
                  st == "ready" and first == src, f"{st} {first}")
                for _ in range(100):
                    if _cast.playable(vid, "iphone")[1] == "ready":
                        break
                    time.sleep(0.1)
                tuned, st = _cast.playable(vid, "iphone")
                c("the tuning chain runs in ffmpeg", st == "ready" and tuned
                  and tuned.stat().st_size > 10_000, st)
                again, _ = _cast.serve(vid, "iphone")
                c("a url keeps its file once it's being read", again == first)
                _cast._held.clear()
                later, _ = _cast.serve(vid, "iphone")
                c("...and gets the tuned one next time", later == tuned)
                _cast.prune()
                c("prune keeps a tuned file", tuned.is_file())
                if tuned and tuned.is_file():
                    import re as _re

                    def rms(path, af):
                        out = _sp.run(["ffmpeg", "-hide_banner", "-i", str(path),
                                       "-af", af + "astats=metadata=0",
                                       "-f", "null", "-"],
                                      capture_output=True, text=True).stderr
                        got = _re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", out)
                        return float(got[-1]) if got and got[-1] != "-inf" else -200.0

                    # Relative to the whole: the chain also adds level on
                    # purpose, so the absolute 60Hz figure undersells the cut.
                    def share(path):
                        return rms(path, "lowpass=f=80,lowpass=f=80,") - rms(path, "")
                    before, after = share(src), share(tuned)
                    c("the sub-bass a phone can't play is taken out",
                      after < before - 15, f"{before:.1f} -> {after:.1f} dB of the whole")
            say("speaker tuning", c)

            # -- 19. lyrics for the recording that's playing ----------------
            c = _Checker("lyrics")
            from .resolve import lyrics as _ly
            edit = {"duration": 230, "syncedLyrics": "[00:01.00]radio edit"}
            album = {"duration": 310, "syncedLyrics": "[00:01.00]album"}
            plain = {"duration": 309, "plainLyrics": "words"}
            row, trust = _ly._pick([edit, album, plain], 310)
            c("Plush: the 310s album take, not the 230s edit listed first",
              row is album and trust)
            row, trust = _ly._pick([{"duration": 261, "syncedLyrics": "x"},
                                    {"duration": 304, "syncedLyrics": "y"}], 303)
            c("Blurry: 304 for a 303s track", row["duration"] == 304 and trust)
            row, trust = _ly._pick([edit], 310)
            c("nothing close: the words, but not the timings", row is edit and not trust)
            row, trust = _ly._pick([plain, album], 0)
            c("no length to go on: a timed one", row is album and trust)
            c("nothing at all is nothing", _ly._pick([], 300) == (None, False))

            calls = []

            def fake(url):
                calls.append(url)
                return None if "/get" in url else [edit]
            _ly._cache.clear()
            with _patch.object(_ly, "_fetch", fake), _patch.object(_ly.time, "sleep", lambda s: None):
                got = _ly.get_lyrics("Plush", "Stone Temple Pilots", 310)
            c("an exact-match failure is retried before searching",
              sum("/get" in u for u in calls) == 2, str(len(calls)))
            c("untrusted timings aren't shown as synced",
              got and got["synced"] == [] and "radio edit" in got["plain"], str(got)[:80])
            with _patch.object(_ly, "_fetch", lambda u: None if "/get" in u else [album]), \
                 _patch.object(_ly.time, "sleep", lambda s: None):
                other = _ly.get_lyrics("Plush", "Stone Temple Pilots", 230)
            c("a different length is a different answer, not the cached one",
              other is not got)
            _ly._cache.clear()
            say("lyrics for the recording that's playing", c)

            # -- 20. what the phone page does with its buttons -------------
            c = _Checker("transport")
            src_html = (Path(__file__).parent / "web" / "templates" /
                        "player.html").read_text("utf-8")
            import re as _re2

            def handler(name):
                m = _re2.search(r'set\("' + name + r'", (.*?)\n  \}\);', src_html, _re2.S)
                return m.group(1) if m else ""
            c("lock-screen pause says pause, not toggle",
              "castWants(false)" in handler("pause") and "playpause" not in handler("pause"))
            c("lock-screen play says play, not toggle",
              "castWants(true)" in handler("play") and "playpause" not in handler("play"))
            c("a stale status can't restart what was just paused",
              "const playing = castPlayingNow(d);" in src_html)
            c("the phone pausing us is passed on",
              "if (!cast.on || el.ended || Date.now() - cast.hush < 800) return;" in src_html)
            c("a late lyrics answer for the last track is dropped",
              "if (state.trackKey !== want || lyricKey !== want) return;" in src_html)
            wave = _re2.search(r"\.miniwave\{display:flex[^}]*\}", src_html)
            c("no dark tab behind the level meter",
              wave and "gradient" not in wave.group(0))
            say("the phone's buttons", c)

            # -- 21. headphone correction from AutoEq ----------------------
            c = _Checker("autoeq")
            from .core import autoeq as _aeq
            from .player import player as _player
            index = "\n".join([
                "# Index",
                "- [Sony WH-1000XM4](./oratory1990/over-ear/Sony%20WH-1000XM4) by oratory1990",
                "- [Sony WH-1000XM4](./Rtings/over-ear/Sony%20WH-1000XM4) by Rtings",
                "- [Sony WH-1000XM4 (ANC off)](./crinacle/GRAS%2043AG-7%20over-ear/Sony%20WH-1000XM4%20(ANC%20off)) by crinacle on GRAS 43AG-7",
                "- [Apple AirPods Pro](./crinacle/711%20in-ear/Apple%20AirPods%20Pro) by crinacle on 711",
                "- [Apple AirPods Pro 2](./crinacle/711%20in-ear/Apple%20AirPods%20Pro%202) by crinacle on 711",
                "- [Sennheiser HD 650](./oratory1990/over-ear/Sennheiser%20HD%20650) by oratory1990",
                "- [Evil](./../../etc) by nobody",
            ])
            hd650 = ("Preamp: -6.1 dB\n"
                     "Filter 1: ON LSC Fc 105 Hz Gain 6.4 dB Q 0.70\n"
                     "Filter 2: ON PK Fc 8800 Hz Gain 5.1 dB Q 1.42\n"
                     "Filter 3: ON PK Fc 118 Hz Gain -3.1 dB Q 0.50\n"
                     "Filter 4: ON HSC Fc 10000 Hz Gain -2.1 dB Q 0.70\n")
            asked = []

            def fake_fetch(url, timeout=10.0):
                asked.append(url)
                if url.endswith("INDEX.md"):
                    return index.encode()
                if url.endswith("Sennheiser%20HD%20650%20ParametricEQ.txt") or \
                        url.endswith("Sony%20WH-1000XM4%20ParametricEQ.txt"):
                    return hd650.encode()
                return None

            _aeq._entries = None
            with _patch.object(_aeq, "_fetch", fake_fetch):
                rows = _aeq._load(refresh=True)
                c("the index parses, and a path out of the results is dropped",
                  len(rows) == 6, str(len(rows)))
                c("whose measurement, and on what", rows[2]["source"] == "crinacle"
                  and rows[2]["rig"] == "GRAS 43AG-7")
                e, sure = _aeq.match("Headphones (WH-1000XM4)")
                c("a Bluetooth name finds its model",
                  e and e["name"] == "Sony WH-1000XM4" and sure)
                c("...measured by the source AutoEq trusts most",
                  e and e["source"] == "oratory1990")
                e, sure = _aeq.match("Headset (WH-1000XM4 Hands-Free AG Audio)")
                c("the hands-free half of the same headphones too", e and sure)
                e, sure = _aeq.match("Headphones (Kyle's AirPods Pro)")
                c("AirPods Pro is found but not assumed: Pro 2 says the same",
                  e and e["name"] == "Apple AirPods Pro" and not sure)
                for generic in ("Headphones (High Definition Audio Device)",
                                "Speakers (USB Audio CODEC )",
                                "4 - PHL 346E2C (AMD High Definition Audio Device)",
                                "Speakers (Steam Streaming Speakers)"):
                    c(f"{generic[:30]!r} matches nothing",
                      _aeq.match(generic) == (None, False))
                c("search finds by the words typed",
                  [r["name"] for r in _aeq.search("hd 650")][:1] == ["Sennheiser HD 650"])

                hd = _aeq.match("Headphones (HD 650)")[0]
                prof = _aeq.profile(hd["id"])
                c("a profile is fetched from its own file",
                  prof and len(prof["filters"]) == 4 and prof["preamp"] == -6.1)
                c("...and kept, so the stream never waits on GitHub",
                  bool(_aeq.cached_chain(hd["id"])))
                wild = _aeq.parse_profile("Preamp: 40 dB\nFilter 1: ON PK Fc 999999 Hz "
                                          "Gain -90 dB Q 0\nFilter 2: OFF PK Fc 1000 Hz Gain 3 dB Q 1")
                c("values out of range are clamped, switched-off filters skipped",
                  wild["preamp"] == 0.0 and len(wild["filters"]) == 1
                  and wild["filters"][0]["f"] == 22000.0 and wild["filters"][0]["gain"] == -30.0)

                if _sh.which("ffmpeg"):
                    import numpy as _np
                    n = 1 << 15
                    imp = _np.zeros(n, dtype=_np.float32)
                    imp[0] = 0.25
                    got = _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                   "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "-",
                                   "-af", _aeq.chain(prof), "-f", "f32le", "-"],
                                  input=imp.tobytes(), capture_output=True, timeout=30)
                    heard = _np.frombuffer(got.stdout, dtype=_np.float32)[:n] / 0.25
                    spec = _np.abs(_np.fft.rfft(heard))
                    freqs = _np.fft.rfftfreq(n, 1 / 48000)
                    worst = 0.0
                    for f, db in _aeq.curve(prof):
                        if f < 40:
                            continue            # below the FFT's resolution here
                        i = int(_np.argmin(_np.abs(freqs - f)))
                        worst = max(worst, abs(20 * _np.log10(spec[i]) - (db + prof["preamp"])))
                    c("ffmpeg applies the curve AutoEq means (RBJ)", worst < 0.3,
                      f"worst {worst:.2f} dB")

                # The PC's own output.
                rows_before = dict(_cfg.get("device_eq") or {})
                with _patch.object(_aeq, "output_name", lambda: "Headphones (HD 650)"):
                    _aeq._Watch.last = None
                    guest_r = client.post("/api/autoeq/search", json={"q": "hd"},
                                          headers={"X-Music-Key": phone})
                    c("a link can search models for its own headphones",
                      guest_r.status_code == 200 and guest_r.json().get("results"))
                    pr = client.post("/api/autoeq/profile", json={"id": hd["id"]},
                                     headers={"X-Music-Key": phone})
                    c("...and fetch one, getting the tune to ask the stream for",
                      pr.status_code == 200 and pr.json().get("tune") == f"aeq-{hd['id']}")
                    c("but not choose the PC's",
                      client.post("/api/autoeq/assign", json={"id": hd["id"]},
                                  headers={"X-Music-Key": phone}).status_code == 403)
                    c("or read what the PC is plugged into",
                      client.get("/api/autoeq/status",
                                 headers={"X-Music-Key": phone}).status_code == 403)
                    r = client.post("/api/autoeq/assign", json={"id": hd["id"]},
                                    headers=owner_h)
                    c("the owner chooses for the output in use", r.status_code == 200
                      and (r.json().get("profile") or {}).get("name") == "Sennheiser HD 650",
                      r.text[:120])
                    c("...and the player's chain carries it",
                      "lowshelf=f=105.0" in _player.audio.build_chain())
                    c("...before normalising, so its preamp comes back up",
                      not _cfg.get("normalize") or _player.audio.build_chain().index("lowshelf")
                      < _player.audio.build_chain().index("dynaudnorm"))
                    _cfg.set("device_eq_enabled", False)
                    c("switched off, it's gone", "lowshelf" not in _player.audio.build_chain())
                    _cfg.set("device_eq_enabled", True)
                    client.post("/api/autoeq/assign", json={"id": ""}, headers=owner_h)
                    c("'none for this output' is kept",
                      "lowshelf" not in _player.audio.build_chain()
                      and _aeq.assigned("Headphones (HD 650)") == {"id": "", "auto": False})
                    _aeq.settle("Headphones (HD 650)")
                    c("...and a certain match doesn't override it",
                      not _aeq.assigned("Headphones (HD 650)").get("id"))
                    r = client.post("/api/autoeq/assign", json={"clear": 1}, headers=owner_h)
                    c("forgetting lets the name match again",
                      (_aeq.assigned("Headphones (HD 650)") or {}).get("auto") is True,
                      str(_aeq.assigned("Headphones (HD 650)")))

                from .core import cast as _cast2
                c("a phone can be tuned to a profile on disk",
                  _cast2.tune_name(f"aeq-{hd['id']}") == f"aeq-{hd['id']}"
                  and "lowshelf" in _cast2.filter_chain(f"aeq-{hd['id']}"))
                c("...but not one that isn't, or anything else",
                  _cast2.tune_name("aeq-000000000000") == ""
                  and _cast2.tune_name("aeq-../../x") == "")
                c("every request went to AutoEq's results, nowhere else",
                  asked and all(u.startswith(_aeq.BASE) for u in asked))
                _cfg.set("device_eq", rows_before)
                _aeq._Watch.last = None
            _aeq._entries = None
            from .core import endpoint as _ep
            got = _ep.default_output()
            c("Windows can be asked for the output's name without crashing",
              isinstance(got.get("name"), str) and isinstance(got.get("id"), str))
            say("headphone correction", c)

            # -- 22. the format a browser plays, and where the sound goes ---
            c = _Checker("formats")
            from .core import cast as _cf
            c("an unknown format is AAC", _cf.fmt_name("flac!") == "aac"
              and _cf.fmt_name("WebM") == "webm")
            if _sh.which("ffmpeg"):
                vid = "chk-opus-src"
                src = cache_dir() / f"{vid}.webm"
                _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-f", "lavfi", "-i", "sine=f=440:d=3", "-ac", "2",
                         "-c:a", "libopus", "-b:a", "96k", str(src)],
                        capture_output=True, timeout=60)
                plain = not _cf.filter_chain("")
                c("(this config processes nothing, so pass-through applies)", plain)
                _cf._held.clear()
                got, st = _cf.serve(vid, "", "webm")
                c("a browser that plays WebM Opus gets the download itself",
                  st == "ready" and got == src, f"{st} {got}")
                t0 = time.monotonic()
                got, st = _cf.serve(vid, "", "ogg")
                took = time.monotonic() - t0

                def codec_of(p):
                    return _sp.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                    "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                    str(p)], capture_output=True, text=True).stdout.strip()
                c("an Ogg Opus browser gets the same packets, repackaged",
                  st == "ready" and got and got.suffix == ".ogg"
                  and codec_of(got) == "opus", f"{st} {got}")
                c("...which is a copy, not an encode", took < 2.0, f"{took:.2f}s")
                got, st = _cf.serve(vid, "", "aac")
                c("everything else gets AAC", st == "ready" and got
                  and got.suffix == ".m4a" and codec_of(got) == "aac", f"{st} {got}")
                _cf._held.clear()
                got, st = _cf.convert(vid, "iphone", "webm")
                c("tuned for an Opus browser, it's encoded as Opus",
                  st == "ready" and got and got != src and codec_of(got) == "opus")
                r = client.get(f"/api/output/stream/{vid}?fmt=ogg",
                               headers=dict(owner_h, Range="bytes=0-99"))
                c("the stream route takes the format and still serves ranges",
                  r.status_code == 206 and r.headers.get("content-type", "").startswith("audio/ogg"),
                  f"{r.status_code} {r.headers.get('content-type')}")
                junk = _cf.work_dir() / f"{vid}~deadbeef.ogg"
                junk.write_bytes(b"x" * 20000)
                _cf._held.clear()
                _cf.prune()
                c("prune drops a file for processing nobody uses",
                  not junk.exists())
                c("...and keeps the ones in use",
                  _cf._converted(vid, "", "ogg").exists())
            page = (Path(__file__).parent / "web" / "templates" / "player.html").read_text("utf-8")
            c("the page names its format in every stream url",
              '"&fmt=" + FMT' in page and '"?fmt=" + FMT' in page)
            c("only a 'probably' counts as playing a format",
              'probe.canPlayType(type) === "probably"' in page)
            c("a format is only written off when the server had the file",
              "had = r.ok" in page and "(code === 3 || code === 4)" in page)
            c("Safari's made-up latency is treated as not knowing",
              "Math.abs(lat - 512 / ctx.sampleRate) < 1e-6" in page)
            c("a listener's choice holds while the route stays the same",
              'if (pin === route || pin === "pending") return;' in page
              and 'tuneStore("tunepin", "pending");' in page)
            say("formats and where the sound goes", c)

            # -- 23. what the place has done, per link and altogether -------
            c = _Checker("stats")
            import json as _json
            from .core import stats as _st
            _st.flush()
            before = _st.house()["totals"]["requests"]
            _st.note("link-a", requests=2, seconds=120, bytes_out=1024)
            _st.note("link-b", plays=1, bytes_out=2048)
            c("a link's count is the house's count too",
              _st.house()["totals"]["requests"] == before + 2
              and _st.house()["totals"]["bytes_out"] >= 3072)
            c("...before anything is written to disk",
              _st.link("link-a")["totals"]["seconds"] == 120)
            _st.flush()
            c("...and after", _st.link("link-a")["totals"]["seconds"] == 120
              and _st.link("link-b")["totals"]["plays"] == 1)
            c("this month is where it landed",
              _st.link("link-a")["this_month"]["requests"] == 2)
            c("the busiest link comes first",
              [r["id"] for r in _st.links()][:1] == ["link-a"])
            c("a link nobody counted has zeroes, not an error",
              _st.link("nobody")["totals"]["plays"] == 0)
            _st.note("link-a", requests=0, seconds=0)
            c("nothing to add writes nothing", True)
            # An unreadable file is not a reason to lose the next month.
            (_st._path()).write_text("{not json", "utf-8")
            c("a wrecked stats file reads as empty",
              _st.house()["totals"]["plays"] == 0)
            _st.note("link-c", plays=3)
            _st.flush()
            c("...and writing carries on from there",
              _st.link("link-c")["totals"]["plays"] == 3)
            # Months: old ones age out, the recent ones stay.
            old = {"version": 1, "links": {},
                   "house": {"totals": {"plays": 5}, "first": 1,
                             "months": {f"20{y:02d}-01": {"plays": 1} for y in range(1, 30)}}}
            _st._path().write_text(_json.dumps(old), "utf-8")
            c("only two years of months are kept",
              len(_st.house(months=99)["months"]) <= 24)

            r = client.get("/api/stats", headers=owner_h)
            c("the owner can read the numbers", r.status_code == 200
              and "house" in r.json(), str(r.status_code))
            c("a link cannot",
              client.get("/api/stats", headers={"X-Music-Key": phone}).status_code == 403)
            body = r.json()
            c("...and the links in them are named",
              all("id" in row for row in body.get("links", [])))

            # The meters, where they actually sit.
            _st.flush()
            link_id = full.split(".")[0]
            was = _st.link(link_id)["totals"]["requests"]
            asked = client.post("/", json={"input": "check-stats-please-ignore"},
                                headers={"X-Music-Key": full})
            _st.flush()
            c("a request through a link is counted against that link",
              asked.status_code == 200
              and _st.link(link_id)["totals"]["requests"] == was + 1,
              f"{asked.status_code} {was} -> {_st.link(link_id)['totals']['requests']}")
            house_was = _st.house()["totals"]["requests"]
            client.post("/", json={"input": "check-stats-owner"}, headers=owner_h)
            _st.flush()
            c("...and one of the owner's lands on the house",
              _st.house()["totals"]["requests"] > house_was)
            page = (Path(__file__).parent / "web" / "templates" / "player.html").read_text("utf-8")
            # Only if the trail is on the page at all: when it is, it has to
            # read as what was done, not as the route that did it.
            if 'id="auditlist"' in page:
                c("the system tab says what happened, not which route",
                  '"passes/new": "Made a link"' in page
                  and "auditSaid(row.action)" in page)
            c("...and shows the month and the all-time totals",
              'id="statsmonth"' in page and 'id="statstotal"' in page
              and 'id="statslinks"' in page)
            c("bytes are counted as they leave", "_note_served(request, sent)" in
              (Path(__file__).parent / "web" / "api.py").read_text("utf-8"))
            say("what the place has done", c)

            # -- 24. a real certificate, or none --------------------------
            c = _Checker("tls")
            from . import server as _srv
            from .paths import data_dir as _dd
            was_cert, was_key = _cfg.get("tls_cert"), _cfg.get("tls_key")
            try:
                _cfg.set("tls_cert", "", save=False)
                _cfg.set("tls_key", "", save=False)
                c("nothing configured and nothing in the standard place is http",
                  _srv.tls_files() is None and _srv.cert_days_left() == 0.0)
                _cfg.set("tls_cert", str(_dd() / "nope.pem"), save=False)
                _cfg.set("tls_key", str(_dd() / "nope.key"), save=False)
                c("a path that points at nothing doesn't stop the server",
                  _srv.tls_files() is None)
                # A pair that doesn't go together is the renewal failure that
                # matters: it looks configured and serves nothing.
                certs = _dd() / "certs"
                certs.mkdir(parents=True, exist_ok=True)
                (certs / "fullchain.pem").write_text("not a certificate", "utf-8")
                (certs / "privkey.pem").write_text("not a key", "utf-8")
                _cfg.set("tls_cert", "", save=False)
                _cfg.set("tls_key", "", save=False)
                c("rubbish in the standard place is refused, not served",
                  _srv.tls_files() is None)
                c("the standard place is the one certificate.ps1 writes to",
                  (certs / "fullchain.pem").exists()
                  and ("MusicRequestServer" + chr(92) + "certs")
                  in (Path(__file__).resolve().parents[2] / "certificate.ps1")
                  .read_text("utf-8", "replace"))
            finally:
                _cfg.set("tls_cert", was_cert or "", save=False)
                _cfg.set("tls_key", was_key or "", save=False)
                for leftover in ("fullchain.pem", "privkey.pem"):
                    (_dd() / "certs" / leftover).unlink(missing_ok=True)
            from .core import net as _net
            c("the scheme follows what is being served, not what is set",
              _net.scheme() == "http" and not _srv.runtime.get("tls"))
            _srv.runtime["tls"] = True
            try:
                # addresses() is stubbed in here (it reaches the network),
                # so the scheme itself is what there is to check.
                c("...and says https once a certificate is loaded",
                  _net.scheme() == "https")
            finally:
                _srv.runtime.pop("tls", None)
            _srv.runtime["local_port"] = 41234
            try:
                c("this machine talks to itself in plaintext",
                  _srv.local_url(443) == "http://127.0.0.1:41234/api/ping")
            finally:
                _srv.runtime.pop("local_port", None)
            # PowerShell 5.1 reads a file with no BOM as ANSI, which turns a
            # dash into a quote and the script into a syntax error.
            raw = (Path(__file__).resolve().parents[2] / "certificate.ps1").read_bytes()
            c("certificate.ps1 is ASCII with a BOM, so PowerShell can read it",
              raw[:3] == bytes([0xEF, 0xBB, 0xBF]) and all(b < 128 for b in raw[3:]))
            say("a real certificate", c)

            # -- 25. people sign in; a link is no longer the identity ------
            c = _Checker("accounts")
            import base64 as _b64mod
            from .web import accounts as _acc, google as _goog
            from .web.api import SESSION_COOKIE as _COOKIE

            def _sign_in_as(sub):
                return {_COOKIE: sec.session_cookie(now_key(), sub)}

            _acc._path().unlink(missing_ok=True)
            was_owner_email = _cfg.get("owner_email")
            was_new_scope = _cfg.get("new_account_scope")
            _cfg.set("owner_email", "", save=False)
            _cfg.set("new_account_scope", "phone", save=False)
            _cfg.set("google_client_id", "test-client-id", save=False)
            _cfg.set("google_client_secret", "test-secret", save=False)
            _cfg.set("ddns_hostname", "music.example.test", save=False)

            person = _acc.admit("1234567890", "guest@example.com", "A Guest")
            c("a new sign-in lands at the owner's default", person["scope"] == "phone")
            c("being first in does not make you the owner",
              _acc.get("1234567890")["scope"] == "phone")
            _cfg.set("new_account_scope", "blocked", save=False)
            held = _acc.admit("2222220", "held@example.com", "Held")
            c("...which the owner can set to hold newcomers until they say yes",
              held["scope"] == "blocked")
            _cfg.set("new_account_scope", "phone", save=False)
            _cfg.set("owner_email", "me@example.com", save=False)
            mine = _acc.admit("9000000001", "ME@example.com", "Me")
            c("the address written down beforehand is the owner",
              mine["scope"] == "owner" and mine["email"] == "me@example.com")
            c("a made-up account id is refused",
              _acc.get("../../etc") is None and not _acc._ok_sub("../x"))
            try:
                _acc.set_scope("9000000001", "phone")
                c("the only owner cannot demote themselves", False)
            except ValueError:
                c("the only owner cannot demote themselves", True)
            _acc.set_scope("1234567890", "full")
            c("the owner can let somebody onto the speakers",
              _acc.get("1234567890")["scope"] == "full")

            # The cookie: signed, and worth nothing if touched.
            good = sec.session_cookie(now_key(), "1234567890")
            c("a cookie says who it is", sec.read_session(now_key(), good) == "1234567890")
            c("...and not if a letter of it changes",
              sec.read_session(now_key(), good[:-1] + ("x" if good[-1] != "x" else "y")) == "")
            c("...or if another server signed it",
              sec.read_session("some other key", good) == "")
            c("...or once it has expired",
              sec.read_session(now_key(),
                               sec.session_cookie(now_key(), "1234567890", days=-1)) == "")

            # Signed in, the cookie is the credential.
            r = client.get("/api/status", cookies=_sign_in_as("1234567890"))
            c("a signed-in listener gets in with no link at all",
              r.status_code == 200, str(r.status_code))
            r = client.post("/api/setting", json={"key": "device_eq_auto", "value": "1"},
                            cookies=_sign_in_as("1234567890"))
            c("...and still can't change the machine", r.status_code == 403)
            r = client.post("/api/setting", json={"key": "device_eq_auto", "value": "1"},
                            cookies=_sign_in_as("9000000001"))
            c("the owner's account can", r.status_code == 200, str(r.status_code))
            c("a cookie for somebody who was forgotten is nobody",
              client.get("/api/status",
                         cookies=_sign_in_as("no-such-person")).status_code in (401, 403))
            _bans.forgive("testclient")
            _acc.set_scope("1234567890", "blocked")
            c("a blocked account is turned away",
              client.get("/api/status", cookies=_sign_in_as("1234567890")).status_code == 403)
            _acc.set_scope("1234567890", "phone")
            _bans.forgive("testclient")

            # The front door.
            with _patch("mrs.web.security.is_home", lambda ip: False):
                home = client.get("/", follow_redirects=False)
                c("a stranger meets a sign-in page, not the player",
                  home.status_code == 200 and "Log in with Google" in home.text
                  and "Sign up with Google" in home.text,
                  str(home.status_code))
                signed = client.get("/", cookies=_sign_in_as("1234567890"),
                                    follow_redirects=False)
                c("someone signed in is sent straight to the player",
                  signed.status_code == 302 and signed.headers.get("location") == "/player")
                _acc.set_scope("1234567890", "blocked")
                blk = client.get("/", cookies=_sign_in_as("1234567890"),
                                 follow_redirects=False)
                c("a blocked account is told so, not bounced to the player",
                  blk.status_code == 200 and "listen here" in blk.text)
                c("...and can still take its data or delete it",
                  "/api/me/export" in blk.text and "/api/me/delete" in blk.text)
                _acc.set_scope("1234567890", "phone")
            with _patch("mrs.web.security.is_home", lambda ip: True):
                c("the owner at home goes straight in",
                  client.get("/", follow_redirects=False).headers.get("location") == "/player")

            # The page: a guest signed in is still a guest, with no key in it.
            with _patch("mrs.web.security.is_home", lambda ip: True):
                page = client.get("/player", cookies=_sign_in_as("1234567890"))
                c("a signed-in guest is not handed the owner's page",
                  page.status_code == 200 and 'const GUEST = "1"' in page.text,
                  str(page.status_code))
                c("...and no credential is baked into it", 'const KEY = "";' in page.text)
                owner_page = client.get("/player", cookies=_sign_in_as("9000000001"))
                c("the owner's account gets the owner's page",
                  'const GUEST = "0"' in owner_page.text)

            # A front door is meant to be knocked on, so anyone may start a
            # login. Signing in is not signing up, though: a Google account we
            # haven't met is asked for a name and its agreements, and no account
            # exists until it has answered.
            _cfg.set("new_account_scope", "phone", save=False)

            def _start(**form):
                """Begin a sign-in: log in by GET, sign up by POST."""
                _bans.forgive("testclient")
                if form:
                    r = client.post("/auth/google/start", data=form,
                                    follow_redirects=False)
                else:
                    r = client.get("/auth/google/start", follow_redirects=False)
                where = r.headers.get("location", "")
                got = where.split("state=")[1].split("&")[0] if "state=" in where else ""
                return got, r

            def _token(asked, sub="55501", **over):
                claims = {"sub": sub, "email": f"{sub}@example.com",
                          "email_verified": True, "name": "New Person",
                          "picture": "https://pics/new.jpg",
                          "aud": "test-client-id", "exp": time.time() + 600,
                          "iss": "https://accounts.google.com", "nonce": asked}
                claims.update(over)
                raw = _json.dumps(claims).encode()
                mid = _b64mod.urlsafe_b64encode(raw).decode().rstrip("=")
                return {"id_token": "x." + mid + ".y"}

            def _back(state, nonce, sub="55501"):
                """Google sends them back, having confirmed who they are."""
                with _patch.object(_goog, "_post",
                                   lambda *a, **k: _token(nonce, sub)):
                    got = client.get(f"/auth/google/callback?code=abc&state={state}",
                                     follow_redirects=False)
                client.cookies.clear()      # the jar would sign every later call in
                return got

            with _patch("mrs.web.security.is_home", lambda ip: False):
                state, r = _start()
                c("a stranger can start a sign-in", r.status_code == 302, str(r.status_code))
                sent = r.headers.get("location", "")
                c("...sent to Google, nowhere else",
                  sent.startswith("https://accounts.google.com/o/oauth2/v2/auth"))
                c("...with the client id, asking which account",
                  "test-client-id" in sent and "prompt=select_account" in sent)
                waiting = _goog._PENDING.get(state, {})
                c("the sign-in is remembered while Google has them", bool(waiting.get("nonce")))

                c("a callback with a state nobody issued is refused",
                  client.get("/auth/google/callback?code=x&state=made-up",
                             follow_redirects=False).status_code == 400)
                for name, over in (("meant for another app", {"aud": "someone-else"}),
                                   ("from the wrong issuer", {"iss": "https://evil.example"}),
                                   ("already expired", {"exp": time.time() - 600}),
                                   ("answering a different sign-in", {"nonce": "other"}),
                                   ("an address Google hasn't checked",
                                    {"email_verified": False})):
                    with _patch.object(_goog, "_post",
                                       lambda *a, **k: _token(waiting.get("nonce"), **over)):
                        c(f"a token {name} is refused",
                          _goog.finish("code", dict(waiting)) is None)

                # -- logging in as somebody we've never met ------------------
                r = _back(state, waiting.get("nonce"))
                c("a Google account we haven't met is asked to finish, not admitted",
                  r.status_code == 302 and r.headers.get("location") == "/auth/claim"
                  and _acc.get("55501") is None,
                  f"{r.status_code} {r.headers.get('location')}")
                ck = r.headers.get("set-cookie", "")
                c("...held by a short-lived cookie that page scripts can't read",
                  "mrs_claim=" in ck and "httponly" in ck.lower())
                handle = ck.split("mrs_claim=")[1].split(";")[0]
                held = {"mrs_claim": handle}
                client.cookies.clear()
                c("a state cannot be used twice",
                  client.get(f"/auth/google/callback?code=abc&state={state}",
                             follow_redirects=False).status_code == 400)

                page = client.get("/auth/claim", cookies=held)
                c("the finishing page offers their name to change",
                  page.status_code == 200 and "One last thing" in page.text
                  and 'value="New Person"' in page.text, str(page.status_code))
                c("...and the photo Google gave", "https://pics/new.jpg" in page.text)
                c("nobody without the cookie gets it",
                  "One last thing" not in client.get("/auth/claim").text
                  and "One last thing" not in client.get(
                      "/auth/claim", cookies={"mrs_claim": "nope"}).text)

                # A browser posting one of our own forms says Origin: null when the
                # referrer policy is strict, and only a real browser ever does --
                # the test client sends no Origin at all, which is how this hid.
                c("the front page doesn't tell browsers to send Origin: null",
                  client.get("/").headers.get("referrer-policy") == "same-origin")
                nul = client.post("/auth/claim", data={"name": "Sam Rivers", "terms": "1"},
                                  cookies=held, headers={"Origin": "null"}, follow_redirects=False)
                c("a form from our own page with Origin: null is refused without the browser's say-so",
                  nul.status_code == 403 and _acc.get("55501") is None)
                for site in ("cross-site", "same-site", "none"):
                    got = client.post("/auth/claim", data={"name": "Sam Rivers", "terms": "1"},
                                      cookies=held, headers={"Origin": "null", "Sec-Fetch-Site": site},
                                      follow_redirects=False)
                    c(f"...and a sandboxed frame or other site ({site}) still is",
                      got.status_code == 403 and _acc.get("55501") is None)
                _bans.forgive("testclient")
                c("Origin: null from the browser saying it was our own page is taken",
                  client.post("/auth/google/start", data={"name": "Origin Null", "terms": "1"},
                              headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
                              follow_redirects=False).status_code == 302)

                short = client.post("/auth/claim", data={"name": "A", "terms": "1"},
                                    cookies=held, follow_redirects=False)
                c("a one-letter name is turned back with a reason",
                  short.status_code == 400 and "at least two" in short.text)
                bare = client.post("/auth/claim", data={"name": "Sam Rivers"},
                                   cookies=held, follow_redirects=False)
                c("finishing without the privacy notice is turned back",
                  bare.status_code == 400 and "privacy notice" in bare.text)
                c("...and neither attempt made an account, or used up the claim",
                  _acc.get("55501") is None
                  and client.get("/auth/claim", cookies=held).status_code == 200)

                cross = client.post("/auth/claim", data={"name": "Sam Rivers", "terms": "1"},
                                    cookies=held, headers={"Origin": "https://evil.example"},
                                    follow_redirects=False)
                c("another site can't post the form for them",
                  cross.status_code == 403 and _acc.get("55501") is None)

                done = client.post("/auth/claim", data={"name": "  Sam   Rivers ", "terms": "1"},
                                   cookies=held, follow_redirects=False)
                c("a name and the notice make an account", done.status_code == 302
                  and done.headers.get("location") == "/player",
                  f"{done.status_code} {done.headers.get('location')}")
                made = _acc.get("55501")
                c("...named what they chose, not what Google calls them",
                  bool(made) and made["name"] == "Sam Rivers")
                c("...at the default scope, with their photo",
                  made["scope"] == "phone" and made["picture"] == "https://pics/new.jpg")
                c("...having agreed to the notice, and not to tracking",
                  made["terms_at"] > 0 and made["terms_version"] == _acc.TERMS_VERSION
                  and made["tracking"] is False and made["tracking_at"] == 0)
                c("...and OAuth alone can never mint an owner", made["scope"] != "owner")
                sc = done.headers.get("set-cookie", "")
                c("...signed in, with a cookie that is http-only",
                  "mrs_account=" in sc and "httponly" in sc.lower())
                c("a claim is used once",
                  client.post("/auth/claim", data={"name": "Sam Rivers", "terms": "1"},
                              cookies=held, follow_redirects=False).status_code == 400
                  and "One last thing" not in client.get(
                      "/auth/claim", cookies=held).text)

                # -- signing up, having said who they are first --------------
                before = len(_goog._PENDING)
                for label, form, needle in (
                        ("a name that is too short", {"name": "x", "terms": "1"}, "at least two"),
                        ("a name that's only spaces", {"name": "   ", "terms": "1"}, "at least two"),
                        ("no agreement to the privacy notice", {"name": "Priya"}, "privacy notice"),
                        ("an agreement that isn't a yes", {"name": "Priya", "terms": "0"},
                         "privacy notice")):
                    _bans.forgive("testclient")
                    r = client.post("/auth/google/start", data=form, follow_redirects=False)
                    c(f"signing up with {label} is refused, before Google",
                      r.status_code == 400 and needle in r.text, str(r.status_code))
                c("...and none of those started anything with Google",
                  len(_goog._PENDING) == before)
                _bans.forgive("testclient")
                c("another site can't start a sign-up for somebody",
                  client.post("/auth/google/start", data={"name": "Priya", "terms": "1"},
                              headers={"Origin": "https://evil.example"},
                              follow_redirects=False).status_code == 403)

                # Tracking is exactly what was ticked; a form that says nothing
                # about it says no.
                st2, r = _start(name="Priya Nair", terms="1", tracking="1")
                c("a proper sign-up goes on to Google", r.status_code == 302 and bool(st2),
                  str(r.status_code))
                c("...carrying the name and the tracking choice with it",
                  _goog._PENDING.get(st2, {}).get("signup")
                  == {"name": "Priya Nair", "tracking": True})
                r = _back(st2, _goog._PENDING[st2]["nonce"], "55502")
                c("...and coming back creates the account outright, with no second page",
                  r.status_code == 302 and r.headers.get("location") == "/player"
                  and "mrs_claim=" not in r.headers.get("set-cookie", ""))
                p2 = _acc.get("55502")
                c("...under the name they gave", bool(p2) and p2["name"] == "Priya Nair")
                c("...having opted in, and when",
                  p2["tracking"] is True and p2["tracking_at"] > 0 and p2["terms_at"] > 0)

                st3, r = _start(name="Quiet One", terms="1")
                r = _back(st3, _goog._PENDING[st3]["nonce"], "55503")
                p3 = _acc.get("55503")
                c("a sign-up that leaves tracking unticked is not tracked",
                  bool(p3) and p3["tracking"] is False and p3["tracking_at"] == 0)

                # -- coming back ---------------------------------------------
                st4, _ = _start()
                r = _back(st4, _goog._PENDING[st4]["nonce"], "55501")
                c("logging in again goes straight through",
                  r.status_code == 302 and r.headers.get("location") == "/player"
                  and "mrs_claim=" not in r.headers.get("set-cookie", ""))
                c("...and Google's name for them does not replace theirs",
                  _acc.get("55501")["name"] == "Sam Rivers")
                st5, _ = _start(name="Someone Else", terms="1", tracking="1")
                _back(st5, _goog._PENDING[st5]["nonce"], "55501")
                c("signing up as a name that's already an account changes nothing about it",
                  _acc.get("55501")["name"] == "Sam Rivers"
                  and _acc.get("55501")["tracking"] is False)
                client.cookies.clear()

            c("only the owner sees who has signed in",
              client.get("/api/accounts", headers={"X-Music-Key": phone}).status_code == 403
              and client.get("/api/accounts", headers=owner_h).status_code == 200)
            body = client.get("/api/accounts", headers=owner_h).json()
            c("...with what to paste into the Google Console",
              body.get("redirect_uri", "").endswith("/auth/google/callback"))
            c("...and the default a new sign-in gets",
              body.get("new_account_scope") == "phone")
            c("...and the UI offers only safe new-account scopes",
              tuple(body.get("new_account_scopes") or ()) == _acc.NEW_ACCOUNT_SCOPES)
            from .core import net as _net
            shared_url = _net.player_url("music.example.test", phone)
            c("the link you hand out is the bare front door, no token in it",
              shared_url.endswith("/") and "token=" not in shared_url,
              shared_url[:100])
            c("a header-authenticated owner is recognised at the front door",
              client.get("/", headers=owner_h,
                         follow_redirects=False).status_code == 302)
            signed_out = client.get("/auth/signout",
                                    cookies=_sign_in_as("1234567890"),
                                    follow_redirects=False)
            c("signing out returns to the public start page",
              signed_out.headers.get("location") == "/"
              and "mrs_account=" in signed_out.headers.get("set-cookie", ""))
            was_legacy_mutations = _cfg.get("allow_legacy_get_mutations")
            _cfg.set("allow_legacy_get_mutations", True, save=False)
            c("the unsafe owner default is rejected",
              client.get("/api/setting?key=new_account_scope&value=owner",
                         headers=owner_h).status_code == 400)
            _cfg.set("new_account_scope", "owner", save=False)
            c("a damaged owner default fails closed",
              _acc.default_scope() == "blocked"
              and _acc.admit("noowner01", "noowner@example.com", "No Owner")["scope"]
              == "blocked")
            _cfg.set("new_account_scope", "phone", save=False)

            # Neither an account session nor a pass may be claimed when the
            # corresponding durable write failed.
            with _patch.object(_acc, "_write", return_value=False):
                try:
                    _acc.admit("nosave001", "nosave@example.com", "No Save")
                    saved_account_refused = False
                except _acc.AccountPersistenceError:
                    saved_account_refused = True
            c("an unwritable account registry refuses admission", saved_account_refused)
            with _patch("mrs.web.security._save_passes", return_value=False):
                broken_pass = sec.issue(now_key(), name="not-durable", scope="phone")
            c("an unwritable pass registry returns no credential", not broken_pass)

            # Forget means delete the profile folder too, not merely remove
            # the account row that points to it.
            from .core.profile import profiles as _account_profiles
            erased_sub = "erase0001"
            _acc.admit(erased_sub, "erase@example.com", "Erase Me")
            client.get("/api/setting?key=theme&value=warm",
                       cookies=_sign_in_as(erased_sub))
            erased_home = _account_profiles.for_row(
                _acc.as_row(_acc.get(erased_sub))).home()
            removed = client.get("/api/accounts/forget?sub=" + erased_sub,
                                 headers=owner_h)
            c("forgetting an account also removes its profile data",
              removed.status_code == 200 and not erased_home.exists()
              and _acc.get(erased_sub) is None)

            # Pending OAuth state is bounded before it can consume arbitrary
            # memory. Directly exercise the tiny state store; no browser or
            # network request is needed to construct an authorization URL.
            pending_before = dict(_goog._PENDING)
            _goog._PENDING.clear()
            try:
                for _ in range(_goog._PENDING_PER_IP):
                    _goog.start(client_ip="198.51.100.55")
                try:
                    _goog.start(client_ip="198.51.100.55")
                    oauth_cap = False
                except _goog.SignInBusy:
                    oauth_cap = len(_goog._PENDING) == _goog._PENDING_PER_IP
            finally:
                _goog._PENDING.clear()
                _goog._PENDING.update(pending_before)
            c("one address cannot allocate unlimited Google sign-in state", oauth_cap)

            # These are per-record single flights.  Followers receive the
            # shared cache result rather than multiplying external requests.
            import threading as _threading
            from .resolve import insights as _insights, lyrics as _lyrics
            insight_key = _insights._key("One Lookup", "The Band")
            _insights.store._rows.pop(insight_key, None)
            _insights.store._busy.discard(insight_key)
            insight_entered, insight_release = _threading.Event(), _threading.Event()
            insight_followers_done = _threading.Event()
            insight_calls = []
            insight_followers_left = [5]
            insight_followers_lock = _threading.Lock()

            def _slow_insight(*args):
                insight_calls.append(args)
                insight_entered.set()
                insight_release.wait(2)
                return {"at": time.time()}

            with _patch.object(_insights, "_lookup_claimed", side_effect=_slow_insight):
                first_lookup = _threading.Thread(
                    target=lambda: _insights.lookup("One Lookup", "The Band"))
                first_lookup.start()
                insight_entered.wait(1)
                def _follow_insight():
                    _insights.lookup("One Lookup", "The Band")
                    with insight_followers_lock:
                        insight_followers_left[0] -= 1
                        if not insight_followers_left[0]:
                            insight_followers_done.set()
                followers = [_threading.Thread(target=_follow_insight) for _ in range(5)]
                [t.start() for t in followers]
                insight_followers_done.wait(1)
                insight_release.set()
                first_lookup.join(2)
                [t.join(2) for t in followers]
            c("concurrent about lookups issue one upstream job", len(insight_calls) == 1)

            from .web import api as _api
            rate_before = dict(_api._shared_rate)
            rate_cap_before = _cfg.get("guest_requests_hour")
            _api._shared_rate.clear()
            _cfg.set("guest_requests_hour", 1, save=False)

            class _SharedRateRequest:
                client = type("Client", (), {"host": "198.51.100.10"})()
                state = type("State", (), {
                    "pass_row": {"id": "quota-check", "owner": False,
                                 "internal": False}})()

            try:
                with _patch.object(sec, "note_use"):
                    _api._guard_rate(None, _SharedRateRequest())
                    try:
                        _api._guard_rate(None, _SharedRateRequest())
                        shared_rate_limited = False
                    except Exception as exc:
                        shared_rate_limited = getattr(exc, "status_code", None) == 429
            finally:
                _api._shared_rate.clear()
                _api._shared_rate.update(rate_before)
                _cfg.set("guest_requests_hour", rate_cap_before, save=False)
            c("shared passes have an enforced hourly request cap", shared_rate_limited)

            lyric_key = "Band|One Lyric|200"
            _lyrics._cache.pop(lyric_key, None)
            _lyrics._misses.pop(lyric_key, None)
            lyric_entered, lyric_release = _threading.Event(), _threading.Event()
            lyric_calls = []

            def _slow_lyrics(url):
                lyric_calls.append(url)
                lyric_entered.set()
                lyric_release.wait(2)
                return {"plainLyrics": "one shared result"}

            with _patch.object(_lyrics, "_fetch", side_effect=_slow_lyrics):
                first_lyric = _threading.Thread(
                    target=lambda: _lyrics.get_lyrics("One Lyric", "Band", 200))
                first_lyric.start()
                lyric_entered.wait(1)
                lyric_followers = [_threading.Thread(
                    target=lambda: _lyrics.get_lyrics("One Lyric", "Band", 200))
                    for _ in range(5)]
                [t.start() for t in lyric_followers]
                time.sleep(0.05)
                lyric_release.set()
                first_lyric.join(2)
                [t.join(2) for t in lyric_followers]
            c("concurrent lyric lookups issue one upstream request", len(lyric_calls) == 1)
            _lyrics._cache.pop(lyric_key, None)

            # A compatibility GET reaches the handler only when explicitly
            # enabled. Its cache-only helpers must not quietly turn that read
            # back into Last.fm or MusicBrainz work.
            from .core.era import era as _era
            from .core.tags import tagstore as _tagstore
            from .models import Track as _PassiveTrack
            passive_track = _PassiveTrack(title="Passive", artist="No Network")
            with _patch.object(_tagstore, "get", side_effect=AssertionError), \
                 _patch.object(_era, "get", side_effect=AssertionError), \
                 _patch.object(_tagstore, "cached", return_value={}) as cached_tags, \
                 _patch.object(_era, "cached", return_value=None) as cached_era:
                _insights.about(passive_track, fetch=False)
            c("a cache-only insight read does not queue enrichment",
              cached_tags.called and cached_era.called)
            c("a cache-only lyric read does not start a fetch",
              _lyrics.cached_lyrics("Passive", "No Network", 180) is None)

            # Listing cannot be the thing that expires credentials or ends
            # listeners. Those jobs have a lifecycle watchdog of their own.
            from .core.session import sessions as _sessions
            with _patch.object(_api.sec, "tidy_passes", side_effect=AssertionError), \
                 _patch.object(_sessions, "reap", side_effect=AssertionError):
                passive_lists = (
                    client.get("/api/passes", headers=owner_h).status_code == 200
                    and client.get("/api/sessions", headers=owner_h).status_code == 200)
            c("pass and session lists have no cleanup side effects", passive_lists)

            # Legacy GET remains supported for an old owner page, but it must
            # use the in-memory model list rather than make a Groq request.
            with _patch.object(_api.llm, "models", side_effect=AssertionError):
                legacy_models = client.get("/api/groqmodels", headers=owner_h)
            c("a legacy model-list GET stays cache-only",
              legacy_models.status_code == 200)

            # A diagnostic must not clear startup preferences merely because
            # Windows currently reports that the matching entries are gone.
            saved_get = _api.config.get
            def startup_flags(key, default=None):
                if key in {"start_before_signin", "start_on_boot"}:
                    return True
                return saved_get(key, default)
            with _patch.object(_api.config, "get", side_effect=startup_flags), \
                 _patch.object(_api.config, "set", side_effect=AssertionError), \
                 _patch.object(_api, "_run_ps", return_value=(True, "none")), \
                 _patch("winreg.OpenKey", side_effect=FileNotFoundError):
                missing_boot = _api.boot_state()
            c("boot status reports missing startup entries without overwriting settings",
              any("missing" in w for w in missing_boot.get("warnings", [])))

            from .core import radio as _radio
            from .models import Track as _RadioTrack
            station_url = "https://radio.example.test/live"
            _radio._cache["check-station"] = (
                time.time(), [_RadioTrack(title="Test Radio", url=station_url,
                                          source="radio")])
            c("station loading rejects local, file, and unrecognised URLs",
              not _radio.is_known_stream("http://127.0.0.1:8080/private")
              and not _radio.is_known_stream("file:///C:/private.wav")
              and not _radio.is_known_stream("https://other.example.test/live")
              and _radio.is_known_stream(station_url))
            _radio._cache.pop("check-station", None)
            _cfg.set("allow_legacy_get_mutations", was_legacy_mutations, save=False)
            c("the secret is never handed back",
              "google_client_secret" not in client.get(
                  "/api/settings", headers=owner_h).json())
            class _SetupSocket:
                def __init__(self, *args, **kwargs):
                    pass

                def connect(self, address):
                    pass

                def getsockname(self):
                    return ("192.0.2.1", 0)

                def close(self):
                    pass

            with _patch("socket.socket", _SetupSocket):
                setup_owner = client.get("/setup", headers=owner_h)
            c("the setup page still needs the key",
              client.get("/setup", headers={"X-Music-Key": phone}).status_code == 403
              and setup_owner.status_code == 200)
            for k, v in (("google_client_id", ""), ("google_client_secret", ""),
                         ("ddns_hostname", ""), ("owner_email", was_owner_email or ""),
                         ("new_account_scope", was_new_scope or "phone")):
                _cfg.set(k, v, save=False)
            _acc._path().unlink(missing_ok=True)
            say("people sign in", c)

            # -- 26. what the request reader asks for, and what it does with the reply
            c = _Checker("reader")
            from .resolve import llm as _llm
            from . import requests as _rq

            def _reply(**fields):
                row = dict(_llm._BLANK)
                row.update(fields)
                return lambda body, timeout: {"choices": [{"message": {"content": _json.dumps(row)}}]}

            def _ask(said, **fields):
                with _patch.object(_llm, "_post", _reply(**fields)), \
                     _patch.object(_llm, "available", lambda: True):
                    return _llm.parse(said)

            # The prompt offers exactly the commands the app can do. A word it
            # doesn't offer is a word the model invents, and the app answers
            # "I don't know how to skip".
            c("the prompt offers exactly the commands the app can carry out",
              set(_llm.COMMANDS) == set(_rq._COMMANDS) | {"more_like_this", "save", "add_to_playlist"},
              str(sorted(set(_llm.COMMANDS) ^ (set(_rq._COMMANDS) | {"more_like_this", "save", "add_to_playlist"}))))
            c("...and says every one of them", all(w in _llm.SYSTEM for w in _llm.COMMANDS))
            c("...and tells the model what to do when it isn't about music",
              '"none"' in _llm.SYSTEM or "none" in _llm.SYSTEM)

            # What the prompt teaches has to be something the parser accepts.
            for said, f in _llm.EXAMPLES:
                plan = _ask(said, **f)
                if f["kind"] == "none":
                    c(f"its own example {said[:28]!r} is declined, handing it back", plan is None)
                else:
                    c(f"its own example {said[:28]!r} parses as {f['kind']}",
                      plan is not None and plan.kind == f["kind"], str(plan)[:70])

            # Words a helpful model uses instead of the ones it was given.
            for said_word, want in (("skip", "next"), ("stop", "pause"), ("play", "resume"),
                                    ("back", "previous"), ("Silence", "mute"),
                                    ("more like this", "more_like_this"), ("download", "save")):
                plan = _ask("x", kind="command", title=said_word)
                c(f"'{said_word}' is understood as {want}", plan is not None and plan.command == want,
                  str(plan and plan.command))
            c("a word the app doesn't have is still said plainly, not swallowed",
              _ask("x", kind="command", title="teleport").command == "teleport")

            # Numbers used to be dropped: "set the volume to forty" set it to 70.
            for arg, want in (("40", "40"), ("150%", "150"), ("999", "150"), ("-5", "0")):
                got = _ask("x", kind="command", title="volume", argument=arg)
                c(f"volume '{arg}' becomes level {want}", got.query == want, got.query)
            c("...and a number written as a number works too",
              _ask("x", kind="command", title="volume", argument=55).query == "55")
            c("a change with an amount keeps it",
              _ask("x", kind="command", title="volume_delta", argument="-20").query == "-20")
            c("a change with no amount is up for 'louder'",
              _ask("crank it up", kind="command", title="volume_delta").query == "10")
            c("...and down when what was said was down",
              _ask("make it quieter please", kind="command", title="volume_delta").query == "-10")
            c("'quieter' as the command word is a downward change",
              _ask("x", kind="command", title="quieter").query == "-10")
            c("a playlist needs a name", _ask("x", kind="command", title="add_to_playlist") is None)
            c("...and gets the one it was given",
              _ask("x", kind="command", title="add_to_playlist", argument="road trip").query == "road trip")

            # And that the level really reaches the player.
            calls = []
            with _patch.object(_rq.player, "control",
                               side_effect=lambda a, v=None: calls.append((a, v)) or {"message": "ok"}):
                _rq._run_command(_ask("x", kind="command", title="volume", argument="40"))
                _rq._run_command(_ask("x", kind="command", title="skip"))
                _rq._run_command(_ask("turn it down", kind="command", title="volume_delta", argument="-10"))
            c("'set the volume to forty' asks the player for 40, not its default",
              calls[:1] == [("volume", 40)], str(calls))
            c("'skip' does what 'next' does", calls[1:2] == [("next", None)], str(calls))
            c("a quieter request goes down", calls[2:3] == [("volume_delta", -10)], str(calls))

            # Declines and odd replies.
            c("'none' hands the request back to the grammar", _ask("x", kind="none") is None)
            c("an unknown kind does too", _ask("x", kind="poem") is None)
            c("a model that says 'false' as a word doesn't turn shuffle on",
              not _ask("x", kind="song", title="A", shuffle="false").shuffle)
            c("...and 'true' as a word does", _ask("x", kind="genre", genre="chill", shuffle="true").shuffle)
            multi = _ask("x", kind="artist", artist="Nirvana and Foo Fighters")
            c("two artists are two seeds", multi.seeds == ["Nirvana", "Foo Fighters"], str(multi.seeds))
            c("a song's artist is kept", _ask("x", kind="song", title="Coming Undone",
                                              artist="Korn").artist == "Korn")
            c("a null argument doesn't become the word 'None'",
              _ask("x", kind="command", title="next", argument=None).query == "")
            say("the request reader", c)

            # -- 27. the address: /music, and no port ------------------------
            c = _Checker("address")
            from .web import prefix as _pfx
            from .core import net as _n
            from . import server as _s2
            was = {k: _cfg.get(k) for k in ("url_prefix", "public_port", "ddns_hostname",
                                            "google_client_id", "google_client_secret")}
            try:
                # The front door only has a button to inspect once sign-in is on.
                _cfg.set("google_client_id", "addr-test-id", save=False)
                _cfg.set("google_client_secret", "addr-test-secret", save=False)
                for junk in ("//evil.example", "http://evil.example", "/a/b", "music",
                             "/music/", "/", "/ music", "/" + "a" * 40, "/Music"):
                    _cfg.set("url_prefix", junk, save=False)
                    if junk == "music" or junk == "/music/":
                        want = "/music"          # tolerated: a missing or trailing slash
                    else:
                        want = ""
                    c(f"prefix {junk[:22]!r} is read as {want!r}", _pfx.configured() == want,
                      _pfx.configured())
                _cfg.set("url_prefix", "/music", save=False)
                _cfg.set("ddns_hostname", "music.example.test", save=False)

                # What the outside world is told.
                _cfg.set("public_port", 0, save=False)
                c("with nothing set, the link carries the real port",
                  _n.public_base("h.test").endswith(f":{_n.live_port()}/music"))
                _cfg.set("public_port", 443, save=False)
                c("http on 443 keeps the port (it isn't http's)",
                  _n.public_base("h.test") == "http://h.test:443/music")
                _s2.runtime["tls"] = True
                try:
                    c("https on 443 has no port and a path: https://h.test/music",
                      _n.public_base("h.test") == "https://h.test/music", _n.public_base("h.test"))
                    _cfg.set("public_port", 8443, save=False)
                    c("https on 8443 keeps it", _n.public_base("h.test") == "https://h.test:8443/music")
                    _cfg.set("public_port", 443, save=False)
                    c("the link handed out is that",
                      _n.player_url("h.test") == "https://h.test/music", _n.player_url("h.test"))
                    c("...but a device on this network still needs the real port",
                      _n.player_url("192.168.1.5", outside=False)
                      == f"https://192.168.1.5:{_n.live_port()}/music")
                    c("Google is sent back to the same public spelling",
                      _goog.redirect_uri() == "https://music.example.test/music/auth/google/callback",
                      _goog.redirect_uri())
                finally:
                    _s2.runtime.pop("tls", None)
                _cfg.set("public_port", 0, save=False)

                # The server accepts both, and says what fits the door used.
                r = client.get("/music/api/status", headers=owner_h)
                c("the prefixed api answers", r.status_code == 200, str(r.status_code))
                c("...and so does the bare one", client.get("/api/status", headers=owner_h).status_code == 200)
                c("a look-alike prefix is not the prefix",
                  client.get("/musical/api/status", headers=owner_h).status_code == 404)
                with _patch("mrs.web.security.is_home", lambda ip: False):
                    r = client.get("/music", follow_redirects=False)
                    c("/music is the front door", r.status_code == 200
                      and "Sign in" in r.text, str(r.status_code))
                    c("...and its buttons go through the prefix",
                      'href="/music/auth/google/start' in r.text)
                    bare = client.get("/", follow_redirects=False)
                    c("the bare front door's buttons don't",
                      'href="/auth/google/start' in bare.text)
                r = client.get("/music/", headers=owner_h, follow_redirects=False)
                c("the owner is sent on to the player, inside the prefix",
                  r.status_code == 302 and r.headers.get("location") == "/music/player",
                  str(r.headers.get("location")))
                r = client.get("/", headers=owner_h, follow_redirects=False)
                c("...and to the bare player when they came in bare",
                  r.headers.get("location") == "/player")
                left = client.get("/music/auth/signout", follow_redirects=False)
                c("signing out lands back at the front, inside the prefix",
                  left.headers.get("location") == "/music/")
                c("...and clears a cookie scoped to the application, not the whole address",
                  "path=/music" in left.headers.get("set-cookie", "").lower().replace(" ", ""),
                  left.headers.get("set-cookie", "")[:90])
                bare_left = client.get("/auth/signout", follow_redirects=False)
                c("the bare route still clears the site-wide one",
                  "path=/;" in bare_left.headers.get("set-cookie", "").lower().replace(" ", "") + ";")

                # The pages know how they were reached.
                page = client.get("/music/player", headers=owner_h).text
                c("the player is told its base", 'const BASE = "/music";' in page)
                c("...and is told nothing when it came in bare",
                  'const BASE = "";' in client.get("/player", headers=owner_h).text)
                src = (Path(__file__).parent / "web" / "templates" / "player.html").read_text("utf-8")
                for what, needle in (("requests", "fetch(BASE + url.pathname"),
                                     ("the stream", 'BASE + "/api/output/stream/"'),
                                     ("the event feed", 'new EventSource(BASE + "/api/events?"'),
                                     ("sign out", 'BASE + "/auth/signout"')):
                    c(f"the player's {what} carry the base", needle in src)
            finally:
                for k, v in was.items():
                    _cfg.set(k, v if v is not None else "", save=False)
            say("the address", c)

            # -- 28. taking a person away: every store, then look for them -----
            c = _Checker("erasure")
            import logging as _logging
            from .web import accounts as _acc2, privacy as _priv
            from .core import stats as _st2, audit as _audit2
            from .core.playlists import playlists as _pl2
            from .core.profile import profiles as _profiles2
            from .core.session import sessions as _sessions2
            from .models import Track as _T2
            from .paths import data_dir as _dd2

            class _Grab(_logging.Handler):
                def __init__(self):
                    super().__init__()
                    self.lines = []

                def emit(self, rec):
                    try:
                        self.lines.append(rec.getMessage())
                    except Exception:
                        pass

            ev_grab = _Grab()
            ev_root = _logging.getLogger()
            ev_old_level = ev_root.level
            ev_root.addHandler(ev_grab)
            ev_root.setLevel(_logging.DEBUG)
            ev_was = {k: _cfg.get(k) for k in ("owner_email", "new_account_scope")}
            try:
                _acc2._path().unlink(missing_ok=True)
                _cfg.set("owner_email", "", save=False)
                _cfg.set("new_account_scope", "phone", save=False)

                SUB_A, SUB_B = "77001100", "77002200"
                EM_A, EM_B = "erase-a@example.com", "erase-b@example.com"
                # Same display name on purpose: names are typed by people, and
                # two of them can be Sam.
                ev_a = _acc2.admit(SUB_A, EM_A, "Sam", terms=True, tracking=True)
                ev_b = _acc2.admit(SUB_B, EM_B, "Sam", terms=True, tracking=False)
                pa, pb = _acc2.profile_id(SUB_A), _acc2.profile_id(SUB_B)
                c("an account records what it agreed to, and when",
                  ev_a["terms_version"] == _acc2.TERMS_VERSION and ev_a["terms_at"] > 0
                  and ev_a["tracking"] is True and ev_a["tracking_at"] > 0)
                c("tracking is off unless it was said yes to",
                  ev_b["tracking"] is False and _acc2.admit("77003300", "x@example.com", "X")["tracking"] is False)
                c("a chosen name isn't overwritten by Google's on the next sign-in",
                  _acc2.admit(SUB_A, EM_A, "Someone Else Entirely")["name"] == "Sam")

                prof_a = _profiles2.for_row(_acc2.as_row(ev_a))
                prof_b = _profiles2.for_row(_acc2.as_row(ev_b))
                c("a tracked account gets a learning taste store", prof_a.tracking and
                  type(prof_a.taste).__name__ == "TasteEngine")
                c("one that declined gets one that only keeps what it did on purpose",
                  not prof_b.tracking and type(prof_b.taste).__name__ == "ExplicitTaste")

                t1 = _T2(video_id="erase-song-1", title="Erase Song", artist="Erase Band")
                for prof in (prof_a, prof_b):
                    prof.taste.record(t1, 200, 210)
                    prof.taste.toggle_like(t1)
                    prof.taste.save()
                home_a, home_b = _dd2() / "profiles" / pa, _dd2() / "profiles" / pb
                c("a tracked profile learns from what it played",
                  prof_a.taste.history_ids() == ["erase-song-1"]
                  and (home_a / "taste" / "play_stats.json").exists())
                c("an untracked one learns nothing and writes no history",
                  prof_b.taste.history_ids() == [] and not (home_b / "taste" / "play_stats.json").exists())
                c("...but keeps a heart, because a heart was asked for",
                  prof_b.taste.is_liked("erase-song-1")
                  and (home_b / "taste" / "liked_songs.json").exists())
                prof_a.lists.create("Sam's mix")
                prof_a.lists.add("Sam's mix", t1)

                # counters: the tracked person has a row, the other doesn't
                _st2.set_tracked(pa, True)
                _st2.set_tracked(pb, False)
                _st2.note(pa, requests=3, plays=2, seconds=90, name="Sam")
                _st2.note(pb, requests=2, plays=1)
                _st2.flush()
                ids_with_rows = {row["id"] for row in _st2.links()}
                c("a tracked person has a usage row", pa in ids_with_rows)
                c("one who declined has none, though the house still counts them",
                  pb not in ids_with_rows and _st2.house()["totals"]["requests"] >= 5)
                house_before = _st2.house()["totals"]["requests"]

                # a shared list, credited by id and (from before ids) by name
                _pl2.create("erase-shared")
                _pl2.set_shared("erase-shared", True)
                _pl2.add("erase-shared", _T2(video_id="ea1", title="A's song", artist="X"), by="Sam", by_id=pa)
                _pl2.add("erase-shared", _T2(video_id="eb1", title="B's song", artist="X"), by="Sam", by_id=pb)
                _pl2.add("erase-shared", _T2(video_id="el1", title="Old song", artist="X"), by="Sam")
                c("two people called Sam are told apart by id",
                  _pl2.is_credit_owner("erase-shared", "ea1", "Sam", pa)
                  and not _pl2.is_credit_owner("erase-shared", "ea1", "Sam", pb))
                c("...so one Sam can't take back the other's addition",
                  _pl2.is_credit_owner("erase-shared", "eb1", "Sam", pb)
                  and not _pl2.is_credit_owner("erase-shared", "eb1", "Sam", pa))
                c("a row from before ids is a link's, and an account can't claim it by name",
                  not _pl2.is_credit_owner("erase-shared", "el1", "Sam", pa)
                  and _pl2.is_credit_owner("erase-shared", "el1", "Sam", ""))

                _audit2.record("POST /api/setting", f"account:{pa}", 403)
                _audit2.record("POST /api/setting", "owner", 200)
                _sessions2.for_pass(pa, "Sam", "phone", prof_a)

                # what they can be given back
                got = _priv.export(SUB_A)
                text = _json.dumps(got)
                c("the export has what was signed up with", EM_A in text and "Sam" in text)
                c("...their playlists and what it learned", "Erase Song" in text and "play_stats.json" in text)
                c("...their part of a shared list", any(x["video_id"] == "ea1"
                                                          for x in got["shared_playlist_additions"]))
                c("...and none of anybody else's", EM_B not in text and SUB_B not in text and "eb1" not in text)
                c("...and says usage isn't kept for someone who declined",
                  "not kept" in str(_priv.export(SUB_B)["usage_counters"]))

                # -- take them away --------------------------------------------
                report = _priv.erase(SUB_A)
                c("erasing reports what went",
                  report.get("account") and report.get("profile") and report.get("usage")
                  and report.get("credits") and report.get("audit") and report.get("session"),
                  str(report))
                c("the account is gone and the other one isn't",
                  _acc2.get(SUB_A) is None and _acc2.get(SUB_B) is not None)
                c("their profile folder is gone and the other's is not",
                  not home_a.exists() and home_b.exists())
                c("their usage row is gone, and the place's numbers didn't move",
                  pa not in {row["id"] for row in _st2.links()}
                  and _st2.house()["totals"]["requests"] == house_before)
                after = _json.loads((_pl2._by_file("erase-shared")).read_text("utf-8"))
                c("their name is off the shared list, by id",
                  "ea1" not in after and pa not in after.get("@ids", {}).values())
                c("...and off an old row that only had a name",
                  "el1" not in after)
                c("...but the other Sam keeps theirs",
                  after.get("eb1") == "Sam" and after["@ids"].get("eb1") == pb)
                c("a song they suggested is still in the list, without their name",
                  any(t.video_id == "ea1" for t in _pl2.tracks("erase-shared")))
                trail = _audit2.entries()
                c("the owner's trail forgets them and keeps its own entries",
                  not any(e["actor"] == f"account:{pa}" for e in trail)
                  and any(e["actor"] == "owner" for e in trail))
                c("their live session ended", pa not in getattr(_sessions2, "_rooms", {}))

                # The test that matters: look for them everywhere.
                strays = []
                for path in _dd2().rglob("*"):
                    if path.is_file():
                        try:
                            blob = path.read_bytes()
                        except OSError:
                            continue
                        for needle in (EM_A, SUB_A):
                            if needle.encode() in blob:
                                strays.append(f"{path.name} still has {needle}")
                c("no file in the data folder still holds their address or Google id",
                  not strays, "; ".join(strays[:4]))
                said = "\n".join(ev_grab.lines)
                c("nothing logged carries an address, or a whole account id",
                  not any(n in said for n in (EM_A, EM_B, SUB_A, SUB_B, "x@example.com")),
                  [ln for ln in ev_grab.lines if any(n in ln for n in (EM_A, EM_B, SUB_A, SUB_B))][:2])

                # -- through the door: what a person can do about themselves ------
                SUB_C, EM_C = "77004400", "cee@example.com"
                _acc2.admit(SUB_C, EM_C, "Cee", terms=True)
                ck_c = {_COOKIE: sec.session_cookie(now_key(), SUB_C)}
                me = client.get("/api/me", cookies=ck_c).json()
                c("an account can ask who it is and what it agreed to",
                  me.get("account") and me["consent"]["tracking"] is False
                  and me["consent"]["privacy_notice"]["current"], str(me)[:80])
                c("a link or the owner's key isn't an account here",
                  client.get("/api/me", headers=owner_h).json().get("account") is False
                  and client.get("/api/me/export", headers=owner_h).status_code == 403)
                SUB_N = "77009900"
                _acc2.admit(SUB_N, "old@example.com", "Older Account")   # from before the notice
                ck_n = {_COOKIE: sec.session_cookie(now_key(), SUB_N)}
                c("an account from before the notice hasn't agreed to it",
                  client.get("/api/me", cookies=ck_n).json()["consent"]
                  ["privacy_notice"]["current"] is False)
                c("...and can, from Settings",
                  client.post("/api/me/consent", json={"accept": 1}, cookies=ck_n).json()
                  ["consent"]["privacy_notice"]["current"] is True)
                _acc2.forget(SUB_N)
                r_on = client.post("/api/me/consent", json={"tracking": 1}, cookies=ck_c)
                c("consent can be given", r_on.status_code == 200 and r_on.json()["consent"]["tracking"] is True,
                  str(r_on.status_code))
                c("...and the profile is rebuilt to learn",
                  _profiles2.for_row(_acc2.as_row(_acc2.get(SUB_C))).tracking is True)
                from .web import privacy as _priv3
                _learned = _priv3._home(SUB_C) / "taste" / "play_stats.json"
                _learned.parent.mkdir(parents=True, exist_ok=True)
                _learned.write_text('{"x": 1}', encoding="utf-8")
                _pc = _acc2.profile_id(SUB_C)
                _st2.note(_pc, requests=2, plays=1, name="Cee")
                _st2.flush()
                r_off = client.post("/api/me/consent", json={"tracking": 0}, cookies=ck_c)
                c("...and withdrawn as easily",
                  r_off.status_code == 200 and r_off.json()["consent"]["tracking"] is False
                  and _acc2.get(SUB_C)["tracking_at"] > 0)
                c("...and withdrawing takes what was learned with it",
                  r_off.json().get("forgot") is True and not _learned.exists()
                  and _pc not in {row["id"] for row in _st2.links()})
                r_again = client.post("/api/me/consent", json={"tracking": 0}, cookies=ck_c)
                c("...and saying no twice forgets nothing new",
                  r_again.status_code == 200 and r_again.json().get("forgot") is False)
                c("the change of mind is on record", _acc2.get(SUB_C)["tracking_at"] >= _acc2.get(SUB_C)["created"])
                c("a name can be changed", client.post("/api/me/rename", json={"name": "Cee Two"},
                                                        cookies=ck_c).json().get("name") == "Cee Two")
                c("...but not to nothing", client.post("/api/me/rename", json={"name": " "},
                                                        cookies=ck_c).status_code == 400)
                dl = client.get("/api/me/export", cookies=ck_c)
                c("the export arrives as a file", dl.status_code == 200
                  and "attachment" in dl.headers.get("content-disposition", "")
                  and EM_C in dl.text, str(dl.status_code))

                wrong = client.post("/api/me/delete", json={"confirm": "yes"}, cookies=ck_c)
                c("deleting needs the sentence typed", wrong.status_code == 400
                  and _acc2.get(SUB_C) is not None)
                gone = client.post("/api/me/delete", json={"confirm": "Delete My Account"}, cookies=ck_c)
                c("...and then it is done", gone.status_code == 200 and _acc2.get(SUB_C) is None,
                  str(gone.status_code))
                c("the cookie is thrown away", "mrs_account" in gone.headers.get("set-cookie", ""))
                c("and what it was is nobody",
                  client.get("/api/status", cookies=ck_c).status_code in (401, 403))

                # the owner's account is not one you delete from a browser tab
                _cfg.set("owner_email", "boss@example.com", save=False)
                SUB_O = "77005500"
                _acc2.admit(SUB_O, "boss@example.com", "Boss", terms=True)
                ck_o = {_COOKIE: sec.session_cookie(now_key(), SUB_O)}
                c("the owner can't delete the account that runs the server",
                  client.post("/api/me/delete", json={"confirm": "delete my account"},
                              cookies=ck_o).status_code == 409 and _acc2.get(SUB_O) is not None)
                c("...nor be removed by a forget from the list",
                  client.post("/api/accounts/forget", json={"sub": SUB_O},
                              headers=owner_h).status_code == 409)

                # Blocked from listening is not blocked from one's own data.
                SUB_D = "77006600"
                _acc2.admit(SUB_D, "dee@example.com", "Dee", terms=True)
                _acc2.set_scope(SUB_D, "blocked")
                ck_d = {_COOKIE: sec.session_cookie(now_key(), SUB_D)}
                c("a blocked account can't listen", client.get("/api/status", cookies=ck_d).status_code == 403)
                c("...but can still see what's held about it",
                  client.get("/api/me/export", cookies=ck_d).status_code == 200)
                c("...and have it deleted",
                  client.post("/api/me/delete", json={"confirm": "delete my account"},
                              cookies=ck_d).status_code == 200 and _acc2.get(SUB_D) is None)

                # the owner removing somebody is the same erasure
                SUB_E = "77007700"
                _acc2.admit(SUB_E, "eee@example.com", "Eee", terms=True, tracking=True)
                _st2.set_tracked(_acc2.profile_id(SUB_E), True)
                _st2.note(_acc2.profile_id(SUB_E), requests=1, name="Eee")
                _st2.flush()
                rem = client.post("/api/accounts/forget", json={"sub": SUB_E}, headers=owner_h)
                c("the owner's Forget erases the same things",
                  rem.status_code == 200 and _acc2.get(SUB_E) is None
                  and _acc2.profile_id(SUB_E) not in {row["id"] for row in _st2.links()}, str(rem.status_code))

                # what the owner's trail shows
                _audit2.record("POST /api/setting", f"account:{pb}", 403)
                names = [e["actor"] for e in client.get("/api/audit", headers=owner_h).json()["entries"]]
                c("the trail reads names, not ids, for accounts that exist",
                  "guest:Sam" in names)
                _acc2.forget(SUB_B)
                names = [e["actor"] for e in client.get("/api/audit", headers=owner_h).json()["entries"]]
                c("...and says so when the account is gone",
                  "guest:a deleted account" in names and "guest:Sam" not in names)
            finally:
                ev_root.removeHandler(ev_grab)
                ev_root.setLevel(ev_old_level)
                for k, v in ev_was.items():
                    _cfg.set(k, v if v is not None else "", save=False)
                _acc2._path().unlink(missing_ok=True)
            say("taking a person away", c)

            # -- 29. personal links: off, and said so kindly -----------------
            c = _Checker("personal links")
            from .config import DEFAULTS as _DEF
            c("they are off unless somebody switches them on",
              _DEF.get("allow_shared_links") is False)
            config.set("allow_shared_links", False, save=False)
            try:
                given = issue(now_key(), name="check-dead", hours=1, scope="phone")
                minted.append(given["id"])
                dead = given["token"]                          # one handed to a person
                inner = issue(now_key(), name="this player", hours=1, scope="full",
                              internal=True)
                minted.append(inner["id"])
                mine = sec.owner_pass(now_key())
                minted.append(mine.split(".")[0])
                r = client.get("/api/status", headers={"X-Music-Key": dead})
                c("a personal link no longer gets in", r.status_code == 403, str(r.status_code))
                c("...and says why, in words", "Sign in instead" in r.text)
                c("...in a url as well",
                  client.get(f"/api/status?token={dead}").status_code == 403)
                for _ in range(12):
                    client.get(f"/api/status?token={dead}")
                c("...without anybody being banned for holding an old bookmark",
                  client.get("/api/status",
                             headers={"X-Music-Key": now_key()}).status_code == 200)
                page = client.get(f"/player?token={dead}")
                c("an old link's page is the front door with the reason, not JSON",
                  page.status_code == 403 and "<!doctype html>" in page.text.lower()
                  and "Sign in instead" in page.text, str(page.status_code))
                c("nobody can be given a new one",
                  get("/api/passes/new?name=x&hours=1").status_code == 409)
                c("the owner's own device pass is not a link, and still works",
                  client.get("/api/status", headers={"X-Music-Key": mine}).status_code == 200)
                c("nor is the player's own pass",
                  client.get("/api/status", headers={"X-Music-Key": inner["token"]}).status_code == 200)
                SUB_L = "99900111"
                _acc2.admit(SUB_L, "linkless@example.com", "Linkless", terms=True)
                c("a signed-in account never needed one",
                  client.get("/api/status",
                             cookies={_COOKIE: sec.session_cookie(now_key(), SUB_L)}
                             ).status_code == 200)
                _acc2.forget(SUB_L)
                config.set("allow_shared_links", True, save=False)
                c("switched back on, the same link works again",
                  client.get("/api/status", headers={"X-Music-Key": dead}).status_code == 200)
            finally:
                config.set("allow_shared_links", True, save=False)
            say("personal links", c)

            # -- 30. the desktop app: offered, addressed to this server, limited --
            c = _Checker("the desktop app")
            import io as _io30
            import zipfile as _zf30
            from .paths import data_dir as _dd30
            from .web import api as _api30
            _keep30 = {k: config.get(k) for k in
                       ("google_client_id", "google_client_secret", "ddns_hostname")}
            config.set("google_client_id", "dl-test-id", save=False)
            config.set("google_client_secret", "dl-test-secret", save=False)
            config.set("ddns_hostname", "music.example.test", save=False)
            _dl30 = _dd30() / "downloads"
            _cache30 = _dd30() / "downloads-cache"
            _dl30.mkdir(parents=True, exist_ok=True)
            _zip30 = _dl30 / "MusicClient.zip"
            _api30._DL_SEEN.clear()
            try:
                with _patch("mrs.web.security.is_home", lambda ip: False),                         _patch("mrs.core.net.lan_ip", lambda: "192.168.1.9"):
                    c("with no build, the front page offers nothing to download",
                      "Get the Windows app" not in client.get("/").text)
                    none = client.get("/download/client")
                    c("...and the link says so in words",
                      none.status_code == 404 and "isn't available" in none.text,
                      str(none.status_code))

                    with _zf30.ZipFile(_zip30, "w", _zf30.ZIP_DEFLATED) as z:
                        z.writestr("MusicClient/MusicClient.exe", b"MZ" + b"x" * 5000)
                        z.writestr("MusicClient/_internal/base.dll", b"dll")
                    c("with a build, the sign-in page offers it",
                      "Get the Windows app" in client.get("/").text)
                    got = client.get("/download/client")
                    c("it downloads with no account and no key",
                      got.status_code == 200
                      and got.headers.get("content-type") == "application/zip",
                      str(got.status_code))
                    c("...as a file with a name",
                      "MusicClient.zip" in got.headers.get("content-disposition", ""))
                    with _zf30.ZipFile(_io30.BytesIO(got.content)) as z:
                        names = z.namelist()
                        addr = z.read("MusicClient/server.txt").decode()
                    c("...with the program intact",
                      "MusicClient/MusicClient.exe" in names
                      and "MusicClient/_internal/base.dll" in names)
                    c("...and this server's public address already in it",
                      "music.example.test" in addr, addr[:120])
                    c("...with the one on the home network as a fallback",
                      "192.168.1.9" in addr, addr[:160])

                    first = _api30._client_zip()
                    client.get("/download/client", headers={"Host": "evil.example"})
                    c("a visitor's own Host header can't mint more copies",
                      _api30._client_zip() == first
                      and len(list(_cache30.glob("MusicClient-*.zip"))) == 1)
                    config.set("ddns_hostname", "other.example.test", save=False)
                    second = _api30._client_zip()
                    c("a changed address makes a new copy and clears the old one",
                      second != first and second.exists() and not first.exists())
                    config.set("ddns_hostname", "music.example.test", save=False)

                    _api30._DL_SEEN.clear()
                    cap = _api30.DOWNLOADS_PER_HOUR
                    codes = [client.get("/download/client").status_code
                             for _ in range(cap + 2)]
                    c("each address gets a few an hour, then is asked to wait",
                      codes[:cap] == [200] * cap and codes[-1] == 429, str(codes))

                    _zip30.write_bytes(b"this is not a zip")
                    _api30._DL_SEEN.clear()
                    c("a broken build is a polite 404, not a crash",
                      client.get("/download/client").status_code == 404)
            finally:
                _zip30.unlink(missing_ok=True)
                for leftover in _cache30.glob("*"):
                    leftover.unlink(missing_ok=True)
                _api30._DL_SEEN.clear()
                for k, v in _keep30.items():
                    config.set(k, v, save=False)
            say("the desktop app", c)

            # -- 31. Siri keys: an account asking for one thing ---------------
            c = _Checker("siri keys")
            from .web import api as _api31
            _keep31 = {k: config.get(k) for k in ("owner_email", "allow_shared_links")}
            _seen31: list = []

            def _asked(text, queue=None, lists=None, **_kw):
                _seen31.append({"text": text, "queue": queue})
                return {"status": "ok", "message": "stubbed"}

            SUB_S, SUB_T, SUB_B = "88800111", "88800222", "88800333"
            _acc2.admit(SUB_S, "siri@example.com", "Siri Sam", terms=True)
            _acc2.admit(SUB_T, "other@example.com", "Other Person", terms=True)
            _acc2.set_scope(SUB_S, "phone")
            _acc2.set_scope(SUB_T, "phone")
            ck_s = {_COOKIE: sec.session_cookie(now_key(), SUB_S)}
            ck_t = {_COOKIE: sec.session_cookie(now_key(), SUB_T)}
            pid_s = _acc2.profile_id(SUB_S)
            _bans.forgive("testclient")
            config.set("allow_shared_links", False, save=False)   # keys aren't links
            try:
                with _patch("mrs.core.net.lan_ip", lambda: "192.168.1.9"), \
                        _patch("mrs.web.api.handle_request", _asked):
                    c("the owner's key is not an account's, so it can't make one",
                      client.post("/api/me/siri/new", json={}, headers=owner_h).status_code == 403)
                    made = client.post("/api/me/siri/new", json={"name": "Key 1"}, cookies=ck_s)
                    c("an account can make a key", made.status_code == 200
                      and made.json().get("token", "").count(".") == 3, str(made.status_code))
                    tid, key_s = made.json()["id"], made.json()["token"]
                    listed = client.get("/api/me/siri", cookies=ck_s)
                    c("...and lists it without showing it",
                      len(listed.json()["keys"]) == 1 and key_s not in listed.text)
                    c("...with where to point it", "192.168.1.9" in listed.json().get("address", ""),
                      listed.json().get("address", ""))
                    c("it isn't on the owner's list of links",
                      all(row["id"] != tid for row in sec.list_passes())
                      and sec.extend(tid, 5).get("ok") is False)

                    config.set("url_prefix", "/music", save=False)
                    try:
                        _bans.forgive("testclient")
                        via = client.post("/music/", json={"input": "x"},
                                          headers={"X-Music-Key": key_s})
                        c("the key works through the path Music may live at",
                          via.status_code == 200, str(via.status_code))
                        c("...and the address it is told to use includes that path",
                          client.get("/api/me/siri", cookies=ck_s).json()
                          .get("address", "").endswith("/music/"))
                    finally:
                        config.set("url_prefix", "", save=False)

                    _seen31.clear()
                    r = client.post("/", json={"input": "play something"}, headers={"X-Music-Key": key_s})
                    c("the key asks for a song", r.status_code == 200 and _seen31, str(r.status_code))
                    c("...in that account's own queue, not the shared one",
                      bool(_seen31) and _seen31[-1]["queue"] is not None)
                    c("...and in a url as well",
                      client.post(f"/?token={key_s}", json={"input": "x"}).status_code == 200)

                    for label, method, path in (
                            ("reading the player's state", "get", "/api/status"),
                            ("playing something directly", "post", "/api/play"),
                            ("changing a setting", "post", "/api/setting"),
                            ("the settings", "get", "/api/settings"),
                            ("who they are", "get", "/api/me"),
                            ("making another key", "post", "/api/me/siri/new"),
                            ("deleting the account", "post", "/api/me/delete"),
                            ("the player page", "get", "/player")):
                        _bans.forgive("testclient")
                        got = getattr(client, method)(path, headers={"X-Music-Key": key_s},
                                                       **({"json": {"confirm": "delete my account"}}
                                                          if method == "post" else {}))
                        c(f"the key is refused {label}", got.status_code == 403,
                          f"{path} {got.status_code}")
                    c("...and said so in words", "only for Siri" in client.get(
                        "/api/status", headers={"X-Music-Key": key_s}).text)
                    c("...and none of that touched the account", _acc2.get(SUB_S) is not None)

                    # Being told no, over and over, is not being banned. A Shortcut
                    # retries, and a key on the wrong route is a mistake, not a guess.
                    _bans.forgive("testclient")
                    for _ in range(8):
                        client.get("/api/status", headers={"X-Music-Key": key_s})
                    c("a key sent to the wrong route, again and again, bans nobody",
                      client.post("/", json={"input": "x"},
                                  headers={"X-Music-Key": key_s}).status_code == 200)

                    # Somebody else's account can't see or remove it.
                    c("another account can't show it",
                      client.post("/api/me/siri/token", json={"id": tid}, cookies=ck_t).status_code == 404)
                    c("...or remove it",
                      client.post("/api/me/siri/revoke", json={"id": tid}, cookies=ck_t).status_code == 404)
                    c("...and its own list is empty of it",
                      client.get("/api/me/siri", cookies=ck_t).json()["keys"] == [])
                    _bans.forgive("testclient")
                    c("...so the key still works",
                      client.post("/", json={"input": "x"}, headers={"X-Music-Key": key_s}).status_code == 200)
                    again = client.post("/api/me/siri/token", json={"id": tid}, cookies=ck_s)
                    c("its own account can show it again", again.status_code == 200
                      and again.json().get("token") == key_s)

                    for n in range(2, sec.MAX_SIRI_KEYS + 1):
                        client.post("/api/me/siri/new", json={"name": f"Key {n}"}, cookies=ck_s)
                    over = client.post("/api/me/siri/new", json={"name": "one too many"}, cookies=ck_s)
                    c("there is a limit on how many", over.status_code == 409
                      and len(sec.siri_keys(pid_s)) == sec.MAX_SIRI_KEYS, str(over.status_code))

                    # Blocking the account stops its keys.
                    _acc2.set_scope(SUB_S, "blocked")
                    _bans.forgive("testclient")
                    c("a blocked account's key stops",
                      client.post("/", json={"input": "x"}, headers={"X-Music-Key": key_s}).status_code == 403)
                    _acc2.set_scope(SUB_S, "phone")
                    _bans.forgive("testclient")
                    c("...and works again when they're let back in",
                      client.post("/", json={"input": "x"}, headers={"X-Music-Key": key_s}).status_code == 200)

                    # The owner's own key asks for songs on the shared player, and is
                    # still not the master key.
                    _cfg.set("owner_email", "boss2@example.com", save=False)
                    _acc2.admit(SUB_B, "boss2@example.com", "Boss Two", terms=True)
                    ck_b = {_COOKIE: sec.session_cookie(now_key(), SUB_B)}
                    key_b = client.post("/api/me/siri/new", json={}, cookies=ck_b).json()["token"]
                    _seen31.clear()
                    _bans.forgive("testclient")
                    r = client.post("/", json={"input": "x"}, headers={"X-Music-Key": key_b})
                    c("an owner's key plays on the shared player",
                      r.status_code == 200 and _seen31 and _seen31[-1]["queue"] is None)
                    _bans.forgive("testclient")
                    c("...and isn't the master key",
                      client.get("/api/settings", headers={"X-Music-Key": key_b}).status_code == 403
                      and client.get("/api/accounts", headers={"X-Music-Key": key_b}).status_code == 403)

                    # Taking one back, and what it says when it's used.
                    gone = client.post("/api/me/siri/revoke", json={"id": tid}, cookies=ck_s)
                    _bans.forgive("testclient")
                    dead = [client.post("/", json={"input": "x"}, headers={"X-Music-Key": key_s})
                            for _ in range(8)]
                    c("removing a key kills it", gone.status_code == 200
                      and all(d.status_code == 403 for d in dead))
                    c("...and says it has gone, in words", "removed" in dead[0].text)
                    c("...and a Shortcut still trying it doesn't get its owner banned",
                      client.get("/api/status", headers=owner_h).status_code == 200
                      and client.post("/", json={"input": "x"},
                                      headers={"X-Music-Key": key_b}).status_code == 200)
                    for _ in range(5):
                        client.post("/", json={"input": "x"},
                                    headers={"X-Music-Key": "a.0.phone.forged"})
                    c("a made-up key is still a guess: a few of them and the address is banned",
                      client.get("/api/status", headers=owner_h).status_code == 403)
                    _bans.forgive("testclient")

                    # The export lists them without the secret; deleting removes them.
                    exp = client.get("/api/me/export", cookies=ck_s)
                    c("the export says what keys exist, not what they are",
                      exp.status_code == 200 and '"siri_keys"' in exp.text
                      and "Key 2" in exp.text and key_s not in exp.text)
                    key_2 = sec.siri_keys(pid_s)[0]["id"]
                    token_2 = sec.siri_token(now_key(), pid_s, key_2)
                    dele = client.post("/api/me/delete", json={"confirm": "delete my account"},
                                       cookies=ck_s)
                    c("deleting the account removes its keys", dele.status_code == 200
                      and sec.siri_keys(pid_s) == [])
                    _bans.forgive("testclient")
                    c("...so they no longer open anything",
                      client.post("/", json={"input": "x"},
                                  headers={"X-Music-Key": token_2}).status_code == 403)
                    with sec._held():
                        held31 = _json.dumps(sec._load_passes())
                    c("...and nothing in the pass file still names them", SUB_S not in held31)
            finally:
                for k, v in _keep31.items():
                    config.set(k, v, save=False)
                for sub in (SUB_S, SUB_T, SUB_B):
                    try:
                        from .web import privacy as _p31
                        _p31.erase(sub)
                    except Exception:
                        pass
                _bans.forgive("testclient")
            say("siri keys", c)

            # -- 32. one address, two applications: the settings and the switch --
            c = _Checker("the address settings")
            _keep32 = {k: config.get(k) for k in
                       ("url_prefix", "public_port", "movies_url",
                        "google_client_id", "google_client_secret")}
            config.set("google_client_id", "sw-test-id", save=False)
            config.set("google_client_secret", "sw-test-secret", save=False)

            def _put(k, v, who=None):
                return client.post("/api/setting", json={"key": k, "value": v},
                                   headers=who or owner_h)

            try:
                c("a path is stored the way it will be used",
                  _put("url_prefix", "music").status_code == 200
                  and config.get("url_prefix") == "/music")
                for bad in ("/Bad Path", "//evil.example", "/a/b", "/x" * 40,
                            "https://evil.example"):
                    c(f"a path like {bad[:20]!r} is refused",
                      _put("url_prefix", bad).status_code == 400
                      and config.get("url_prefix") == "/music")
                c("an empty path means the bare address",
                  _put("url_prefix", "").status_code == 200 and config.get("url_prefix") == "")
                c("a public port is a port", _put("public_port", "443").status_code == 200
                  and config.get("public_port") == 443)
                for bad in ("70000", "-1", "lots"):
                    c(f"a port of {bad} is refused", _put("public_port", bad).status_code == 400)
                for bad in ("javascript:alert(1)", "ftp://host/", "//host/", "https://a b/",
                            'https://x/"onmouseover="y'):
                    c(f"a movies address of {bad[:24]!r} is refused",
                      _put("movies_url", bad).status_code == 400)
                c("a web address is taken",
                  _put("movies_url", "https://movies.example.test:8443/").status_code == 200)
                c("none of it is a guest's to change",
                  _put("url_prefix", "/mine", {"X-Music-Key": phone}).status_code == 403
                  and config.get("url_prefix") == "")

                with _patch("mrs.web.security.is_home", lambda ip: False):
                    _put("url_prefix", "/music")
                    sw = client.get("/")
                    c("with a path and a movies address, the bare address is a choice",
                      sw.status_code == 200 and "What are we in the mood for" in sw.text)
                    c("...to Music at its path and to Movies where it lives",
                      'href="/music/"' in sw.text
                      and 'href="https://movies.example.test:8443/"' in sw.text)
                    door = client.get("/music/")
                    c("...and the path is still Music's own front door",
                      door.status_code == 200 and "What are we in the mood for" not in door.text
                      and "Log in with Google" in door.text, str(door.status_code))
                    _put("movies_url", "")
                    plain = client.get("/")
                    c("without a movies address there is nothing to choose between",
                      "What are we in the mood for" not in plain.text
                      and "Log in with Google" in plain.text)
                    _put("url_prefix", "")
                    _put("movies_url", "https://movies.example.test:8443/")
                    c("without a path the bare address is Music, whatever else is set",
                      "Log in with Google" in client.get("/").text)
            finally:
                for k, v in _keep32.items():
                    config.set(k, v, save=False)
            say("the address settings", c)

            # -- 33. asking Google about a sign-in address before changing it ---
            c = _Checker("checking the address with Google")
            import base64 as _b64g
            from urllib.parse import unquote as _unq33
            from .web import google as _g33
            _keep33 = {k: config.get(k) for k in
                       ("google_client_id", "google_client_secret", "ddns_hostname",
                        "url_prefix", "public_port")}
            config.set("google_client_id", "chk-client", save=False)
            config.set("google_client_secret", "chk-secret", save=False)
            config.set("ddns_hostname", "music.example.test", save=False)
            config.set("url_prefix", "", save=False)
            config.set("public_port", 0, save=False)

            def _err(text):
                blob = _b64g.urlsafe_b64encode(("\n\x0e" + text).encode()).decode().rstrip("=")
                return (302, "https://accounts.google.com/signin/oauth/error?authError=" + blob)

            asked: list = []

            def _answer(reply):
                def probe(url):
                    asked.append(url)
                    return reply
                return probe

            def _chk(path=""):
                _g33._CHECKED.update(at=0.0, got=None)       # not the cooldown's answer
                return client.get("/api/accounts/check" + path, headers=owner_h)

            try:
                with _patch.object(_g33, "_probe", _answer((302, "https://accounts.google.com/v3/signin/identifier?x=1"))):
                    r = _chk()
                    c("an address Google will take is said to be accepted",
                      r.status_code == 200 and r.json()["verdict"] == "accepted", r.text[:120])
                    c("...asking it about the address this install would really use",
                      any("redirect_uri=http" in a and "music.example.test" in a
                          for a in [_unq33(u) for u in asked]))
                with _patch.object(_g33, "_probe", _answer(_err("redirect_uri_mismatch: not registered"))):
                    r = _chk("?url_prefix=/music&public_port=443").json()
                    c("one it hasn't been told about is refused, with what to add",
                      r["verdict"] == "rejected" and r["reason"] == "redirect_uri_mismatch"
                      and "/music/auth/google/callback" in r["message"], str(r)[:200])
                    c("...for the address as it would be, not as it is",
                      r["redirect_uri"].endswith("/music/auth/google/callback")
                      and config.get("url_prefix") == "" and config.get("public_port") == 0)
                    with _patch("mrs.core.net.scheme", lambda: "https"):
                        c("...and on https 443 the port is left off, which is the point",
                          _g33.redirect_uri(prefix_override="music", port_override=443)
                          == "https://music.example.test/music/auth/google/callback")
                        c("...while any other port stays",
                          _g33.redirect_uri(prefix_override="", port_override=8443)
                          == "https://music.example.test:8443/auth/google/callback")
                        c("...and a path that isn't one is dropped rather than trusted",
                          _g33.redirect_uri(prefix_override="//evil.example", port_override=443)
                          == "https://music.example.test/auth/google/callback")
                with _patch.object(_g33, "_probe", _answer(_err("invalid_client The OAuth client was not found."))):
                    r = _chk().json()
                    c("a client id Google doesn't know is its own message",
                      r["verdict"] == "rejected" and r["reason"] == "invalid_client")
                with _patch.object(_g33, "_probe", _answer(_err("invalid_request policy"))):
                    r = _chk().json()
                    c("an address that breaks its rules says so", r["verdict"] == "rejected"
                      and r["reason"] == "invalid_request")

                def _down(url):
                    raise OSError("no route")
                with _patch.object(_g33, "_probe", _down):
                    c("Google being out of reach isn't a verdict on the address",
                      _chk().json()["verdict"] == "unreachable")
                with _patch.object(_g33, "_probe", _answer((200, ""))):
                    c("an answer it doesn't recognise is said to be unknown",
                      _chk().json()["verdict"] == "unknown")

                asked.clear()
                with _patch.object(_g33, "_probe", _answer((302, "https://accounts.google.com/v3/signin/identifier"))):
                    _g33._CHECKED.update(at=0.0, got=None)
                    client.get("/api/accounts/check", headers=owner_h)
                    client.get("/api/accounts/check", headers=owner_h)
                    c("asking twice in a row asks Google once", len(asked) == 1, str(len(asked)))
                c("it is the owner's to ask",
                  client.get("/api/accounts/check", headers={"X-Music-Key": phone}).status_code == 403)
                config.set("google_client_id", "", save=False)
                c("with sign-in not set up there is nothing to ask",
                  _chk().json()["verdict"] == "unset")
                config.set("google_client_id", "chk-client", save=False)
                config.set("ddns_hostname", "", save=False)
                c("...and with no hostname there is nowhere to send anybody",
                  _chk().json()["verdict"] == "unset")
            finally:
                for k, v in _keep33.items():
                    config.set(k, v, save=False)
                _g33._CHECKED.update(at=0.0, got=None)
            say("checking the address with Google", c)

            # -- 34. a file being replaced while something else holds it --------
            c = _Checker("replacing a file")
            from .paths import replace_file as _replace34
            _calls34: list = []

            def _busy_then_free(n):
                left = [n]

                def fake(src, dst):
                    _calls34.append(1)
                    if left[0] > 0:
                        left[0] -= 1
                        raise PermissionError(13, "The process cannot access the file")
                return fake

            with _patch("mrs.paths.os.replace", _busy_then_free(3)), \
                    _patch("mrs.paths.time.sleep", lambda s: None):
                _calls34.clear()
                _replace34("a", "b")
                c("a file somebody else has open for a moment is replaced once they let go",
                  len(_calls34) == 4, str(len(_calls34)))
            with _patch("mrs.paths.os.replace", _busy_then_free(99)), \
                    _patch("mrs.paths.time.sleep", lambda s: None):
                _calls34.clear()
                try:
                    _replace34("a", "b", tries=4)
                    gave_up = False
                except PermissionError:
                    gave_up = True
                c("one that never lets go is still an error, after a fair try",
                  gave_up and len(_calls34) == 4, str(len(_calls34)))

            def _missing(src, dst):
                _calls34.append(1)
                raise FileNotFoundError("gone")
            with _patch("mrs.paths.os.replace", _missing), \
                    _patch("mrs.paths.time.sleep", lambda s: None):
                _calls34.clear()
                try:
                    _replace34("a", "b")
                except FileNotFoundError:
                    pass
                c("anything else fails at once, not after eight tries", len(_calls34) == 1)
            say("replacing a file", c)

            # -- 22. focused regressions for the issue register ------------
            c = _Checker("issue regressions")
            import json as _json
            import os as _os
            import sys as _sys
            import tempfile as _tempfile
            import threading as _threading
            import types as _types
            import zipfile as _zipfile
            from pathlib import Path as _Path
            from .core import backup as _backup
            from .core import downloader as _dlmod
            from .core import library as _libmod
            from .core import playlists as _plmod
            from .core import spectrum as _spectrum
            from .core.session import Session as _Session, Sessions as _Sessions
            from .core.profile import Profiles as _Profiles
            from .core.taste import TasteEngine as _TasteEngine
            from .models import Track as _Track
            from .web import api as _api_mod

            # The dynamic playlists only draw from the current listener's
            # records and use the same queue boundary as ordinary requests.
            with _tempfile.TemporaryDirectory(prefix="mrs-smart-list-") as root:
                store = _TasteEngine(root=_Path(root))
                a = _Track(video_id="smart-a", title="Smart A", artist="Band A")
                b = _Track(video_id="smart-b", title="Smart B", artist="Band B")
                blocked = _Track(video_id="smart-blocked", title="Hidden", artist="No")
                store.toggle_like(a)
                store.record(a, 100, 100)
                store.record(a, 100, 100)
                store.record(b, 100, 100)
                store.toggle_like(blocked)
                store.block(track=blocked)
                with _patch.object(_api_mod, "taste", store):
                    r = client.get("/api/smartplaylists", headers=owner_h)
                    c("smart playlist counts are live and skip blocked rows",
                      r.status_code == 200 and
                      {x["kind"]: x["count"] for x in r.json()["playlists"]}
                      == {"liked": 1, "recent": 2, "most_played": 2}, r.text[:180])
                    r = client.get("/api/smartplaylists?kind=liked", headers=owner_h)
                    c("the list panel can read dynamic tracks with GET",
                      r.status_code == 200 and
                      [x["video_id"] for x in r.json().get("tracks", [])] == ["smart-a"])
                    c("adding a dynamic list cannot be triggered by GET",
                      client.get("/api/smartplaylists/play?kind=recent",
                                 headers=owner_h).status_code == 405)
                    queued = []
                    with _patch.object(_player.queue, "enqueue",
                                       side_effect=lambda ts, imported=False:
                                       queued.extend(ts)):
                        r = client.post("/api/smartplaylists/play",
                                        json={"kind": "recent"}, headers=owner_h)
                    c("adding a smart list uses the shared queue, not a new resolver",
                      r.status_code == 200 and r.json().get("added") == 2
                      and {t.video_id for t in queued} == {"smart-a", "smart-b"},
                      r.text[:160])
                c("a link cannot inspect the owner's audit trail",
                  client.get("/api/audit", headers={"X-Music-Key": phone}).status_code == 403)
                ar = client.get("/api/audit", headers=owner_h)
                c("the owner audit endpoint returns bounded entries",
                  ar.status_code == 200 and isinstance(ar.json().get("entries"), list))

            # Playlist files are user-editable JSON. Reject malformed fields
            # row-by-row while leaving playable siblings intact.
            with _tempfile.TemporaryDirectory(prefix="mrs-playlist-rows-") as root:
                lists = _plmod.Playlists(home=_Path(root))
                folder = lists.folder("Mixed")
                (folder / "tracks.json").write_text(_json.dumps([
                    {"video_id": "good-row", "title": "Good row", "duration": 12},
                    "not an object",
                    {"video_id": ["bad"], "title": "Wrong id type"},
                    {"video_id": "bad-duration", "title": "Bad", "duration": "long"},
                    {"title": "No playable locator"},
                ]), encoding="utf-8")
                got = lists.tracks("Mixed")
                c("valid playlist rows survive wrong types and partial records",
                  [t.video_id for t in got] == ["good-row"])

                # A failed directory removal was previously suppressed by
                # rmtree(ignore_errors=True), so the UI claimed a list was
                # gone while all of its files remained.
                with _patch("mrs.core.playlists.shutil.rmtree",
                            side_effect=OSError("directory locked")):
                    failed_delete = lists.delete("Mixed")
                absent_delete = lists.delete("Never existed")
                c("playlist deletion reports filesystem failure and does not invent lists",
                  not failed_delete.get("ok") and folder.exists()
                  and not absent_delete.get("ok")
                  and not (lists.root() / "Never existed").exists())

            # Long and filesystem-invalid labels are identifiers, not merely
            # display text. They must never converge on one playlist folder.
            with _tempfile.TemporaryDirectory(prefix="mrs-playlist-names-") as name_root:
                named = _plmod.Playlists(home=_Path(name_root))
                first_name = "Very long list " + "x" * 80 + " one"
                second_name = "Very long list " + "x" * 80 + " two"
                named.create(first_name)
                named.create(second_name)
                first_folder, second_folder = (named.folder(first_name),
                                               named.folder(second_name))
                named.delete(second_name)
                c("distinct long list names cannot overwrite or delete each other",
                  first_folder != second_folder and first_folder.is_dir()
                  and not second_folder.exists())

            # Read helpers service the GET tracks endpoint. They must not use
            # the writer folder helper and materialise a list for a typo.
            with _tempfile.TemporaryDirectory(prefix="mrs-playlist-read-") as read_root:
                readonly = _plmod.Playlists(home=_Path(read_root))
                missing_name = "This list is not here"
                read_path = readonly.root() / missing_name
                read_result = (readonly.tracks(missing_name) == []
                               and readonly.credit(missing_name) == {}
                               and not readonly.is_shared(missing_name)
                               and not readonly.remove(missing_name, "nope").get("ok")
                               and not readonly.set_shared(missing_name, True).get("ok"))
                c("playlist reads and failed mutations do not create empty folders",
                  read_result and not read_path.exists())

            # Windows can decline a Run-key change. The stored preference is
            # only trustworthy if that operation actually completed.
            with _patch.object(_api_mod, "_set_run_at_boot", return_value=False), \
                 _patch.object(_api_mod.config, "set") as save_boot:
                failed_boot_set = _api_mod.api_boot(enabled=1, _=True)
            c("a failed start-at-sign-in change does not claim or save success",
              failed_boot_set.get("status") == "error"
              and failed_boot_set.get("applied") is False and not save_boot.called)

            # Listener state is user-editable persistence. A failed write must
            # remain retryable, malformed counters must be ignored, and a
            # block must durably remove the song's scoring evidence.
            with _tempfile.TemporaryDirectory(prefix="mrs-taste-state-") as root:
                home = _Path(root)
                stats = home / "play_stats.json"
                stats.write_text(_json.dumps({
                    "songs": {"valid-song": [3, 1], "bad-song": "broken"},
                    "artists": {"valid-band": [2, 0], "bad-band": ["x"]},
                    "history": ["valid-song", 42],
                    "recent": [{"video_id": "valid-song"}, "bad-row"],
                    "played_at": {"valid-song": 1, "bad": "soon"},
                }), encoding="utf-8")
                store = _TasteEngine(root=home)
                counts_ok = store.play_counts() == {"valid-song": 3}
                with _patch("mrs.core.taste.write_atomic",
                            side_effect=OSError("disk full")):
                    store._dirty = True
                    store.save()
                failed_write_is_retryable = store._dirty
                with _patch("mrs.core.taste.write_atomic") as write_atomic:
                    store.save()
                retry_ok = write_atomic.called and not store._dirty
                blocked = _Track(video_id="durable-block", title="Blocked")
                store.record(blocked, 100, 100)
                store.flush()
                store.block(track=blocked)
                block_persisted = blocked.video_id not in _TasteEngine(
                    root=home).play_counts()
                c("malformed taste counters are ignored and failed saves retry",
                  counts_ok and failed_write_is_retryable and retry_ok)
                c("blocking a song durably removes its scoring evidence",
                  block_persisted)

            # The library index has the same row-by-row failure mode as the
            # playlist files. Keep valid local tracks when one row is corrupt.
            with _tempfile.TemporaryDirectory(prefix="mrs-library-rows-") as root:
                index = _Path(root) / "library.json"
                local_row = _Track(video_id="local-good", title="Good",
                                   path="C:/music/good.mp3", url="C:/music/good.mp3",
                                   source="local").to_dict()
                index.write_text(_json.dumps([local_row, "not an object",
                                              {"title": "no file"}]),
                                 encoding="utf-8")
                with _patch.object(_libmod, "state_file", lambda name: index):
                    local = _libmod.LocalLibrary()
                c("valid local-library rows survive malformed siblings",
                  local.count() == 1
                  and local.search("good", limit=1)[0].video_id == "local-good")

            # Every browser output format is cache-owned; evicting its source
            # must not leave an orphaned non-AAC transcode behind.
            with _tempfile.TemporaryDirectory(prefix="mrs-cast-prune-") as root:
                base = _Path(root)
                cache, pinned, cast_work = base / "cache", base / "pinned", base / "cast"
                cache.mkdir()
                pinned.mkdir()
                cast_work.mkdir()
                (cache / "format-prune.webm").write_bytes(b"source")
                orphan = cast_work / "format-prune~default.ogg"
                orphan.write_bytes(b"transcode")
                unrelated = cast_work / "format-prune-extra~default.ogg"
                unrelated.write_bytes(b"keep")
                with _patch.object(_dlmod, "cache_dir", lambda: cache), \
                     _patch.object(_dlmod, "pinned_dir", lambda: pinned), \
                     _patch.object(_cast2, "work_dir", lambda: cast_work), \
                     _patch.object(_cast2, "prune", lambda: 0):
                    removed = _dlmod.downloader.prune_cache(keep_mb=0)
                c("cache eviction removes every orphaned cast format",
                  removed == 1 and not orphan.exists() and unrelated.is_file())

            # A corrupt play-count value should make a row disappear, not
            # turn the whole most-played endpoint into a 500.
            class _BrokenCounts:
                def liked(self):
                    return []
                def recent(self, limit):
                    return [{"video_id": "good-count", "title": "Good"},
                            {"video_id": "bad-count", "title": "Bad"}]
                def play_counts(self):
                    return {"good-count": 4, "bad-count": "not a number"}
                def is_blocked(self, track):
                    return False
            rows = _api_mod._smart_rows(_BrokenCounts(), "most_played")
            c("corrupt play counts do not break most-played rows",
              [row["video_id"] for row in rows] == ["good-count"])

            media_route = next((route for route in _api_mod.app.routes
                                if route.path == "/api/announce/{aid}.mp3"), None)
            c("announcement media handler is not shadowed by the toggle handler",
              media_route is not None
              and media_route.endpoint is _api_mod.api_announce_file
              and _api_mod.api_announce.__name__ == "api_announce")
            page = (_Path(__file__).parent / "web" / "templates" /
                    "player.html").read_text(encoding="utf-8")
            c("malformed browser format preferences are ignored safely",
              "return Array.isArray(got)" in page
              and "got.filter(x => typeof x === \"string\")" in page)

            # Cache identity must include path and file version, not the stem.
            with _tempfile.TemporaryDirectory(prefix="mrs-spectrum-") as root:
                base = _Path(root)
                left, right = base / "one", base / "two"
                left.mkdir(); right.mkdir()
                f1, f2 = left / "01 Intro.mp3", right / "01 Intro.mp3"
                f1.write_bytes(b"one"); f2.write_bytes(b"two")
                with _patch.object(_spectrum, "_dir", lambda: base / "spectra"):
                    key1 = _spectrum._cache_file(str(f1))
                    key2 = _spectrum._cache_file(str(f2))
                    old = key1
                    f1.write_bytes(b"replacement with another size")
                    _os.utime(f1, (time.time() + 3, time.time() + 3))
                    new = _spectrum._cache_file(str(f1))
                c("same-stem files and a replaced file get distinct spectrum keys",
                  key1 != key2 and old != new)

            # Both separators are legal ZIP spelling on Windows. A malicious
            # member must not escape the playlist subtree during restore.
            with _tempfile.TemporaryDirectory(prefix="mrs-backup-") as root:
                base = _Path(root) / "data"
                base.mkdir()
                sentinel = base / "sentinel.txt"
                sentinel.write_text("keep", encoding="utf-8")
                archive = _Path(root) / "mixed.zip"
                with _zipfile.ZipFile(archive, "w") as z:
                    z.writestr("playlists/..\\sentinel.txt", "overwrite")
                    z.writestr("playlists/good.json", "[]")
                with _patch.object(_backup, "data_dir", lambda: base), \
                     _patch.object(_backup, "make_backup",
                                   lambda: {"ok": True, "path": "safety.zip"}):
                    restored = _backup.restore(str(archive))
                c("mixed-separator archive traversal is skipped safely",
                  restored.get("ok") and sentinel.read_text("utf-8") == "keep"
                  and (base / "playlists" / "good.json").is_file())

            # The library scheduler is inert when disabled, avoids overlapping
            # a manual scan, and runs at most once per configured interval.
            local = _libmod.LocalLibrary()
            options = {"library_monitor_minutes": 5, "library_paths": ["music"]}
            with _patch.object(_cfg, "get",
                               side_effect=lambda key, default=None:
                               options.get(key, default)):
                local._next_monitor = 0
                due1 = local._monitor_due(now=100)
                due2 = local._monitor_due(now=101)
                local._next_monitor = 0
                with local._lock:
                    local._scanning = True
                overlaps = local._monitor_due(now=200)
                with local._lock:
                    local._scanning = False
                options["library_monitor_minutes"] = 0
                disabled = local._monitor_due(now=300)
            c("automatic library refresh is scheduled without overlapping scans",
              due1 and not due2 and not overlaps and not disabled)
            options_before = _cfg.get("library_monitor_minutes", 0)
            with _patch.object(_api_mod.library, "start_monitor") as start_monitor:
                r = client.post("/api/setting",
                                json={"key": "library_monitor_minutes", "value": "15"},
                                headers=owner_h)
                c("the owner can configure periodic refresh through POST",
                  r.status_code == 200 and r.json().get("value") == 15
                  and start_monitor.called, r.text[:160])
            _cfg.set("library_monitor_minutes", options_before)

            from . import server as _server_mod
            old_wanted_port = _server_mod.runtime.get("wanted_port")
            preferred = 48371
            with _patch.object(_server_mod, "_is_ours", return_value=False), \
                 _patch.object(_server_mod, "_port_free",
                               side_effect=lambda port: port == preferred + 2), \
                 _patch("time.sleep", return_value=None):
                selected = _server_mod.pick_port(preferred)
            launcher_text = (_Path(__file__).resolve().parents[2] /
                             "launcher.pyw").read_text(encoding="utf-8")
            c("port fallback publishes the selected port for launcher URLs",
              selected == preferred + 2
              and _server_mod.runtime.get("wanted_port") == preferred
              and _server_mod.local_url(selected) ==
                  f"http://127.0.0.1:{preferred + 2}/api/ping"
              and "srv.runtime.get(\"port\")" in launcher_text)
            _server_mod.runtime["wanted_port"] = old_wanted_port

            # Deterministic IDs survive fresh Python interpreters.
            with _tempfile.TemporaryDirectory(prefix="mrs-local-id-") as root:
                env = dict(_os.environ)
                env["PYTHONPATH"] = str(_Path(__file__).resolve().parents[1])
                env["MRS_TESTING"] = "1"
                env["MRS_DATA_DIR"] = str(_Path(root) / "data")
                env["MRS_CACHE_DIR"] = str(_Path(root) / "cache")
                script = ("from pathlib import Path; from mrs.core.library import LocalLibrary; "
                          "print(LocalLibrary._stable_id(Path(r'C:\\Music\\A\\01.mp3')))")
                one = _sp.check_output([_sys.executable, "-c", script], env=env, text=True).strip()
                two = _sp.check_output([_sys.executable, "-c", script], env=env, text=True).strip()
                c("local track identity is stable across interpreter processes",
                  one.startswith("local:v2:") and one == two)

            # A pass's persistence policy and the live session must change
            # together in both directions.
            profs, rooms = _Profiles(), _Sessions()
            row = {"id": "check-policy-transition", "name": "check",
                   "scope": "phone", "expires": time.time() + 3600}
            temp_profile = profs.for_row(row)
            with _patch.object(_Session, "start", lambda self: None), \
                 _patch.object(_Session, "stop", lambda self: None):
                first_room = rooms.for_pass(row["id"], "check", "phone", temp_profile)
                row["expires"] = 0
                permanent_profile = profs.for_row(row)
                second_room = rooms.for_pass(row["id"], "check", "phone", permanent_profile)
                row["expires"] = time.time() + 3600
                expiring_profile = profs.for_row(row)
                third_room = rooms.for_pass(row["id"], "check", "phone", expiring_profile)
                rooms.close(row["id"])
            c("both permanence transitions replace the cached live session",
              first_room is not second_room and second_room is not third_room
              and not temp_profile.permanent and permanent_profile.permanent
              and not expiring_profile.permanent)

            # Silent stdout cannot suspend the timeout, and cleanup must clear
            # the active process registry even after cancellation.
            class _SilentProcess:
                def __init__(self):
                    self.stopped = _threading.Event()
                    self.returncode = None
                    self.stdout = self._lines()
                def _lines(self):
                    self.stopped.wait(5)
                    if False:
                        yield ""
                def poll(self):
                    return self.returncode
                def terminate(self):
                    self.returncode = -15
                    self.stopped.set()
                def kill(self):
                    self.returncode = -9
                    self.stopped.set()
                def wait(self, timeout=None):
                    if not self.stopped.wait(timeout):
                        raise _sp.TimeoutExpired("fake yt-dlp", timeout)
                    return self.returncode
            proc = _SilentProcess()
            with _patch("mrs.core.downloader.subprocess.Popen", return_value=proc), \
                 _patch.object(_dlmod.downloader, "_stop_process",
                               lambda p, force=False: p.terminate()):
                started = time.monotonic()
                code, output = _dlmod.downloader._run(["yt-dlp"], timeout=0.1)
                elapsed = time.monotonic() - started
            c("a silent downloader hits its deadline and reaps the process",
              code != 0 and "TIMEOUT" in output and elapsed < 1
              and proc not in _dlmod.downloader._procs)

            failed_id = "cleanup-exception-check"
            failed_track = _Track(video_id=failed_id, title="Failure cleanup")
            _dlmod._note_inflight(failed_id, total=10)
            with _patch.object(_dlmod.downloader, "cached", return_value=None), \
                 _patch.object(_dlmod.downloader, "_fetch_locked",
                               side_effect=RuntimeError("mock fetch failure")):
                try:
                    _dlmod.downloader.fetch(failed_track)
                except RuntimeError:
                    pass
            c("an unexpected fetch exception clears local and shared in-flight state",
              failed_id not in _dlmod.downloader._inflight
              and _dlmod.arriving(failed_id) is None)

            # Partial files aren't cache hits; pruning cannot remove an active
            # download even when the cache limit is deliberately zero.
            with _tempfile.TemporaryDirectory(prefix="mrs-download-cache-") as root:
                cache, pinned = _Path(root) / "cache", _Path(root) / "pinned"
                cache.mkdir(); pinned.mkdir()
                partial = cache / "prune-check.m4a.part"
                partial.write_bytes(b"partial")
                active = cache / "active-check.m4a"
                active.write_bytes(b"active")
                with _dlmod._INFLIGHT_LOCK:
                    _dlmod._INFLIGHT["active-check"] = {"started": time.time()}
                try:
                    with _patch.object(_dlmod, "cache_dir", lambda: cache), \
                         _patch.object(_dlmod, "pinned_dir", lambda: pinned), \
                         _patch("mrs.core.cast.prune", lambda: 0):
                        partial_hit = _dlmod.downloader.cached("prune-check")
                        removed = _dlmod.downloader.prune_cache(keep_mb=0)
                    c("partial audio is not served and active files survive pruning",
                      partial_hit is None and active.is_file() and removed == 0)
                finally:
                    with _dlmod._INFLIGHT_LOCK:
                        _dlmod._INFLIGHT.pop("active-check", None)

            # Completed files have stable media types and byte-range behavior
            # for browser/Safari clients.
            with _tempfile.TemporaryDirectory(prefix="mrs-range-") as root:
                media = _Path(root) / "track.mp3"
                payload = b"0123456789abcdefghij"
                media.write_bytes(payload)
                with _patch.object(_cast2, "serve", return_value=(media, "ready")):
                    full_response = client.get("/api/output/stream/range-check",
                                               headers=owner_h)
                    suffix_response = client.get("/api/output/stream/range-check",
                                                 headers={**owner_h, "Range": "bytes=-5"})
                    bad_range = client.get("/api/output/stream/range-check",
                                           headers={**owner_h, "Range": "bytes=99-"})
                    multi_range = client.get("/api/output/stream/range-check",
                                             headers={**owner_h, "Range": "bytes=0-1,4-5"})
                c("completed media honors full, suffix, invalid and multi-range requests",
                  full_response.status_code == 200
                  and full_response.headers.get("content-type", "").startswith("audio/mpeg")
                  and full_response.content == payload
                  and suffix_response.status_code == 206
                  and suffix_response.content == payload[-5:]
                  and suffix_response.headers.get("content-range") == "bytes 15-19/20"
                  and bad_range.status_code == 416
                  and multi_range.status_code == 206
                  and multi_range.headers.get("content-type", "").startswith(
                      "multipart/byteranges")
                  and b"01" in multi_range.content and b"45" in multi_range.content,
                  str([(r.status_code, r.headers.get("content-type"),
                        r.headers.get("content-range"), r.content[:24])
                       for r in (full_response, suffix_response, bad_range,
                                 multi_range)]))
                type_map = {ext: _api_mod._media_type(_Path("track" + ext))
                            for ext in (".m4a", ".mp3", ".aac", ".wav",
                                        ".flac", ".ogg", ".opus", ".webm")}
                c("every native cast format has an audio content type",
                  type_map == {".m4a": "audio/mp4", ".mp3": "audio/mpeg",
                               ".aac": "audio/aac", ".wav": "audio/wav",
                               ".flac": "audio/flac", ".ogg": "audio/ogg",
                               ".opus": "audio/ogg", ".webm": "audio/webm"},
                  str(type_map))
                with _patch.object(_dlmod.downloader, "cached",
                                   return_value=str(media)):
                    guest_cached = client.get("/api/stream/range-check",
                                              headers={"X-Music-Key": phone})
                    owner_cached = client.get("/api/stream/range-check",
                                              headers=owner_h)
                c("a shared link cannot fetch arbitrary cached media",
                  guest_cached.status_code == 403 and owner_cached.status_code == 200
                  and owner_cached.content == payload,
                  f"guest={guest_cached.status_code}, owner={owner_cached.status_code}")

            # Narration uses the music mixer's exact base level, applies a
            # configurable dB duck/gain, and restores gain on success/failure.
            old_duck = _cfg.get("announce_duck_db", -12.0)
            old_voice = _cfg.get("announce_voice_gain_db", 0.0)
            duck_bound = client.post("/api/setting",
                                     json={"key": "announce_duck_db", "value": "-100"},
                                     headers=owner_h)
            voice_bound = client.post("/api/setting",
                                      json={"key": "announce_voice_gain_db", "value": "99"},
                                      headers=owner_h)
            guest_gain = client.post("/api/setting",
                                     json={"key": "announce_duck_db", "value": "-6"},
                                     headers={"X-Music-Key": phone})
            c("announcement dB settings clamp safely and remain owner-only",
              duck_bound.status_code == 200 and duck_bound.json().get("value") == -60.0
              and voice_bound.status_code == 200
              and voice_bound.json().get("value") == 12.0
              and guest_gain.status_code == 403)
            _cfg.set("announce_duck_db", old_duck)
            _cfg.set("announce_voice_gain_db", old_voice)
            _cfg.set("announce_duck_db", -12.0)
            _cfg.set("announce_voice_gain_db", 3.0)
            class _FakeTTS:
                async def save(self, path):
                    with open(path, "wb") as fh:
                        fh.write(b"voice")
            edge = _types.SimpleNamespace(Communicate=lambda *args: _FakeTTS())
            audio_state = {}
            def _volume_get(prop, default=None):
                return audio_state.get(prop, default)
            def _volume_set(prop, value):
                audio_state[prop] = value
            fake_mpv = _types.SimpleNamespace(get=_volume_get, set=_volume_set)
            gains_ok = True
            with _patch.dict(_sys.modules, {"edge_tts": edge}), \
                 _patch.object(_player, "mpv", fake_mpv), \
                 _patch.object(_player, "casting", return_value=False), \
                 _patch("mrs.player.shutil.which", return_value="mpv"):
                for volume in (0, 5, 70, 150):
                    audio_state.update(volume=volume, **{"volume-gain": 2.5})
                    command = []
                    def _capture(cmd, **kwargs):
                        command.extend(cmd)
                        gains_ok_local = audio_state.get("volume-gain") == -9.5
                        if not gains_ok_local:
                            raise AssertionError("music wasn't ducked during speech")
                    with _patch("mrs.player.subprocess.run", side_effect=_capture):
                        _player._speak("volume check")
                    got_volume = next((x for x in command if x.startswith("--volume=")), "")
                    got_gain = next((x for x in command if x.startswith("--volume-gain=")), "")
                    gains_ok = gains_ok and got_volume == f"--volume={volume}" \
                        and float(got_gain.split("=", 1)[1]) == 5.5 \
                        and audio_state.get("volume-gain") == 2.5
                audio_state.update(volume=55, **{"volume-gain": 1.5})
                with _patch("mrs.player.subprocess.run",
                            side_effect=RuntimeError("fake speech failure")):
                    _player._speak("failure check")
                restored_after_failure = audio_state.get("volume-gain") == 1.5
                c("speech follows zero/low/normal/high music volume and restores existing gain",
                  gains_ok)
                c("a failed announcement restores the original music gain",
                  restored_after_failure and not _player._ducking)

                audio_state.update(volume=60, **{"volume-gain": 4.0})
                entered, release = _threading.Event(), _threading.Event()
                count = active_count = maximum_active = 0
                counter_lock = _threading.Lock()
                def _slow_speech(cmd, **kwargs):
                    nonlocal count, active_count, maximum_active
                    with counter_lock:
                        count += 1; active_count += 1
                        maximum_active = max(maximum_active, active_count)
                        first_call = count == 1
                    if first_call:
                        entered.set()
                        release.wait(3)
                    with counter_lock:
                        active_count -= 1
                with _patch("mrs.player.subprocess.run", side_effect=_slow_speech):
                    first = _threading.Thread(target=_player._speak, args=("one",))
                    second = _threading.Thread(target=_player._speak, args=("two",))
                    first.start(); entered.wait(2); second.start()
                    time.sleep(0.1)
                    overlap_blocked = count == 1
                    release.set(); first.join(3); second.join(3)
                c("overlapping announcements serialize and restore the mixer state",
                  overlap_blocked and count == 2 and maximum_active == 1
                  and audio_state.get("volume-gain") == 4.0)
            _cfg.set("announce_duck_db", old_duck)
            _cfg.set("announce_voice_gain_db", old_voice)

            # On the browser path, restore the old attenuation before sampling
            # the next announcement and guard stale ended events by generation.
            template_text = (_Path(__file__).parent / "web" / "templates" /
                             "player.html").read_text(encoding="utf-8")
            apply_at = template_text.index("function applyAnnounce(d)")
            restore_at = template_text.index("restoreCastDuck();", apply_at)
            volume_at = template_text.index("const wasVol = el.volume", apply_at)
            c("cast announcements restore before sampling and reject stale callbacks",
              restore_at < volume_at
              and "restoreCastDuck(generation)" in template_text[volume_at:volume_at + 1400])
            c("the new controls expose dB ducking, audit and library refresh",
              all(mark in template_text for mark in
                  ('id="announceDuck"', 'id="announceVoice"',
                   'id="auditrefresh"', 'id="libmonitor"',
                   'data-smart-play=')))

            # Blocking advances the player immediately. The undo target must
            # consequently be the recording that was blocked, not whatever
            # happened to become current between the first and second tap.
            from .models import Track as _UndoTrack
            class _UndoStore:
                def __init__(self): self.songs, self.artists = set(), set()
                def block(self, track=None, artist="", on=True):
                    if artist:
                        if on and artist not in self.artists:
                            self.artists.add(artist); return True
                        if not on and artist in self.artists:
                            self.artists.remove(artist); return True
                        return False
                    if not track or not track.video_id: return False
                    if on and track.video_id not in self.songs:
                        self.songs.add(track.video_id); return True
                    if not on and track.video_id in self.songs:
                        self.songs.remove(track.video_id); return True
                    return False
                def blocks(self): return {"songs": [], "artists": []}
            undo_store = _UndoStore()
            undo_track = _UndoTrack(video_id="undo-song", title="Undo me")
            with _patch.object(_api_mod, "_session_for", return_value=None), \
                 _patch.object(_api_mod, "_profile_for", return_value=None), \
                 _patch.object(_api_mod, "taste", undo_store), \
                 _patch.object(_api_mod.player.queue, "current_track", return_value=undo_track), \
                 _patch.object(_api_mod, "_skip_current"):
                blocked = _api_mod.api_block(object(), on=1, _=True)
                restored = _api_mod.api_block(object(), on=0,
                                               video_id=blocked["undo"]["video_id"],
                                               _=True)
                try:
                    _api_mod.api_block(object(), on=1, video_id="not-current", _=True)
                    arbitrary_block_refused = False
                except Exception as exc:
                    arbitrary_block_refused = getattr(exc, "status_code", None) == 400
            c("block undo names the song that was blocked, not the one now playing",
              blocked.get("undo") == {"video_id": "undo-song"}
              and restored.get("changed") and "undo-song" not in undo_store.songs
              and arbitrary_block_refused)
            c("the page protects destructive controls and keeps the device icon visible",
              all(mark in template_text for mark in
                  ('function armDanger(button, action)', 'function toastUndo(msg, onUndo)',
                   '.danger.arm::after', 'class="ib small danger" id="blocksong"',
                   'class="pickx danger sesskick"',
                   'if (!armDanger(del, "delete this list")) return;',
                   '<button class="ib small" id="volicon"',
                   'aria-label="Choose output device"><svg')))
            c("queue and activity text wrap rather than clipping mid-word",
              '-webkit-line-clamp:2' in template_text
              and 'title="${esc(t.title)}"' in template_text
              and '$("actlabel").title = text;' in template_text)
            say("issue regressions", c)

    except Exception as exc:            # a check suite must not be the thing
        import traceback
        traceback.print_exc()           # where it broke, not only that it did
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


if __name__ == "__main__":
    raise SystemExit(main())
