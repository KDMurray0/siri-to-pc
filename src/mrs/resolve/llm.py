"""Groq request parsing.

Gotchas: Cloudflare 403s the default urllib UA; Groq retires models so a
pinned one can look like a bad key; free tier is ~8k tokens/min; gpt-oss
returns "false" as a string.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from ..config import config
from ..logging_setup import get
from ..models import Plan, _fold
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

# ── the free tier, kept to ────────────────────────────────────────────
# Groq says in every reply how many tokens are left this minute and when the
# minute resets. Read that, and a request that can't be afforded goes straight
# to the local parser instead of waiting on a 429 -- and nothing is asked at all
# until the reset. The same text asked twice is answered from memory.
_limit = {"left": None, "reset_at": 0.0, "cool_until": 0.0}
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_DAYS = 14
_cache_lock = threading.Lock()


def _seconds(v: str | None) -> float:
    """Groq's "1.5s", "5m45.6s", "120ms" as seconds."""
    total = 0.0
    for n, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(v or "")):
        total += float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _note_limits(headers) -> None:
    try:
        left = headers.get("x-ratelimit-remaining-tokens")
        if left is not None:
            _limit["left"] = int(float(left))
            _limit["reset_at"] = time.monotonic() + _seconds(headers.get("x-ratelimit-reset-tokens"))
    except Exception:
        pass


def resting() -> float:
    """Seconds until Groq should be asked again; 0 when it can be now."""
    return max(0.0, _limit["cool_until"] - time.monotonic())


def _affordable(cost: int) -> bool:
    now = time.monotonic()
    if now < _limit["cool_until"]:
        return False
    if _limit["left"] is not None and now < _limit["reset_at"] and _limit["left"] < cost:
        _limit["cool_until"] = _limit["reset_at"]
        return False
    return True


def _cache_on() -> bool:
    return os.environ.get("MRS_TESTING") != "1"


def _cache_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _cache_file():
    from ..paths import data_dir
    return data_dir() / "groq_cache.json"


def _cache_load() -> None:
    if _cache or not _cache_on():
        return
    try:
        raw = json.loads(_cache_file().read_text("utf-8"))
        cutoff = time.time() - _CACHE_DAYS * 86400
        with _cache_lock:
            _cache.update({k: (t, v) for k, (t, v) in raw.items() if t > cutoff and isinstance(v, dict)})
    except Exception:
        pass


def _cache_get(text: str) -> dict | None:
    if not _cache_on():
        return None
    _cache_load()
    with _cache_lock:
        hit = _cache.get(_cache_key(text))
    return dict(hit[1]) if hit and hit[0] > time.time() - _CACHE_DAYS * 86400 else None


def _cache_put(text: str, data: dict) -> None:
    if not _cache_on():
        return
    from ..paths import write_atomic
    with _cache_lock:
        _cache[_cache_key(text)] = (time.time(), data)
        if len(_cache) > 600:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        snapshot = dict(_cache)
    try:
        write_atomic(_cache_file(), json.dumps(snapshot))
    except Exception as exc:
        log.debug("couldn't keep the groq cache: %s", exc)

_BLANK = {"kind": "song", "title": "", "artist": "", "album": "", "genre": "",
          "argument": "", "variant": False, "shuffle": False,
          "when": "now", "count": 0, "items": []}


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
    ("nirvana and foo fighters", dict(kind="artist", artist="Nirvana and Foo Fighters", genre="grunge")),
    ("korn and some glam metal", dict(kind="mix", genre="metal", items=[
        {"kind": "artist", "name": "Korn"}, {"kind": "genre", "name": "glam metal"}])),
    ("play mother by danzig next", dict(kind="song", title="Mother", artist="Danzig", when="next")),
    ("five songs by queen", dict(kind="artist", artist="Queen", count=5)),
    ("play a few songs from the darker side of alternative rock next", dict(
        kind="genre", genre="dark alternative rock", count=4, when="next")),
    ("songs like motorhead", dict(kind="similar", artist="Motörhead")),
    ("something chill", dict(kind="genre", genre="chill", shuffle=True)),
    ("shuffle my taylor swift", dict(kind="artist", artist="Taylor Swift", shuffle=True)),
    ("the album rumours", dict(kind="album", title="Rumours", album="Rumours", artist="Fleetwood Mac")),
    ("set it to forty", dict(kind="command", title="volume", argument="40")),
    ("crank it up", dict(kind="command", title="volume_delta", argument="10")),
    ("skip this one", dict(kind="command", title="next")),
    ("keep this one", dict(kind="command", title="save")),
    ("add this to my road trip playlist", dict(kind="command", title="add_to_playlist", argument="road trip")),
    ("what's the weather like", dict(kind="none")),
]

