"""Turning the lighting off, without a SignalRGB Pro licence.

SignalRGB gates every route behind Pro. Measured, not assumed:

    GET /api/v1/lighting  ->  403 "You must be a SignalRGB Pro user"

every other path under /api/v1 is a flat 404, and the free signalrgb:// URI
channel is a no-op whose launcher exits 0 whatever you hand it — including
verbs that don't exist, which is how it managed to look like it was working.

OpenRGB is the way round it: open source, no licence, and it talks to the
hardware itself rather than asking someone else's daemon nicely. Its command
line drives the already-running instance, so there's no binary SDK protocol
to hand-roll:

    OpenRGB.exe --color 000000 --mode static     everything off
    OpenRGB.exe --profile <name>                 put it back

Profiles are what make the restore honest. Rather than guessing what the
lighting looked like — which is where the SignalRGB attempt came unstuck,
reading a Qt QVariant blob out of the registry and posting it back as a
colour — the setup is saved once, by name, and loaded again afterwards.

The catch, and it's a real one: OpenRGB and SignalRGB both want exclusive
control of the same devices. Run one or the other, not both.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
import shutil

from ..config import config
from ..logging_setup import get

log = get("lighting")

CREATE_NO_WINDOW = 0x08000000
SDK_PORT = 6742
# What we save the pre-blackout state as, so restore has something real.
RESTORE_PROFILE = "mrs-before-dark"

_USUAL = (
    Path(r"C:\Program Files\OpenRGB\OpenRGB.exe"),
    Path(r"C:\Program Files (x86)\OpenRGB\OpenRGB.exe"),
    Path.home() / "AppData/Local/OpenRGB/OpenRGB.exe",
)


def exe() -> Path | None:
    named = (config.get("openrgb_exe", "") or "").strip()
    if named:
        p = Path(named)
        return p if p.is_file() else None
    for p in _USUAL:
        if p.is_file():
            return p
    found = shutil.which("OpenRGB")
    return Path(found) if found else None


def _run(args: list[str], timeout: int = 20) -> tuple[int, str]:
    binary = exe()
    if not binary:
        return 1, "OpenRGB isn't installed"
    try:
        p = subprocess.run([str(binary), *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "OpenRGB didn't answer"
    except Exception as exc:
        return 1, str(exc)


def server_up() -> bool:
    """The SDK server, which the CLI needs to reach a running instance."""
    try:
        with socket.create_connection(("127.0.0.1", SDK_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def devices() -> list[str]:
    code, out = _run(["--list-devices"], timeout=25)
    if code:
        return []
    # One device per line that isn't indented detail.
    return [ln.strip() for ln in out.splitlines()
            if ln.strip() and not ln.startswith((" ", "\t"))]


def available() -> bool:
    return bool(exe()) and server_up()


# ── the two things we actually want ──────────────────────────────────────
def dark() -> str:
    """Everything OpenRGB can see, off — saving the current look first."""
    if not exe():
        return "OpenRGB isn't installed"
    if not server_up():
        return "OpenRGB isn't running"
    # Save before changing anything, every time: the lighting may have been
    # changed by hand since the last blackout.
    code, out = _run(["--save-profile", RESTORE_PROFILE])
    if code:
        log.warning("couldn't save the lighting profile: %s", out.strip()[:120])
    code, out = _run(["--color", "000000", "--mode", "static"])
    if code:
        return f"failed: {out.strip()[:120]}"
    return "off"


def restore() -> str:
    if not exe():
        return "OpenRGB isn't installed"
    if not server_up():
        return "OpenRGB isn't running"
    wanted = config.get("lighting_profile", "") or RESTORE_PROFILE
    code, out = _run(["--profile", wanted])
    if code:
        return f"couldn't load {wanted}: {out.strip()[:120]}"
    return f"{wanted} restored"


def describe() -> dict:
    binary = exe()
    up = server_up()
    return {
        "installed": bool(binary),
        "path": str(binary) if binary else "",
        "server": up,
        "can_control": bool(binary) and up,
        "devices": devices() if (binary and up) else [],
        "profile": config.get("lighting_profile", "") or RESTORE_PROFILE,
    }


if __name__ == "__main__":
    import json
    import sys
    import time

    state = describe()
    print(json.dumps(state, indent=2))
    if not state["can_control"]:
        print("\nNothing to drive. Install OpenRGB and leave it running")
        print("(its SDK server listens on 6742), and close SignalRGB —")
        print("the two fight over the same devices.")
        sys.exit(1)
    if "--blink" in sys.argv:
        print("\ngoing dark for 6 seconds — watch the mouse and the case")
        print("  ", dark())
        time.sleep(6)
        print("  ", restore())
