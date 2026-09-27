"""Keeping the certificate good, without anybody remembering to.

Every few hours: is the certificate being served one for the hostname we hand
out, and does it have more than a month left? If not, ask for one -- the same
name renewed, or a first one for a new name -- through the Posh-ACME scripts
that ship beside the exe. The server already picks up new files on its own.

Two things shape how hard it tries. The Dynu credential lives in the Windows
user's Posh-ACME store, encrypted to that user, which a copy started before
sign-in can't open -- so only a copy with a desktop does this. And a shared
Dynu domain has a 50-a-week Let's Encrypt limit spent by
thousands of strangers: renewing the same name is exempt, a new name is not.
So it asks at most once per BACKOFF, honours any "retry after" Let's Encrypt
gives, and never re-issues a name that is already covered and in date.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import config
from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("certs")

CHECK_EVERY = 6 * 3600
RENEW_BELOW_DAYS = 30
BACKOFF = 3 * 3600            # a failure that didn't say when to come back
_lock = threading.Lock()
_running = threading.Event()
_started = False


def _state_path() -> Path:
    return data_dir() / "cert-keeper.json"


def _state() -> dict:
    try:
        got = json.loads(_state_path().read_text("utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(**changes) -> dict:
    with _lock:
        now = _state()
        now.update(changes)
        try:
            write_atomic(_state_path(), json.dumps(now))
        except OSError as exc:
            log.debug("couldn't save the certificate state: %s", exc)
        return now


def live() -> dict:
    """What is being served: names, expiry, days left. {} if nothing."""
    from ..server import tls_files
    import ssl
    pair = tls_files()
    if not pair:
        return {}
    try:
        got = ssl._ssl._test_decode_cert(pair[0])
    except Exception:
        return {}
    names = [v.lower() for k, v in got.get("subjectAltName", ()) if k == "DNS"]
    ends = ssl.cert_time_to_seconds(got["notAfter"])
    return {"names": names, "expires": int(ends),
            "days_left": round(max(0.0, (ends - time.time()) / 86400), 1),
            "issuer": dict(x[0] for x in got.get("issuer", ())).get("organizationName", "")}


def hostname() -> str:
    return str(config.get("ddns_hostname") or "").strip().lower().rstrip(".")


def covers(names: list[str], host: str) -> bool:
    for n in names:
        if n == host or (n.startswith("*.") and host.split(".", 1)[-1] == n[2:]):
            return True
    return False


def need() -> str:
    """"renew", "issue" or "" -- what, if anything, the certificate needs."""
    host = hostname()
    if not host or config.get("https_mode") == "proxy":
        return ""
    cur = live()
    if not cur or not covers(cur["names"], host):
        return "issue"
    return "renew" if cur["days_left"] < RENEW_BELOW_DAYS else ""


def _script(name: str) -> str:
    from ..paths import resource_dir
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    for root in ([here] if here else []) + [Path(resource_dir()),
                                            Path(__file__).resolve().parents[3]]:
        got = root / name
        if got.is_file():
            return str(got)
    return ""


_RETRY = re.compile(r"retry after (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.I)


def _retry_after(text: str) -> float:
    """Let's Encrypt's own "retry after" time, as a timestamp, or 0."""
    m = _RETRY.search(text or "")
    if not m:
        return 0.0
    try:
        return datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _log_tail(since: float) -> str:
    """What the scripts wrote to their log after `since`."""
    path = data_dir() / "tls-provisioning.log"
    try:
        lines = path.read_text("utf-8-sig", errors="replace").splitlines()
    except OSError:
        return ""
    out = []
    for line in lines[-40:]:
        try:
            at = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if at >= since - 2:
            out.append(line[21:].strip())
    return " ".join(out)


def status() -> dict:
    st = _state()
    return {"live": live(), "hostname": hostname(), "need": need(),
            "last_try": st.get("last_try", 0), "last_result": st.get("last_result", ""),
            "next_try": st.get("next_try", 0), "busy": _running.is_set(),
            "can_run": can_run()}


def can_run() -> bool:
    """Only a copy with a desktop can open the stored Dynu credential."""
    return ("--headless" not in sys.argv and str(config.get("ddns_provider") or "dynu") == "dynu"
            and bool(_script("certificate.ps1")))


def attempt(force: bool = False) -> dict:
    """Do whatever is needed now, once. Returns the new status."""
    what = need()
    if not what:
        _save(last_result="Nothing needed: the certificate covers %s and is in date." % hostname())
        return status()
    st = _state()
    if not force and time.time() < float(st.get("next_try", 0)):
        return status()
    if float(st.get("retry_after", 0)) + 60 > time.time():
        # Let's Encrypt said when; asking before that only spends goodwill.
        return status()
    if not can_run() or _running.is_set():
        return status()
    _running.set()
    started = time.time()
    try:
        host = hostname()
        if what == "renew":
            args = [_script("certificate.ps1"), "-Renew", "-Domain", host]
        else:
            script = _script("Request-MusicCertificate.ps1")
            if not script:
                return status()
            args = [script, "-Domain", host, "-SkipSelfCleanup"]
        log.info("certificate: %s for %s", what, host)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args],
                capture_output=True, text=True, timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok, said = proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, said = False, str(exc)
        told = _log_tail(started) or said.strip()[-300:]
        retry = _retry_after(told)
        ok = ok and not need()
        # Told when: come back then, to the minute. Not told: a few hours.
        later = (retry + 60) if retry > time.time() else time.time() + BACKOFF
        _save(last_try=int(started), last_result=("ok: " if ok else "failed: ") + told[:300],
              next_try=int(time.time() + CHECK_EVERY if ok else later),
              retry_after=int(retry))
        (log.info if ok else log.warning)("certificate %s: %s", what, told[:200])
        return status()
    finally:
        _running.clear()


def _wait() -> float:
    """Seconds until the next look: the time it was told, if sooner."""
    if not need():
        return CHECK_EVERY
    st = _state()
    due = max(float(st.get("next_try", 0)), float(st.get("retry_after", 0)) + 60)
    return max(60.0, min(CHECK_EVERY, due - time.time()))


def _loop() -> None:
    time.sleep(120)                     # let startup settle first
    while True:
        try:
            if need():
                attempt()
        except Exception as exc:
            log.debug("certificate keeper: %s", exc)
        time.sleep(_wait())


def start() -> None:
    global _started
    if _started or not can_run():
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="certkeeper").start()
