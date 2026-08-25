"""Last.fm, alarms and casting to other machines."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from ..config import config
from .gate import gate
from ..events import Ev, bus
from ..logging_setup import get
from ..models import Track

log = get("extras")

UA = {"User-Agent": "MusicRequestServer/2.0"}


# ── Last.fm ───────────────────────────────────────────────────────────

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


class Scrobbler:
    """Last.fm now-playing + scrobble. Silent no-op until it's set up."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        return bool(config.get("lastfm_api_key") and config.get("lastfm_session"))

    def _sign(self, params: dict) -> str:
        secret = config.get("lastfm_secret") or ""
        raw = "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _call(self, method: str, extra: dict, *, write: bool = True):
        params = {"method": method, "api_key": config.get("lastfm_api_key"),
                  "sk": config.get("lastfm_session"), **extra}
        params["api_sig"] = self._sign(params)
        params["format"] = "json"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(LASTFM_API, data=data, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode())
        except Exception as exc:
            log.debug("last.fm %s failed: %s", method, exc)
            return None

    def maybe_seed_taste(self) -> int:
        """Give taste a head start from the account, the first time only.

        Taste starts empty and takes weeks to learn anything. If somebody has
        already told us their Last.fm, the answer is sitting there. Once, and
        never again — a second run would keep re-adding artists the user has
        since skipped away.
        """
        if not self.enabled() or config.get("lastfm_seeded"):
            return 0
        names = self.top_artists()
        if not names:
            return 0            # not marked done: try again next start
        from .taste import taste
        added = taste.seed_from(names)
        config.set("lastfm_seeded", True)
        taste.save()
        log.info("seeded taste with %d artists from last.fm", added)
        if added:
            bus.publish(Ev.TOAST, f"Picked up {added} artists from Last.fm")
        return added

    def top_artists(self, limit: int = 50) -> list[str]:
        """Who the user actually listens to, from their own account."""
        who = config.get("lastfm_user") or ""
        if not self.enabled() or not who:
            return []
        got = self._read("user.getTopArtists", {"user": who, "period": "12month",
                                                "limit": str(limit)})
        rows = ((got or {}).get("topartists") or {}).get("artist") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [r["name"] for r in rows if r.get("name")]

    def _read(self, method: str, extra: dict):
        """An unsigned read. Scrobbling is a write and needs the session
        signature; asking what somebody listens to doesn't."""
        params = {"method": method, "api_key": config.get("lastfm_api_key"),
                  "format": "json", **extra}
        url = LASTFM_API + "?" + urllib.parse.urlencode(params)
        gate.wait("lastfm")
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=10) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            log.debug("last.fm %s failed: %s", method, exc)
            return None

    def now_playing(self, track: Track) -> None:
        if not self.enabled() or not track.artist:
            return
        threading.Thread(target=self._call, args=("track.updateNowPlaying", {
            "artist": track.artist.split(",")[0], "track": track.title,
            "album": track.album or ""}), daemon=True).start()

    def scrobble(self, track: Track, played_seconds: float) -> None:
        """Last.fm wants >30s listened and half the track (or 4 minutes)."""
        if not self.enabled() or not track.artist or played_seconds < 30:
            return
        if track.duration and played_seconds < min(track.duration / 2, 240):
            return
        threading.Thread(target=self._call, args=("track.scrobble", {
            "artist": track.artist.split(",")[0], "track": track.title,
            "album": track.album or "",
            "timestamp": str(int(time.time() - played_seconds))}), daemon=True).start()
        log.info("scrobbled %s — %s", track.artist, track.title)

    # -- setup --
    # Last.fm's flow is: fetch a request token, send the user to approve it,
    # then trade that same token for a session key. The approve URL is useless
    # without the token in it.
    def begin_auth(self) -> dict:
        key = config.get("lastfm_api_key")
        if not key:
            return {"ok": False, "message": "Add your Last.fm API key first"}
        url = LASTFM_API + "?" + urllib.parse.urlencode(
            {"method": "auth.gettoken", "api_key": key, "format": "json"})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=10) as r:
                token = json.loads(r.read().decode()).get("token")
        except Exception as exc:
            return {"ok": False, "message": f"Last.fm didn't answer: {exc}"}
        if not token:
            return {"ok": False, "message": "Last.fm didn't give a token"}
        config.set("lastfm_token", token)
        return {"ok": True, "token": token,
                "auth_url": f"https://www.last.fm/api/auth/?api_key={key}&token={token}",
                "message": "Approve it in the browser, then press Finish"}

    def complete_auth(self, token: str = "") -> dict:
        token = token or config.get("lastfm_token") or ""
        if not token:
            return {"ok": False, "message": "Press Approve first"}
        params = {"method": "auth.getSession",
                  "api_key": config.get("lastfm_api_key"), "token": token}
        params["api_sig"] = self._sign(params)
        url = LASTFM_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=10) as r:
                data = json.loads(r.read().decode())
        except Exception as exc:
            return {"ok": False,
                    "message": "Not approved yet — approve it in the browser first"
                               if "403" in str(exc) else f"Last.fm said: {exc}"}
        session = (data.get("session") or {})
        sk, name = session.get("key"), session.get("name")
        if not sk:
            return {"ok": False, "message": data.get("message") or "No session key"}
        config.update({"lastfm_session": sk, "lastfm_user": name or "", "lastfm_token": ""})
        log.info("last.fm connected as %s", name)
        return {"ok": True, "user": name, "message": f"Connected as {name}"}

    def status(self) -> dict:
        return {"connected": self.enabled(), "user": config.get("lastfm_user", ""),
                "has_key": bool(config.get("lastfm_api_key"))}


