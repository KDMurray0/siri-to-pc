"""Music Request, as an app.

The player a browser gets, in a window of its own that remembers who you are.
There is no music logic here and no server: it opens the address in server.txt
and stays out of the way. Signing in, the queue and the playlists all live on
the server, so this doesn't need updating when the server does.

server.txt is one address per line, and the first that answers wins -- the
public one first, then the one on the home network. A copy you saved yourself
from the "can't reach it" page lives in %LOCALAPPDATA%\\MusicClient and is
preferred to the one shipped beside the exe.
"""

from __future__ import annotations

import ctypes
import html
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TITLE = "Music Request"
STATE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MusicClient"
BESIDE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent

# Audio has to start when the page says so, not when somebody has clicked in
# the window first; a player that stays silent until you poke it is broken.
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--autoplay-policy=no-user-gesture-required")

WINDOW = None          # module-level: pywebview walks whatever the js api holds


def _say(text: str, title: str = TITLE) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)


def _clean(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    if "://" not in line:
        line = "https://" + line
    return line.rstrip("/")


def addresses() -> list[str]:
    for path in (STATE / "server.txt", BESIDE / "server.txt"):
        try:
            found = [a for a in map(_clean, path.read_text("utf-8-sig").splitlines()) if a]
        except OSError:
            continue
        if found:
            return found
    return []


def alive(base: str) -> bool:
    """Does something answer as a Music Request server at this address?"""
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=4) as reply:
            return json.loads(reply.read(2048)).get("app") == "music-request-server"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def find() -> str:
    for base in addresses():
        if alive(base):
            return base + "/"
    return ""


def _page(body: str) -> str:
    return f"""<!doctype html><meta charset=utf-8><title>{TITLE}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0e13;color:#f4f1ec;
  font:15px/1.55 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif}}
main{{width:min(440px,90vw)}}
h1{{font:700 26px/1.15 "Segoe UI Variable Display","Segoe UI",sans-serif;letter-spacing:-.02em;margin:0 0 8px}}
p{{color:#a29d95;margin:0 0 18px}}
input{{width:100%;height:44px;padding:0 14px;border-radius:10px;border:1px solid #26242c;background:#17151c;
  color:#f4f1ec;font:inherit;outline:none;margin-bottom:12px}}
input:focus{{border-color:#e8a262}}
button{{height:44px;padding:0 20px;border-radius:10px;border:0;background:#e8a262;color:#1a1107;
  font:600 14px "Segoe UI",sans-serif;cursor:pointer;margin-right:8px}}
button.q{{background:transparent;color:#f4f1ec;border:1px solid #26242c}}
#msg{{margin-top:14px;color:#ff9c8f;min-height:1.5em}}
small{{display:block;color:#6d6963;margin-top:22px}}
</style><main>{body}</main>"""


def connecting_page() -> str:
    return _page("<h1>Connecting&hellip;</h1><p>Finding the server.</p>")


def offline_page(tried: list[str]) -> str:
    seen = "".join(f"<li>{html.escape(a)}</li>" for a in tried) or "<li>nowhere yet</li>"
    return _page(f"""
<h1>Can&rsquo;t reach the server</h1>
<p>Check you&rsquo;re online, or that it&rsquo;s switched on. Tried:</p>
<ul style="color:#a29d95;margin:0 0 20px;padding-left:1.1em">{seen}</ul>
<button onclick="go()">Try again</button>
<button class=q onclick="document.getElementById('a').hidden=false;this.hidden=true">Use another address</button>
<div id=a hidden style="margin-top:18px"><input id=addr placeholder="https://your-server.example/music"
  spellcheck=false autocomplete=off><button onclick="save()">Save and connect</button></div>
<div id=msg></div>
<script>
async function go(){{document.getElementById('msg').textContent='';
  const r=await pywebview.api.retry(); if(r) document.getElementById('msg').textContent=r;}}
async function save(){{const r=await pywebview.api.set_address(document.getElementById('addr').value);
  if(r) document.getElementById('msg').textContent=r;}}
</script>""")


class Bridge:
    """What the offline page can ask for. Nothing here holds the window."""

    def retry(self) -> str:
        url = find()
        if not url:
            return "Still can't reach it."
        WINDOW.load_url(url)
        return ""

    def set_address(self, text: str) -> str:
        base = _clean(str(text or ""))
        if not base:
            return "That doesn't look like an address."
        if not alive(base):
            return "Nothing answers there as a Music Request server."
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "server.txt").write_text(base + "\n", encoding="utf-8")
        WINDOW.load_url(base + "/")
        return ""


def main() -> int:
    global WINDOW
    try:
        import webview
    except ImportError as exc:
        _say(f"Music Request can't start: {exc}")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    WINDOW = webview.create_window(
        TITLE, html=connecting_page(), js_api=Bridge(), width=1120, height=780,
        min_size=(380, 520), background_color="#0f0e13")

    def boot() -> None:
        # The window is up already; looking for the server can take a few
        # seconds when one of the addresses is dead, and that shouldn't be
        # spent staring at nothing.
        url = find()
        if url:
            WINDOW.load_url(url)
        else:
            WINDOW.load_html(offline_page(addresses()))

    try:
        # Its own storage, kept between runs: this is what remembers the
        # sign-in, so nobody has to do it again every time it opens.
        webview.start(boot, private_mode=False, storage_path=str(STATE / "webview"))
    except Exception as exc:
        _say("Music Request needs Microsoft Edge WebView2, which is part of "
             "Windows 11 and a free download for Windows 10.\n\n" + str(exc)[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
