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
from .conjunction import split_seeds

log = get("groq")

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"

# Every command the app can carry out, and no others. requests._COMMANDS and
# the handful handled before it are the truth; a check fails if this list and
# that one ever disagree, because a word the prompt doesn't offer is a word the
# model will invent ("skip", "louder") and the app answers with "I don't know
# how to skip".
COMMANDS = ("pause", "resume", "next", "previous", "shuffle", "repeat", "mute",
            "unmute", "like", "volume", "volume_delta", "more_like_this",
            "save", "add_to_playlist")

# What a model says when it's being helpful rather than exact. Applied after
# the reply, so a near miss still does the right thing instead of erroring.
ALIASES = {
    "skip": "next", "forward": "next", "stop": "pause", "hold": "pause",
    "play": "resume", "continue": "resume", "unpause": "resume",
    "back": "previous", "prev": "previous", "silence": "mute",
    "louder": "volume_delta", "quieter": "volume_delta", "softer": "volume_delta",
    "love": "like", "heart": "like", "download": "save", "keep": "save",
    "similar": "more_like_this", "more_like": "more_like_this",
}

_BLANK = {"kind": "song", "title": "", "artist": "", "album": "", "genre": "",
          "argument": "", "variant": False, "shuffle": False}


def _shot(said: str, **fields) -> str:
    """One worked example, built from data so it can never be malformed."""
    # Only what matters: a blank or false field is left out, which is what the
    # prompt tells the model it may do. Spelling out the whole object nine
    # times was more than half the prompt, and every token of it counts against
    # a free-tier minute that several people now share.
    row = {"kind": fields.get("kind", "song")}
    row.update({k: v for k, v in fields.items() if k != "kind" and v != _BLANK.get(k)})
    return f"{said} -> " + json.dumps(row, separators=(",", ":"), ensure_ascii=False)


# Each of these is also fed back through parse() by the checks, so the prompt
# can't teach the model something the parser then refuses.
EXAMPLES = [
    ("sultans of swing", dict(kind="song", title="Sultans of Swing", artist="Dire Straits")),
    ("nirvana and foo fighters", dict(kind="artist", artist="Nirvana and Foo Fighters")),
    ("something chill", dict(kind="genre", genre="chill", shuffle=True)),
    ("shuffle my taylor swift", dict(kind="artist", artist="Taylor Swift", shuffle=True)),
    ("the album rumours", dict(kind="album", title="Rumours", album="Rumours", artist="Fleetwood Mac")),
    ("set it to forty", dict(kind="command", title="volume", argument="40")),
    ("crank it up", dict(kind="command", title="volume_delta", argument="10")),
    ("skip this one", dict(kind="command", title="next")),
    ("add this to my road trip playlist", dict(kind="command", title="add_to_playlist", argument="road trip")),
    ("what's the weather like", dict(kind="none")),
]