scrobbler = Scrobbler()


# ── Alarms ────────────────────────────────────────────────────────────

class AlarmClock:
    """Wake up to music. Alarms are {time: "07:30", days: [0-6], query: str}."""

    def __init__(self, on_fire) -> None:
        self.on_fire = on_fire
        self._fired: set[str] = set()
        threading.Thread(target=self._loop, daemon=True, name="alarms").start()

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.debug("alarm tick: %s", exc)
            time.sleep(20)

    def _tick(self) -> None:
        alarms = config.get("alarms") or []
        if not alarms:
            return
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for alarm in alarms:
            if not alarm.get("enabled", True):
                continue
            if alarm.get("time") != now.strftime("%H:%M"):
                continue
            days = alarm.get("days")
            if days and now.weekday() not in days:
                continue
            key = f"{stamp}|{alarm.get('time')}|{alarm.get('query')}"
            if key in self._fired:
                continue
            self._fired.add(key)
            if len(self._fired) > 50:
                self._fired = set(list(self._fired)[-20:])
            log.info("alarm firing: %s", alarm.get("query"))
            bus.publish(Ev.TOAST, f"Alarm: {alarm.get('query')}")
            try:
                self.on_fire(alarm)
            except Exception as exc:
                log.warning("alarm failed: %s", exc)


# ── Casting to other machines ─────────────────────────────────────────

class Caster:
    """Play the same request on other machines running this app.

    Not sample-synced audio streaming — each peer resolves and plays the same
    request itself, which is what you want for "the same song in the kitchen".
    Peers are `host:port` strings; each needs its own API key configured.
    """

    def __init__(self) -> None:
        pass

    def peers(self) -> list[dict]:
        out = []
        for entry in config.get("cast_peers") or []:
            if isinstance(entry, str):
                out.append({"host": entry, "key": ""})
            elif isinstance(entry, dict) and entry.get("host"):
                out.append(entry)
        return out

    def broadcast(self, text: str) -> list[str]:
        """Send a request to every peer. Returns the ones that accepted."""
        good = []
        for peer in self.peers():
            host = peer["host"]
            url = f"http://{host}/api/play?q={urllib.parse.quote(text)}"
            if peer.get("key"):
                url += f"&key={urllib.parse.quote(peer['key'])}"
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=6) as r:
                    if r.status == 200:
                        good.append(host)
            except Exception as exc:
                log.debug("cast to %s failed: %s", host, exc)
        if good:
            log.info("cast to %s", ", ".join(good))
        return good

    def control(self, action: str) -> list[str]:
        good = []
        for peer in self.peers():
            url = f"http://{peer['host']}/api/control/{action}"
            if peer.get("key"):
                url += f"?key={urllib.parse.quote(peer['key'])}"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                            timeout=5) as r:
                    if r.status == 200:
                        good.append(peer["host"])
            except Exception:
                pass
        return good


caster = Caster()
