"""Measured word timings for the exact downloaded recording.

LRCLIB's normal LRC knows line starts, not the words inside each line.  A
background Whisper transcription supplies audible word spans; lyric-align
matches those spans back to the known lyric text, misheard words and all.  A
line too little of which was heard keeps its LRC line timing.  Successful
results are cached by recording and transcript.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("lyric-alignment")
_lock = threading.Lock()
_jobs: set[str] = set()
_failed: set[str] = set()
_worker_slots = threading.Semaphore(1)
_CACHE_VERSION = 3  # v2 timed a line only if every word was heard exactly.


def _key(track, lines: list[dict]) -> str | None:
    try:
        path = Path(track.path)
        if not path.is_file():
            return None
        stat = path.stat()
        source = json.dumps([_CACHE_VERSION, track.video_id, str(path), stat.st_size,
                             stat.st_mtime_ns, [(r["t"], r["text"]) for r in lines]],
                            ensure_ascii=False)
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
    except (AttributeError, OSError, TypeError, KeyError):
        return None


def _file(key: str) -> Path:
    folder = data_dir() / "lyric-alignment"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (key + ".json")


def _job_file(key: str) -> Path:
    return _file(key).with_suffix(".job.json")


def _apply(data: dict, cached: dict) -> dict:
    if cached.get("kind") == "plain":
        lines = cached.get("texts") or []
        matches = cached.get("lines") or {}
        if len(matches) < max(2, round(len([line for line in lines if line]) * .35)):
            return {**data, "word_sync": "lines"}
        return {**data, "synced": [
            {"t": matches[str(i)]["t"] if str(i) in matches else None,
             "text": line,
             **({"words": matches[str(i)]["words"]}
                if str(i) in matches and matches[str(i)].get("words") else {})}
            for i, line in enumerate(lines)],
            "word_sync": "measured"}
    copied = {**data, "synced": [dict(line) for line in data.get("synced") or []]}
    count = 0
    for key, words in cached.items():
        try:
            idx = int(key)
            if 0 <= idx < len(copied["synced"]) and words:
                copied["synced"][idx]["words"] = words
                count += 1
        except (TypeError, ValueError):
            continue
    copied["word_sync"] = "measured" if count else "lines"
    return copied


def _plain_lines(text: str) -> list[str]:
    """Make provider prose into phrases before asking the audio to place it."""
    out = []
    for source in (text or "").splitlines():
        line = source.strip()
        if not line:
            if out and out[-1]:
                out.append("")
            continue
        if len(line) <= 55:
            out.append(line)
            continue
        for phrase in re.split(r"(?<=[.!?;])\s+", line):
            chunk, count = "", 0
            for word in phrase.split():
                if chunk and (len(chunk) + len(word) > 52 or count >= 8):
                    out.append(chunk)
                    chunk, count = "", 0
                chunk += (" " if chunk else "") + word
                count += 1
                if count >= 4 and word.endswith((",", ";", ":")):
                    out.append(chunk)
                    chunk, count = "", 0
            if chunk:
                out.append(chunk)
    return out


def enrich(track, data: dict | None) -> dict | None:
    if not data or not (data.get("synced") or data.get("plain")):
        return data
    synced = data.get("synced") or []
    lines = synced or [{"t": None, "text": line} for line in _plain_lines(data.get("plain") or "")]
    # Third-party lyric bodies are untrusted and optional alignment should
    # never spend unbounded CPU or memory on an enormous transcription.
    if len(lines) > 300 or sum(len(r.get("text", "")) for r in lines) > 20000:
        return {**data, "word_sync": "lines"}
    if synced and all(not r.get("text", "").strip() or r.get("words") for r in lines):
        return {**data, "word_sync": "supplied"}
    key = _key(track, lines)
    if not key:
        return {**data, "word_sync": "lines"}
    try:
        cached = json.loads(_file(key).read_text("utf-8"))
        if isinstance(cached, dict):
            return _apply(data, cached)
    except (OSError, ValueError):
        pass
    with _lock:
        if key in _failed:
            return {**data, "word_sync": "lines"}
        if key not in _jobs:
            _jobs.add(key)
            threading.Thread(target=_run_child,
                             args=(key, str(track.path), lines, not bool(synced)),
                             daemon=True, name="lyric-alignment").start()
    return {**data, "word_sync": "aligning"}


def _run_child(key: str, path: str, lines: list[dict], plain: bool) -> None:
    """A native audio crash cannot bring down the player or its queue."""
    job = _job_file(key)
    try:
        with _worker_slots:
            write_atomic(job, json.dumps({"path": path, "lines": lines,
                                          "plain": plain}, ensure_ascii=False))
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--lyric-align", key]
            else:
                launcher = Path(__file__).resolve().parents[3] / "launcher.pyw"
                cmd = [sys.executable, str(launcher), "--lyric-align", key]
            result = subprocess.run(cmd, timeout=240, close_fds=True,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    check=False)
            if result.returncode or not _file(key).is_file():
                raise RuntimeError(f"worker exited {result.returncode}")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        log.warning("audio alignment skipped; ordinary lyrics retained: %s", exc)
        with _lock:
            _failed.add(key)
    finally:
        try:
            job.unlink(missing_ok=True)
        except OSError:
            pass
        with _lock:
            _jobs.discard(key)


def run_job(key: str) -> int:
    """Entry point for the isolated alignment process."""
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        return 2
    try:
        payload = json.loads(_job_file(key).read_text("utf-8"))
        path = Path(payload["path"])
        lines = payload["lines"]
        if (not path.is_file() or not isinstance(lines, list) or
                not all(isinstance(r, dict) for r in lines) or
                len(lines) > 300 or sum(len(r.get("text", "")) for r in lines) > 20000):
            return 2
        _align(key, str(path), lines, bool(payload["plain"]))
        return 0 if _file(key).is_file() else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 2


def _normal_word(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if c.isalnum())


def _audio_words(segments) -> list[dict]:
    """Keep original audible ends, including silence between successive words."""
    words = []
    for segment in segments:
        for word in segment.words or []:
            token = _normal_word(word.word)
            start, end = float(word.start), float(word.end)
            if (token and math.isfinite(start) and math.isfinite(end) and
                    0 <= start < end):
                words.append({"token": token, "t": start, "end": end})
    return sorted(words, key=lambda w: (w["t"], w["end"]))


# A line counts as heard when this share of its words is; Whisper mishears
# sung words often enough ("streamed" for "screamed") that asking for every
# one left four lines in five to the syllable guess.
_HEARD = .6
_NEAR = .62            # spelling likeness for the same word misheard
_JOIN = .8             # stricter where two words meet one
_SKIP_LYRIC, _SKIP_AUDIO = -.45, -.35


def _alike(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    return r if r >= _NEAR else 0.0


def _lyric_words(text: str) -> list[tuple[str, str]]:
    """(token, what's shown) per word, punctuation riding on its word."""
    wanted: list[tuple[str, str]] = []
    prefix = ""
    for match in re.finditer(r"\S+\s*", text):
        display = match.group()
        token = _normal_word(display)
        if token:
            wanted.append((token, prefix + display))
            prefix = ""
        elif wanted:
            wanted[-1] = (wanted[-1][0], wanted[-1][1] + display)
        else:
            prefix += display
    return wanted


def _match_words(text: str, audio: list[dict], cursor: int,
                 anchor: float, end: float | None = None) -> tuple[list[dict], int]:
    """Time a line's words from the recognised words near its anchor.

    Lyric and audio words are lined up allowing misheard, missing and extra
    words, and a word heard as two or two heard as one. Too few heard and the
    line is left untimed. The misheard ones are placed between the heard
    words either side of them, by length, and carry no end of their own.
    """
    wanted = _lyric_words(text)
    if not wanted:
        return [], cursor
    m = len(wanted)
    stop = (end if end is not None else anchor + 4 + .6 * m) + .5
    lo = cursor
    while lo < len(audio) and audio[lo]["t"] < anchor - 1.5:
        lo += 1
    hi = lo
    while hi < len(audio) and audio[hi]["t"] < stop:
        hi += 1
    window = audio[lo:hi]
    n = len(window)
    if not n:
        return [], cursor
    neg = -1e9
    # best[i][j]: i lyric words placed using the first j window words. Audio
    # before the line and after it costs nothing; skips inside it do.
    best = [[neg] * (n + 1) for _ in range(m + 1)]
    back: list[list[tuple | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    for j in range(n + 1):
        best[0][j] = 0.0
    for i in range(1, m + 1):
        token = wanted[i - 1][0]
        for j in range(n + 1):
            top, how = neg, None
            if best[i - 1][j] > neg and best[i - 1][j] + _SKIP_LYRIC > top:
                top, how = best[i - 1][j] + _SKIP_LYRIC, ("lyric",)
            if j and best[i][j - 1] > neg and best[i][j - 1] + _SKIP_AUDIO > top:
                top, how = best[i][j - 1] + _SKIP_AUDIO, ("audio",)
            if j:
                s = _alike(token, window[j - 1]["token"])
                if s and best[i - 1][j - 1] + s > top:
                    top, how = best[i - 1][j - 1] + s, ("pair", 1)
            # one word heard as two ("i" + "m"), never across a breath
            if j >= 2 and window[j - 1]["t"] - window[j - 2]["end"] <= .2:
                s = _alike(token, window[j - 2]["token"] + window[j - 1]["token"])
                if s >= _JOIN and best[i - 1][j - 2] + s - .1 > top:
                    top, how = best[i - 1][j - 2] + s - .1, ("pair", 2)
            # two heard as one ("gotta" for "got to")
            if i >= 2 and j:
                s = _alike(wanted[i - 2][0] + token, window[j - 1]["token"])
                if s >= _JOIN and best[i - 2][j - 1] + 2 * s - .3 > top:
                    top, how = best[i - 2][j - 1] + 2 * s - .3, ("both",)
            best[i][j], back[i][j] = top, how
    j = max(range(n + 1), key=lambda k: best[m][k])
    if best[m][j] <= 0:
        return [], cursor
    heard: list[tuple[float, float] | None] = [None] * m
    i = m
    while i > 0 and back[i][j] is not None:
        how = back[i][j]
        if how[0] == "lyric":
            i -= 1
        elif how[0] == "audio":
            j -= 1
        elif how[0] == "pair":
            k = how[1]
            heard[i - 1] = (window[j - k]["t"], window[j - 1]["end"])
            i, j = i - 1, j - k
        else:
            w = window[j - 1]
            mid = (w["t"] + w["end"]) / 2
            heard[i - 2], heard[i - 1] = (w["t"], mid), (mid, w["end"])
            i, j = i - 2, j - 1
    got = [h for h in heard if h]
    if len(got) < max(1, math.ceil(_HEARD * m)):
        return [], cursor
    first_at = next(k for k, h in enumerate(heard) if h)
    if not anchor - 1.5 <= got[0][0] <= anchor + 2.2 + .4 * first_at:
        return [], cursor
    if end is not None and got[-1][1] > end + .5:
        return [], cursor
    if any(a[1] > b[0] + .03 for a, b in zip(got, got[1:])):
        return [], cursor
    out: list[dict] = []
    i = 0
    while i < m:
        h = heard[i]
        if h:
            out.append({"text": wanted[i][1], "t": round(h[0], 3), "end": round(h[1], 3)})
            i += 1
            continue
        k = i
        while k < m and not heard[k]:
            k += 1
        run = wanted[i:k]
        before = out[-1]["end"] if out else None
        nxt = heard[k][0] if k < m else None
        if before is None:
            # leading: into the first heard word at a singable pace
            a, b = nxt - min(.3 * len(run), max(.12, nxt - anchor)), nxt
        elif nxt is None:
            # trailing: after the last heard word, short of the next line
            a, b = before, min(before + .32 * len(run), end - .05 if end is not None else math.inf)
            if b <= a:
                b = a + .08 * len(run)
        else:
            a, b = before, max(nxt, before + .04 * len(run))
        weights = [len(token) + 1.5 for token, _ in run]
        total, acc = sum(weights), 0.0
        for (_, display), weight in zip(run, weights):
            out.append({"text": display, "t": round(a + (b - a) * acc / total, 3)})
            acc += weight
        i = k
    if any(b["t"] <= a["t"] for a, b in zip(out, out[1:])):
        return [], cursor
    used = got[-1][1]
    pos = lo
    while pos < len(audio) and audio[pos]["t"] < used - 1e-6:
        pos += 1
    return out, pos


def _align(key: str, path: str, lines: list[dict], plain: bool = False) -> None:
    try:
        from lyric_align import align
        from lyric_align.asr import transcribe

        wanted = [(i, line) for i, line in enumerate(lines) if line.get("text", "").strip()]
        segments = transcribe(path, language="en", model_size="base.en",
                              device="cpu", vad=False)
        audio = _audio_words(segments)
        # Plain lyrics need coarse line anchors. Normal LRC already provides
        # them, so avoid the library's stanza/breath splitting there entirely.
        aligned = (align(segments, [line["text"] for _, line in wanted], karaoke=False)
                   if plain else [None] * len(wanted))
        result: dict[str, object] = {}
        cursor = 0
        for (idx, line), measured in zip(wanted, aligned):
            if plain and (not measured.matched or measured.score < .6 or
                          measured.start is None):
                continue
            anchor = measured.start if plain else line["t"]
            if not isinstance(anchor, (int, float)) or not math.isfinite(anchor):
                continue
            end = next((r["t"] for r in lines[idx + 1:]
                        if isinstance(r.get("t"), (int, float)) and
                        math.isfinite(r["t"]) and r["t"] > anchor), None)
            timed, after = _match_words(line["text"], audio, cursor, anchor, end)
            if timed:
                cursor = after
                result[str(idx)] = {"t": timed[0]["t"], "words": timed} if plain else timed
            elif plain:
                # A credible coarse anchor still permits line highlighting;
                # uncertain individual words must not receive guessed spans.
                result[str(idx)] = {"t": measured.start, "words": []}
        payload = {"kind": "plain", "texts": [r["text"] for r in lines],
                   "lines": result} if plain else result
        write_atomic(_file(key), json.dumps(payload, ensure_ascii=False))
        log.info("measured words for %d of %d lyric lines", len(result), len(wanted))
    except Exception as exc:
        log.warning("word alignment unavailable: %s", exc)
        with _lock:
            _failed.add(key)
    finally:
        with _lock:
            _jobs.discard(key)
