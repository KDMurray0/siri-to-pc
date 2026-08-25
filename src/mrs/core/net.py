"""Where this server can be reached from, and whether Tailscale is one of them.

A phone on the same wifi wants the LAN address. A phone anywhere else wants
the Tailscale one, which is the only address that works off the network
without opening a port to the internet.

Tailscale is optional. `tailscale` in settings is auto | on | off:
  auto  use it if it's there, say nothing if it isn't
  on    expect it, and complain when it's missing
  off   don't even look — no subprocess, no delay
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

from ..config import config
from ..logging_setup import get

log = get("net")

CREATE_NO_WINDOW = 0x08000000

_USUAL = (
    Path(r"C:\Program Files\Tailscale\tailscale.exe"),
    Path(r"C:\Program Files (x86)\Tailscale IPN\tailscale.exe"),
)


def lan_ip() -> str:
    """The address of whichever interface would reach the internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packets sent, just routing
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _exe() -> Path | None:
    named = (config.get("tailscale_exe", "") or "").strip()
    if named:
        p = Path(named)
        return p if p.is_file() else None
    for p in _USUAL:
        if p.is_file():
            return p
    found = shutil.which("tailscale")
    return Path(found) if found else None


def _run(args: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def tailscale() -> dict:
    """{ok, ip, detail} — never raises, never blocks longer than a few seconds."""
    mode = str(config.get("tailscale", "auto")).lower()
    if mode in ("off", "false", "no"):
        return {"ok": False, "ip": "", "detail": "turned off in settings",
                "wanted": False}

    exe = _exe()
    if not exe:
        why = ("switched on in settings but not installed" if mode == "on"
               else "not installed")
        return {"ok": False, "ip": "", "detail": why, "wanted": mode == "on"}

    code, out = _run([str(exe), "ip", "-4"])
    ip = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
    if not ip.startswith("100."):
        _, status = _run([str(exe), "status"])
        why = "installed but not connected"
        if "Logged out" in status:
            why = "logged out — run: tailscale up"
        elif "stopped" in status.lower():
            why = "stopped — start the Tailscale service"
        return {"ok": False, "ip": "", "detail": why, "wanted": mode != "auto"}
    return {"ok": True, "ip": ip, "detail": ip, "wanted": True}


def qr_svg(text: str, size: int = 176) -> str:
    """The address as an inline SVG QR code.

    SVG rather than a PNG so it stays sharp on a phone screen and needs no
    image library — the modules go out as one path, and currentColor lets it
    follow whichever theme the page is in.
    """
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)

    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if not row[x]:
                x += 1
                continue
            run = x
            while run < n and row[run]:
                run += 1
            parts.append(f"M{x} {y}h{run - x}v1h-{run - x}z")
            x = run
    path = "".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
            f'height="{size}" viewBox="0 0 {n} {n}" '
            f'shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code for {text.split("?")[0]}">'
            f'<rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')


def addresses() -> dict:
    """Every URL that reaches the player, best one first."""
    port = int(config.get("port", 5000))
    key = config.get("api_key", "") or ""
    q = f"?key={key}" if key else ""
    ts = tailscale()

    rows = []
    if ts["ok"]:
        rows.append({"kind": "tailscale", "label": "Anywhere (Tailscale)",
                     "url": f"http://{ts['ip']}:{port}/player{q}"})
    rows.append({"kind": "lan", "label": "Same wifi",
                 "url": f"http://{lan_ip()}:{port}/player{q}"})
    rows.append({"kind": "local", "label": "This machine",
                 "url": f"http://127.0.0.1:{port}/player{q}"})
    return {"addresses": rows, "tailscale": ts, "port": port}
