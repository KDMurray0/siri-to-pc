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
