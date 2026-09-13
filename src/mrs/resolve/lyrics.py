"""Synced lyrics from LRCLIB (free, no key)."""

from __future__ import annotations

import json
import re
import urllib.parse
import time
import urllib.request

from ..logging_setup import get

log = get("lyrics")

API = "https://lrclib.net/api/get"
SEARCH = "https://lrclib.net/api/search"
UA = {"User-Agent": "MusicRequestServer/2.0 (personal music player)"}
_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_cache: dict[str, dict] = {}


def _fetch(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _parse_synced(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        stamps = _TIME.findall(line)
        if not stamps:
            continue
        words = _TIME.sub("", line).strip()
        for mins, secs in stamps:
            out.append({"t": int(mins) * 60 + float(secs), "text": words})
    out.sort(key=lambda r: r["t"])
    return out


_WORDS = re.compile(r"[^a-z0-9\s]+")


def _flat(text: str) -> str:
    """Lyrics with the punctuation and spacing arguments removed.

    Nobody types the apostrophes, half the transcriptions disagree about
    them anyway, and "dont" should find "don't".
    """
    return " ".join(_WORDS.sub(" ", (text or "").lower()).split())


HUNT = (
    "You identify songs from a fragment of their lyrics. Reply as JSON: "
    '{"songs": [{"title": "...", "artist": "..."}]}. Up to four candidates, '
    "most likely first. Use the exact recorded title and the main credited "
    "artist. If the words are not from a song you recognise, reply "
    '{"songs": []}. Never invent a song to fill the list.'
)


def hunt(fragment: str) -> list[dict]:
    """Which song has these words in it.

    Two steps, and the second is the one that matters. A language model will
    name a song for any line you give it, confidently, including lines that
    are not from a song at all — so every candidate it offers is checked
    against the actual words of that recording before it is allowed to count
    as an answer. LRCLIB holds the words; if the fragment is really in there,
    we know rather than hope.

    Candidates that fail the check are kept, last and marked, because a
    verified miss is still often the right song — the lyric database simply
    may not have that recording. The caller can say "probably" instead of
    pretending to be sure.
    """
    want = _flat(fragment)
    if len(want) < 6:
        return []
    from . import llm
    got = llm.ask_json(HUNT, fragment.strip(), timeout=8.0) or {}
    rows = got.get("songs") if isinstance(got.get("songs"), list) else []

    sure, maybe = [], []
    for row in rows[:4]:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        artist = (row.get("artist") or "").strip()
        if not title:
            continue
        found = get_lyrics(title, artist)
        words = _flat((found or {}).get("plain") or "")
        hit = {"title": title, "artist": artist, "verified": bool(words and want in words)}
        (sure if hit["verified"] else maybe).append(hit)
    if sure:
        log.info("lyric %r -> %s — %s (in the words)", fragment[:40],
                 sure[0]["artist"], sure[0]["title"])
    elif maybe:
        log.info("lyric %r -> %s — %s (unconfirmed)", fragment[:40],
                 maybe[0]["artist"], maybe[0]["title"])
    return sure + maybe


# How far a transcription's length may be from the song's before its timings
# can't be trusted. YouTube uploads run a few seconds long with silence at the
# ends; a radio edit or a live take is out by thirty to eighty.
_SAME_RECORDING = 5.0


def _pick(rows, duration: float) -> tuple[dict | None, bool]:
    """The transcription of the recording that is actually playing.

    Returns (row, trust_timings). A search for a song returns every recording
    of it anyone has transcribed, and they are not interchangeable: a radio
    edit has the verses cut, so its first lines agree with the album version
    to a tenth of a second and everything after the first cut drifts. Taking
    the first result -- which this did -- gave Plush a 230-second edit
    against a 310-second track: right for the first verse, eighty seconds
    wrong by the end. Lyrics that start in time and fall apart is exactly the
    fault, and it only happened on the fallback, which is why it came and went.

    So match on length. If nothing is close enough, the words are still worth
    showing, but not timed: highlighting the wrong line with confidence is
    worse than not highlighting at all.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)
            and (r.get("syncedLyrics") or r.get("plainLyrics"))]
    if not rows:
        return None, False
    if not duration:
        # Nothing to compare against; prefer a timed one and hope.
        synced = [r for r in rows if r.get("syncedLyrics")]
        return (synced or rows)[0], True

    def off(r):
        d = r.get("duration") or 0
        return abs(float(d) - float(duration)) if d else float("inf")

    close = [r for r in rows if off(r) <= _SAME_RECORDING]
    if close:
        # Timed beats untimed; among those, the nearest length.
        best = min(close, key=lambda r: (0 if r.get("syncedLyrics") else 1, off(r)))
        return best, True
    return min(rows, key=off), False


def get_lyrics(title: str, artist: str, duration: int = 0) -> dict | None:
    if not title:
        return None
    # The length is part of what a result is *for*: the same title by the
    # same artist in two recordings needs two answers, not whichever was
    # looked up first.
    key = f"{artist}|{title}|{int(round(duration or 0))}"
    if key in _cache:
        return _cache[key]

    params = {"track_name": title, "artist_name": artist or ""}
    if duration:
        params["duration"] = str(int(duration))
    get_url = f"{API}?{urllib.parse.urlencode(params)}"
    data = _fetch(get_url)
    if not data:
        # Once more before giving up on the exact match. It 503'd on two of
        # twelve real tracks in a row, both answered straight away on retry,
        # and the exact match -- which is duration-checked on LRCLIB's side --
        # is always better than choosing among search results ourselves.
        time.sleep(0.25)
        data = _fetch(get_url)
    trust = True
    if not data:
        rows = _fetch(f"{SEARCH}?{urllib.parse.urlencode({'q': f'{artist} {title}'})}")
        data, trust = _pick(rows if isinstance(rows, list) else [], duration or 0)
    if not data:
        return None

    result = {
        "synced": _parse_synced(data.get("syncedLyrics") or "") if trust else [],
        "plain": data.get("plainLyrics") or "",
        "title": data.get("trackName") or title,
        "artist": data.get("artistName") or artist,
    }
    # Untrusted timings, no plain text: the words are only in the timed
    # version. Keep them, as plain lines, rather than lose them altogether.
    if not trust and not result["plain"] and data.get("syncedLyrics"):
        result["plain"] = "\n".join(r["text"] for r in
                                    _parse_synced(data["syncedLyrics"]))
    if not result["synced"] and not result["plain"]:
        return None
    if len(_cache) > 100:
        _cache.clear()
    _cache[key] = result
    return result