SYSTEM = (
    # Every token here is paid for on every request, out of a shared free-tier
    # minute: say each rule once, plainly.
    "Turn ONE spoken music-player request into ONE JSON object, JSON only. "
    "Fields (omit any that would be \"\" or false): "
    + json.dumps(_BLANK | {"kind": "song|album|artist|genre|similar|mix|command|none",
                           "when": "now|next|end"},
                 separators=(",", ":")) + "\n"
    "kind = the FIRST that fits:\n"
    "1 command: controlling playback, naming no music. title is exactly one of: "
    + ", ".join(COMMANDS) + ". stop/hold=pause, keep going/carry on=resume, "
    "skip=next, go back=previous, silence=mute, love this=like, keep this/download "
    "this=save. argument: volume 0-150 "
    "(\"forty\"->\"40\"); volume_delta \"10\" louder, \"-10\" quieter; "
    "add_to_playlist the list's name. Named music is never a command: \"shuffle "
    "my Taylor Swift\" is artist + shuffle.\n"
    "2 none: not about music.\n"
    "3 song: a track. Keep the FULL title (\"i want to break free\" is I Want to "
    "Break Free). artist: as said, else the well-known recording's.\n"
    "4 album: they say album/record, or name an album.\n"
    "5 similar: music LIKE someone, not by them (songs like X, bands similar to X, "
    "sounds like X). artist = X.\n"
    "6 artist: a band or performer; a band whose name is also one of their songs "
    "(play motorhead) is the band. Several: join with \" and \" and put the genre "
    "they share in genre.\n"
    "7 genre: genre, mood, decade (\"90s\"), activity. Several: join with \" and \".\n"
    "8 mix: several SPECIFIC songs, or different kinds together (a band and a genre, a song and a band). "
    "items: [{kind: artist|song|album|genre, name, artist}] where name is the "
    "song, album, band or genre; genre = what they share.\n"
    "Every thing named becomes an anchor the radio keeps returning to: never drop "
    "one, keep the order said.\n"
    "Fix dictation in names, never swap songs. \"coming undone korn\" = title "
    "Coming Undone, artist Korn. variant only for remix/live/acoustic/cover/sped "
    "up/slowed/instrumental. shuffle only for shuffle/random/surprise, or a genre "
    "or mood. when: \"next\" (play X next, after this), \"end\" (add to the queue). "
    "count: a number of songs, including a few (3-5); when multiple songs are "
    "requested, give a genre/artist with count or a mix of named songs, not "
    "one arbitrary track.\n"
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
    # gpt-oss thinks before it answers, and the thinking is billed against the
    # same minute. Parsing a request doesn't need much of it.
    if "gpt-oss" in str(body.get("model", "")) and "reasoning_effort" not in body:
        body = dict(body, reasoning_effort="low")
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {config.get('groq_api_key')}",
                 "Content-Type": "application/json",
                 # Cloudflare blocks the default urllib UA outright.
                 "User-Agent": "MusicRequestServer/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            _note_limits(r.headers)
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = _seconds(e.headers.get("x-ratelimit-reset-tokens")) or \
                float(e.headers.get("retry-after") or 20)
            _limit["cool_until"] = time.monotonic() + max(2.0, wait)
            log.info("Groq is resting for %.0fs", max(2.0, wait))
        raise


def ask_json(system: str, user: str, timeout: float = 8.0) -> dict | None:
    """One JSON answer, or None. For callers that aren't the request parser."""
    if not available() or not user.strip():
        return None
    if not _affordable((len(system) + len(user)) // 3 + 300):
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


def _same_name(a: str, b: str) -> bool:
    def fold(x: str) -> str:
        return re.sub(r"[^a-z0-9]", "", _fold((x or "").lower()))
    return bool(fold(a)) and fold(a) == fold(b)


_SOME = re.compile(r"^(?:some|a\s+bit\s+of|a\s+little)\s+", re.I)
# What a model leaves in "name" when it's split "some Black Label Society" badly.
_FILLER_NAMES = {"some", "any", "a bit", "a bit of", "a little", "stuff", "songs", "music", ""}


def _items(raw) -> list[dict]:
    """The things a mix named, checked: a known kind and a name each."""
    out = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        name = str(row.get("name") or row.get("title") or row.get("album") or "").strip()
        artist = str(row.get("artist") or "").strip()
        # "...and some Black Label Society" is the band, however the model
        # filed it: a song whose name is only its artist is that artist.
        if kind == "song" and artist and (_same_name(_SOME.sub("", name), artist)
                                          or name.strip().lower() in _FILLER_NAMES):
            kind, name = "artist", artist
        if kind in ("artist", "song", "album", "genre") and name:
            out.append({"kind": kind, "name": name[:120], "artist": artist[:120]})
    return out[:6]


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
    data = _cache_get(text)
    if data is None:
        if not _affordable((len(SYSTEM) + len(text)) // 3 + 200):
            log.info("Groq resting %.0fs more -- local parser", resting())
            return None
    try:
        if data is None:
            payload = _post({
                "model": _model(), "temperature": 0, "max_completion_tokens": 400,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": text.strip()}],
            }, float(config.get("groq_timeout", 4)))
            data = json.loads(payload["choices"][0]["message"]["content"])
            if isinstance(data, dict):
                _cache_put(text, data)
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
    items = _items(data.get("items"))
    if kind == "mix" and len(items) > 1 and len({i["kind"] for i in items}) == 1             and items[0]["kind"] in ("artist", "genre"):
        # Two genres, or three bands, is one kind of thing several times over:
        # the ordinary "X and Y" request, which already deals them out in turn.
        joined = " and ".join(i["name"] for i in items)
        data = dict(data, **({"artist": joined} if items[0]["kind"] == "artist"
                             else {"genre": joined}))
        kind, items = items[0]["kind"], []
    if kind == "mix" and len(items) < 2:
        # A "mix" of one thing is that thing.
        if not items:
            return None
        kind = items[0]["kind"]
        data = dict(data, **({"artist": items[0]["name"]} if kind == "artist" else
                             {"genre": items[0]["name"]} if kind == "genre" else
                             {"title": items[0]["name"], "artist": items[0].get("artist", "")}))
    if kind == "none":
        # The model saying "this isn't about music" is an answer, and an
        # honest one -- better than letting it invent a song to fill the gap.
        # None hands it back to the grammar, exactly as any decline does.
        return None
    if kind not in ("song", "album", "artist", "genre", "similar", "command", "mix"):
        return None
    title = str(data.get("title") or "").strip()
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    genre = str(data.get("genre") or "").strip()
    argument = str(data.get("argument") if data.get("argument") is not None else "").strip()
    _state["working"] = True

    if kind == "command":
        return _command_plan(title, argument, text)

    when = str(data.get("when") or "").strip().lower()
    mode = {"next": "next", "end": "queue", "queue": "queue", "later": "queue"}.get(when, "play")
    count = _number(data.get("count"))
    count = int(max(0, min(500, count))) if count else 0
    if kind == "mix":
        names = " and ".join(i["name"] for i in items)
        plan = Plan(kind="mix", query=names, items=items, theme=genre, mode=mode,
                    count=count, shuffle=_as_bool(data.get("shuffle")), via="llm",
                    spoken=text)
        return plan
    if kind == "song" and not album and artist and (not title or _same_name(title, artist)):
        # "the top 10 metallica songs": a song with no name is the band; and
        # "play motorhead" is the band, not their song called Motorhead.
        kind = "artist"
    query = {"song": title or album or genre,
             "album": album or title,
             "artist": artist or title,
             "similar": artist or title,
             "genre": genre or title}.get(kind, "")
    if not query:
        return None
    plan = Plan(kind=kind, query=query, artist=artist,
                variant=_as_bool(data.get("variant")) is True,
                shuffle=_as_bool(data.get("shuffle")),
                via="llm", spoken=text, mode=mode, count=count,
                theme=genre if kind == "artist" else "")
    # The model gives back one query, so "nirvana and foo fighters" arrives
    # as a single artist and would be searched for as a band of that name.
    # Same reading as the grammar path, and the resolver checks it the same
    # way before believing it.
    if kind in ("artist", "genre", "auto"):
        parts = split_seeds(plan.query)
        if len(parts) > 1:
            plan.seeds = parts
    return plan