SYSTEM = (
    "You turn ONE spoken request for a music player into ONE JSON object. "
    "Reply with the JSON only: no prose, no markdown.\n\n"
    "The fields (leave out any that would be \"\" or false):\n"
    + json.dumps(_BLANK | {"kind": "song|album|artist|genre|command|none"},
                 separators=(",", ":")) + "\n\n"
    "Choose kind by the FIRST rule that fits:\n"
    "1. command: they are controlling playback, not asking for music. title "
    "MUST be exactly one of: " + ", ".join(COMMANDS) + ". Map what they said "
    "to the nearest: stop/hold on/pause that = pause; keep going/carry on = "
    "resume; skip/next one = next; go back/last song = previous; silence = "
    "mute; I love this = like; more like this = more_like_this; keep or "
    "download this = save. argument: for volume a number 0-150 (\"forty\" -> "
    "\"40\"); for volume_delta \"10\" for louder/turn it up/crank it, \"-10\" "
    "for quieter/turn it down, or the amount they said; for add_to_playlist "
    "the playlist's name; otherwise \"\". If they NAME music (a song, album, "
    "artist or genre) it is never a command, even with a word like shuffle, "
    "play or skip in it: \"shuffle my Taylor Swift\" is the artist Taylor Swift "
    "with shuffle true. shuffle is the command only when no music is named.\n"
    "2. none: nothing to do with music or playback (weather, maths, chat).\n"
    "3. song: a specific track; the default when a title is named. Keep the "
    "FULL title and never shorten it (\"i want to break free\" is \"I Want to "
    "Break Free\", not \"Break Free\"). artist is who recorded it: use what they "
    "said; if they didn't, fill in the artist of the well-known recording. "
    "Leave \"\" only if you genuinely don't know the song.\n"
    "4. album: they say album/record/LP, or name an album rather than a song.\n"
    "5. artist: a performer or band (\"some X\", \"songs by X\", \"play X\"). "
    "For several, join the names with \" and \" in artist. title stays \"\".\n"
    "6. genre: a genre, mood, decade or activity (chill, 90s, gym). Write "
    "decades as digits (\"90s\", never \"nineties\"). For several, join with "
    "\" and \". title stays \"\".\n\n"
    "Also:\n"
    "- Fix obvious dictation errors in names (\"dont stop me now\" -> \"Don't "
    "Stop Me Now\") but never swap in a different song.\n"
    "- Word order varies: \"coming undone korn\", \"korn coming undone\" and "
    "\"coming undone by korn\" all mean title \"Coming Undone\", artist \"Korn\".\n"
    "- variant is true ONLY if they explicitly ask for a remix, live, acoustic, "
    "cover, sped-up, slowed or instrumental version.\n"
    "- shuffle is true ONLY if they say shuffle, random, mix or surprise me, "
    "or they ask for a genre or mood. Otherwise false.\n\n"
    "Examples:\n" + "\n".join(_shot(said, **f) for said, f in EXAMPLES)
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


def ask_json(system: str, user: str, timeout: float = 8.0) -> dict | None:
    """One JSON answer, or None. For callers that aren't the request parser."""
    if not available() or not user.strip():
        return None
    try:
        payload = _post({
            "model": _model(), "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user.strip()}],
        }, timeout)
        got = json.loads(payload["choices"][0]["message"]["content"])
        _state["working"] = True
        return got if isinstance(got, dict) else None
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s asking the model", e.code)
        _state.update(working=False, last_error=f"HTTP {e.code}")
    except Exception as exc:
        log.warning("couldn't ask the model: %s", exc)
    return None


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


MODELS_URL = "https://api.groq.com/openai/v1/models"
_models: tuple[float, list[str]] = (0.0, [])


def cached_models() -> list[str]:
    """Return the last model list without contacting Groq."""
    return list(_models[1])


def models(force: bool = False) -> list[str]:
    """What Groq will actually serve us right now, best guess first.

    Asked rather than hard-coded, because Groq retires models and a list
    written down here goes stale the same way a pinned model does — that's
    what made a perfectly good key look broken. Chat models only; the
    transcription and guard models can't answer a request.
    """
    import time
    now = time.time()
    if not force and _models[1] and now - _models[0] < 900:
        return _models[1]
    if not config.get("groq_api_key"):
        return []
    try:
        req = urllib.request.Request(
            MODELS_URL,
            headers={"Authorization": f"Bearer {config.get('groq_api_key')}",
                     "User-Agent": "MusicRequestServer/3.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode("utf-8", "replace")).get("data") or []
    except Exception as exc:
        log.debug("couldn't list models: %s", exc)
        return _models[1]
    # speech and safety models: they answer, but not with a parsed request
    skip = ("whisper", "tts", "guard", "orpheus", "playai", "canopylabs")
    out = sorted(str(m.get("id") or "") for m in rows
                 if m.get("id") and not any(s in str(m["id"]).lower() for s in skip))
    # the one we know parses requests well goes to the top
    out.sort(key=lambda m: (m != DEFAULT_MODEL, m))
    globals()["_models"] = (now, out)
    return out


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


def _number(text: str):
    """"40", "-10", "+10", "40%" -> int, or None."""
    import re
    m = re.search(r"[-+]?\d+", str(text or ""))
    return int(m.group(0)) if m else None


_DOWN = ("quiet", "down", "soft", "lower", "less", "decrease", "reduce")


def _command_plan(word: str, argument: str, said: str) -> Plan | None:
    """A playback command in the words the app understands.

    The model is offered a fixed list, but models are helpful: "skip" for
    "next", "louder" for a volume change. Both are mapped here rather than
    answered with "I don't know how to skip". A level goes in plan.query,
    which is where _run_command reads it -- this used to be dropped, so
    "set the volume to forty" set it to seventy.
    """
    cmd = word.strip().lower().replace(" ", "_").replace("-", "_")
    cmd = ALIASES.get(cmd, cmd)
    query = ""
    if cmd == "volume":
        got = _number(argument)
        if got is not None:
            query = str(max(0, min(150, got)))
    elif cmd == "volume_delta":
        got = _number(argument)
        if got is None:
            # No amount: the direction is in what was said, not in the word.
            down = any(w in f"{said} {word}".lower() for w in _DOWN)
            got = -10 if down else 10
        query = str(max(-50, min(50, got)))
    elif cmd == "add_to_playlist":
        query = argument
        if not query:
            return None          # nowhere to put it; let the grammar have a go
    return Plan(kind="command", command=cmd, query=query, via="llm", spoken=said)


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

    kind = str(data.get("kind") or "").strip().lower()
    if kind == "none":
        # The model saying "this isn't about music" is an answer, and an
        # honest one -- better than letting it invent a song to fill the gap.
        # None hands it back to the grammar, exactly as any decline does.
        return None
    if kind not in ("song", "album", "artist", "genre", "command"):
        return None
    title = str(data.get("title") or "").strip()
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    genre = str(data.get("genre") or "").strip()
    argument = str(data.get("argument") if data.get("argument") is not None else "").strip()
    _state["working"] = True

    if kind == "command":
        return _command_plan(title, argument, text)

    query = {"song": title or album or genre,
             "album": album or title,
             "artist": artist or title,
             "genre": genre or title}.get(kind, "")
    if not query:
        return None
    plan = Plan(kind=kind, query=query, artist=artist,
                variant=_as_bool(data.get("variant")) is True,
                shuffle=_as_bool(data.get("shuffle")),
                via="llm", spoken=text)
    # The model gives back one query, so "nirvana and foo fighters" arrives
    # as a single artist and would be searched for as a band of that name.
    # Same reading as the grammar path, and the resolver checks it the same
    # way before believing it.
    if kind in ("artist", "genre", "auto"):
        parts = split_seeds(plan.query)
        if len(parts) > 1:
            plan.seeds = parts
    return plan
