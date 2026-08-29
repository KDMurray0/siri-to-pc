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

    owner_h = {"X-Music-Key": key}
    minted: list[str] = []

    def link(name: str, scope: str = "full", hours: int = 1) -> str:
        got = issue(key, name=name, hours=hours, scope=scope)
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
                h = dict(owner_h if tok is None else {"X-Music-Key": tok})
                h.update(extra or {})
                return client.get(path, headers=h)

            # -- 1. the owner's listening is the owner's ------------------
            c = _Checker("history")
            c("owner sees their own recents",
              get("/api/history").json().get("mine") is True)
            for who, tok in (("a full link", full), ("a phone link", phone)):
                c(f"{who} gets no recents",
                  not get("/api/history", tok, here).json().get("history"))
                c(f"{who} gets no liked songs",
                  not get("/api/liked", tok, here).json().get("liked"))
            say("recents and liked", c)

            # -- 2. settings are not a shared thing ------------------------
            c = _Checker("settings")
            cfg = get("/api/settings", full, here).json()
            leaked = [k for k in ("api_key", "library_paths", "allowed_ips",
                                  "ddns_hostname", "ddns_user", "volume",
                                  "crossfade", "repeat", "shuffle",
                                  "cookies_from_browser", "queue_minutes")
                      if k in cfg]
            c("a link gets none of the owner's preferences", not leaked,
              ", ".join(leaked))
            c("...but enough to draw itself",
              cfg.get("guest") is True and "theme" in cfg)
            say("settings", c)

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
              key not in page.text)
            bad_key = client.get("/?key=nonsense")
            c("a wrong key can't either", bad_key.status_code == 403,
              f"HTTP {bad_key.status_code}")
            c("...and leaks nothing", key not in bad_key.text)
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
              key not in p.text and full in p.text)
            c("...and is told it's a guest", 'const GUEST = "1"' in p.text)
            mine = get("/player")
            c("the owner's page carries the key", key in mine.text)
            c("...and is told so", 'const GUEST = "0"' in mine.text)
            say("player page", c)

            # -- 7. revoking a link stops it dead --------------------------
            c = _Checker("revocation")
            doomed = link("check-doomed", "full")
            c("it works before", get("/api/status", doomed, here).status_code == 200)
            from .web.security import revoke
            revoke(minted[-1])
            code = get("/api/status", doomed, here).status_code
            c("and not after", code == 403, f"HTTP {code}")
            say("revocation", c)

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
