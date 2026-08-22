"""Groq request parsing.

Gotchas: Cloudflare 403s the default urllib UA; Groq retires models so a
pinned one can look like a bad key; free tier is ~8k tokens/min; gpt-oss
returns "false" as a string.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Plan

log = get("groq")

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"

SYSTEM = (
    "You convert a spoken music request into STRICT JSON for a music player. "
    "Return ONLY a JSON object, no prose. Schema:\n"
    '{"kind":"song|album|artist|genre|command",'
    '"title":string,"artist":string,"album":string,"genre":string,'
    '"variant":boolean,"shuffle":boolean}\n'
    "Rules:\n"
    "- kind=song for a specific track (default when a title is named). Keep the "
    "FULL title; never shorten it.\n"
    "- kind=album only when they say album/record/LP or name a known album.\n"
    "- kind=artist for a whole artist/band.\n"
    "- kind=genre for a mood/vibe/genre/decade.\n"
    "- kind=command for playback control; put the control word in title.\n"
    "- Fill in the artist when you know the song, even if unspoken.\n"
    "- Correct obvious dictation errors.\n"
    "- variant=true only if they explicitly asked for a remix/live/acoustic/"
    "cover/sped-up version.\n"
    "Examples:\n"
    'play sultans of swing -> {"kind":"song","title":"Sultans of Swing",'
    '"artist":"Dire Straits","album":"","genre":"","variant":false,"shuffle":false}\n'
    'i want to break free -> {"kind":"song","title":"I Want to Break Free",'
    '"artist":"Queen","album":"","genre":"","variant":false,"shuffle":false}\n'
    'put on some arctic monkeys -> {"kind":"artist","title":"","artist":'
    '"Arctic Monkeys","album":"","genre":"","variant":false,"shuffle":false}\n'
    'something chill -> {"kind":"genre","title":"","artist":"","album":"",'
    '"genre":"chill","variant":false,"shuffle":true}'
)

_state = {"working": False, "model": "", "last_error": ""}


def status() -> dict:
    return dict(_state)


def available() -> bool:
    return bool(config.get("use_groq", True) and config.get("groq_api_key"))


def _model() -> str:
    return (config.get("groq_model") or DEFAULT_MODEL).strip()


def _post(body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {config.get('groq_api_key')}",
                 "Content-Type": "application/json",
                 # Cloudflare blocks the default urllib UA outright.
                 "User-Agent": "MusicRequestServer/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def test(model: str | None = None) -> bool:
    """Cheap call to see whether the key+model actually work."""
    if not available():
        _state.update(working=False, last_error="no key")
        return False
    use = model or _model()
    try:
        _post({"model": use, "max_tokens": 1,
               "messages": [{"role": "user", "content": "hi"}]}, 8)
        _state.update(working=True, model=use, last_error="")
        return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:160]
        except Exception:
            pass
        _state.update(working=False, last_error=f"HTTP {e.code} {detail}")
        log.warning("self-test failed on %s: HTTP %s %s", use, e.code, detail)
    except Exception as exc:
        _state.update(working=False, last_error=str(exc))
        log.warning("self-test failed on %s: %s", use, exc)
    return False


def ensure_model() -> bool:
    """Self-heal a config pinned to a model Groq has since retired."""
    if not available():
        return False
    if test():
        return True
    current = _model()
    if current != DEFAULT_MODEL:
        log.warning("model %s not usable — falling back to %s", current, DEFAULT_MODEL)
        if test(DEFAULT_MODEL):
            config.set("groq_model", DEFAULT_MODEL)
            log.info("switched to %s", DEFAULT_MODEL)
            return True
    return False


def _as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def parse(text: str) -> Plan | None:
    """Return a Plan, or None so the caller falls back to the grammar."""
    if not available() or not text.strip():
        return None
    try:
        payload = _post({
            "model": _model(), "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": text.strip()}],
        }, float(config.get("groq_timeout", 4)))
        data = json.loads(payload["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:160]
        except Exception:
            pass
        # 429 = rate limited, 403 = model blocked, 401 = bad key. Say which.
        log.warning("HTTP %s — using local parser instead. %s", e.code, detail)
        _state.update(working=False, last_error=f"HTTP {e.code}")
        return None
    except Exception as exc:
        log.warning("%s — using local parser instead", exc)
        return None

    kind = (data.get("kind") or "").strip().lower()
    if kind not in ("song", "album", "artist", "genre", "command"):
        return None
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()
    genre = (data.get("genre") or "").strip()
    _state["working"] = True

    if kind == "command":
        return Plan(kind="command", command=title.lower(), via="llm", spoken=text)

    query = {"song": title or album or genre,
             "album": album or title,
             "artist": artist or title,
             "genre": genre or title}.get(kind, "")
    if not query:
        return None
    return Plan(kind=kind, query=query, artist=artist,
                variant=_as_bool(data.get("variant")) is True,
                shuffle=_as_bool(data.get("shuffle")),
                via="llm", spoken=text)
