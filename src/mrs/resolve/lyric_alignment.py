"""Measured word timings for the exact downloaded recording.

LRCLIB's normal LRC knows line starts, not the words inside each line.  A
background Whisper transcription supplies audible word spans; lyric-align
matches those spans back to the known lyric text.  Unmatched or implausible
lines retain their trustworthy LRC line timing, without a fabricated karaoke
schedule.  Successful results are cached by recording and transcript.
"""

from __future__ import annotations

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
_CACHE_VERSION = 2  # v1 redistributed characters instead of preserving ASR word spans.


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


def _match_words(text: str, audio: list[dict], cursor: int,
                 anchor: float, end: float | None = None) -> tuple[list[dict], int]:
    """Match a complete line to consecutive recognized words near its anchor.

    Punctuation/case may differ, and an ASR split such as ``I`` + ``'m`` may
    supply one lyric word. Missing or changed words leave the line untimed;
    we never allocate sound to words by their length or fill silent gaps.
    """
    wanted: list[tuple[str, str]] = []
    prefix = ""
    for match in re.finditer(r"\S+\s*", text):
        display = match.group()
        token = _normal_word(display)
        if token:
            wanted.append((token, prefix + display))
            prefix = ""
        elif wanted:
            token_before, text_before = wanted[-1]
            wanted[-1] = (token_before, text_before + display)
        else:
            prefix += display
    if not wanted:
        return [], cursor
    candidates = []
    for first in range(cursor, len(audio)):
        if audio[first]["t"] < anchor - 1.5:
            continue
        if audio[first]["t"] > anchor + 2.2:
            break
        pos, matched = first, []
        for token, display in wanted:
            beginning, combined = pos, ""
            while pos < len(audio) and pos < beginning + 3:
                # Joining fragments of a contraction must not span a breath.
                if pos > beginning and audio[pos]["t"] - audio[pos - 1]["end"] > .2:
                    break
                combined += audio[pos]["token"]
                pos += 1
                if combined == token:
                    matched.append({"text": display, "t": audio[beginning]["t"],
                                    "end": audio[pos - 1]["end"]})
                    break
                if not token.startswith(combined):
                    break
            else:
                break
            if combined != token:
                break
        if (len(matched) == len(wanted) and
                (end is None or matched[-1]["end"] <= end + .5) and
                all(a["end"] <= b["t"] + .03 for a, b in zip(matched, matched[1:]))):
            candidates.append((abs(matched[0]["t"] - anchor), matched, pos))
    if not candidates:
        return [], cursor
    _, matched, pos = min(candidates, key=lambda candidate: candidate[0])
    return matched, pos


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
