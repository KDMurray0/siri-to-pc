"""LLM request parsing via Groq (free, OpenAI-compatible Llama).

Returns strict-JSON intent for the resolver. No key or any error -> None, and
the caller falls back to the local grammar parser.
"""

import json
import os
import urllib.error
import urllib.request

_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_cfg = {
    "key": "",
    "model": "llama-3.1-8b-instant",
    "enabled": False,
    "timeout": 6,
    "working": False,   # last test() result
}

_SYSTEM = (
    "You convert a spoken music request into STRICT JSON for a music player. "
    "Return ONLY a JSON object, no prose. Schema:\n"
    '{\n'
    '  "kind": "song" | "album" | "artist" | "genre" | "command",\n'
    '  "title": string,   // the song title, when kind=song\n'
    '  "artist": string,  // the performing artist/band, "" if unknown\n'
    '  "album": string,   // the album name, when kind=album\n'
    '  "genre": string,   // genre / mood / vibe, when kind=genre\n'
    '  "variant": boolean,// true ONLY if the user explicitly wants a remix, '
    'live, acoustic, sped-up or cover version\n'
    '  "shuffle": boolean // true if the user asked to shuffle\n'
    '}\n'
    "Rules:\n"
    "- kind=song for a specific track (default when a title is named).\n"
    "- kind=album only when the user says album/record/LP or names a known album.\n"
    "- kind=artist when they want a whole artist/band (\"play <band>\", "
    "\"songs by <band>\", or a bare well-known band name with no song).\n"
    "- kind=genre for a mood/vibe/genre/decade (\"chill\", \"80s\", \"lofi\", "
    "\"jazz\", \"workout\").\n"
    "- kind=command for playback control (pause/stop/skip/next/previous/resume, "
    "volume up/down/set, like this, more like this). Put the control word in "
    "\"title\".\n"
    "- Correct obvious dictation/spelling mistakes in names.\n"
    "- Prefer the ORIGINAL version: variant=false unless they clearly asked for "
    "a remix/live/acoustic/cover/sped-up.\n"
    "Examples:\n"
    'play sultans of swing -> {"kind":"song","title":"Sultans of Swing",'
    '"artist":"Dire Straits","album":"","genre":"","variant":false,'
    '"shuffle":false}\n'
    'put on some arctic monkeys -> {"kind":"artist","title":"","artist":'
    '"Arctic Monkeys","album":"","genre":"","variant":false,"shuffle":false}\n'
    'play the rumours album -> {"kind":"album","title":"","artist":'
    '"Fleetwood Mac","album":"Rumours","genre":"","variant":false,'
    '"shuffle":false}\n'
    'something chill -> {"kind":"genre","title":"","artist":"","album":"",'
    '"genre":"chill","variant":false,"shuffle":true}\n'
    'skip this -> {"kind":"command","title":"next","artist":"","album":"",'
    '"genre":"","variant":false,"shuffle":false}'
)


def configure(api_key="", model=None, enabled=None, timeout=None):
    """Set the Groq key/model. Enabled defaults to True whenever a key exists."""
    _cfg["key"] = (api_key or os.environ.get("GROQ_API_KEY") or "").strip()
    if model:
        _cfg["model"] = model
    if timeout:
        _cfg["timeout"] = int(timeout)
    _cfg["enabled"] = bool(enabled) if enabled is not None else bool(_cfg["key"])


def available():
    return bool(_cfg["enabled"] and _cfg["key"])


def _call_groq(text):
    body = json.dumps({
        "model": _cfg["model"],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
    }).encode()
    req = urllib.request.Request(
        _API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {_cfg['key']}",
                 "Content-Type": "application/json",
                 # Cloudflare fronts the API and 403s the default urllib UA.
                 "User-Agent": "MusicRequestServer/1.0"},
    )
    with urllib.request.urlopen(req, timeout=_cfg["timeout"]) as r:
        payload = json.loads(r.read().decode())
    return payload["choices"][0]["message"]["content"]


def test():
    """Minimal Groq call (1 token) to confirm the key + model actually work.

    Caches the result in _cfg["working"] so the UI can show a live tick.
    """
    if not available():
        _cfg["working"] = False
        return False
    try:
        body = json.dumps({"model": _cfg["model"], "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(
            _API_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {_cfg['key']}",
                     "Content-Type": "application/json",
                     "User-Agent": "MusicRequestServer/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    _cfg["working"] = ok
    return ok


def interpret(text):
    """Parse *text* into a resolver-ready plan dict, or None to fall back.

    Returns:
        {
          "kind": "song"|"album"|"artist"|"genre"|"command",
          "query": str,      # what to search (title/album/artist/genre)
          "artist": str,     # artist filter, "" if none
          "command": str,    # control word when kind=command
          "variant": bool,   # user wants a remix/live/etc.
          "shuffle": bool|None,
        }
    """
    if not available() or not text or not text.strip():
        return None
    try:
        raw = _call_groq(text.strip())
        data = json.loads(raw)
    except Exception:
        return None

    kind = (data.get("kind") or "").strip().lower()
    if kind not in ("song", "album", "artist", "genre", "command"):
        return None

    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()
    genre = (data.get("genre") or "").strip()

    if kind == "command":
        return {"kind": "command", "command": title.lower(),
                "query": title, "artist": "", "variant": False, "shuffle": None}

    if kind == "song":
        query = title or genre or album
    elif kind == "album":
        query = album or title
    elif kind == "artist":
        query = artist or title
    else:  # genre
        query = genre or title

    if not query:
        return None

    return {
        "kind": kind,
        "query": query,
        "artist": artist,
        "command": None,
        "variant": bool(data.get("variant")),
        "shuffle": data.get("shuffle") if isinstance(data.get("shuffle"), bool) else None,
    }
