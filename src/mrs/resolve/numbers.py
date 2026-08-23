"""Reading numbers out of dictated speech.

Siri writes the same number a dozen ways — "forty five", "45", "fourty-five",
"no. 45", "1,290" — plus the ones nobody writes as digits: "half an hour",
"a couple of minutes". Everything that wants a number comes through here.
"""

from __future__ import annotations

import re

# No homophones here on purpose. Mapping "for"->4 and "to"->2 to catch the
# odd mis-transcription cost far more than it caught: "for 30 minutes" read as
# four-and-thirty, "set the volume to 65" as two-and-sixty-five. The words are
# too common in ordinary phrasing to treat as digits.
UNITS = {
    "zero": 0, "one": 1, "two": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
SCALES = {"hundred": 100, "thousand": 1000}
# "a minute" and "an hour" mean one of them
ARTICLES = {"a", "an", "another"}
FRACTIONS = {"half": 0.5, "quarter": 0.25, "third": 1 / 3}

_WORD = re.compile(r"[a-z0-9]+")
# "no. 5" / "number 5" / "#5" / "track 5"
_LABEL = re.compile(r"\b(?:no\.?|num(?:ber)?|#|track|song)\s*[:.]?\s*(\d+)\b", re.I)


def _tokens(text: str) -> list[str]:
    t = (text or "").lower()
    t = t.replace("-", " ").replace("&", " and ")
    # 1,290 -> 1290, but leave "1, 2" alone by only joining digit groups
    t = re.sub(r"(\d),(\d{3})\b", r"\1\2", t)
    return _WORD.findall(t)


def _words_to_number(tokens: list[str]) -> float | None:
    """A run of number words to a value. None if there wasn't one."""
    total, current, seen, frac = 0, 0, False, 0.0
    for tok in tokens:
        if tok in UNITS:
            current += UNITS[tok]
            seen = True
        elif tok in TENS:
            current += TENS[tok]
            seen = True
        elif tok in SCALES:
            scale = SCALES[tok]
            current = (current or 1) * scale
            if scale >= 1000:
                total += current
                current = 0
            seen = True
        elif tok in FRACTIONS:
            frac += FRACTIONS[tok]
            seen = True
        elif tok == "and":
            continue
        elif tok.isdigit():
            current += int(tok)
            seen = True
        else:
            break
    if not seen:
        return None
    return total + current + frac


def first_number(text: str) -> float | None:
    """The first number in a phrase, however it was written or said."""
    if not text:
        return None
    label = _LABEL.search(text)
    if label:
        return float(label.group(1))
    toks = _tokens(text)
    for i, tok in enumerate(toks):
        if tok.isdigit() or tok in UNITS or tok in TENS or tok in FRACTIONS:
            value = _words_to_number(toks[i:])
            if value is not None:
                return value
        if tok in ARTICLES:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            # "a hundred", "half an hour", "a couple"
            if nxt in SCALES:
                return float(SCALES[nxt])
            if nxt in ("couple", "few"):
                return 2.0 if nxt == "couple" else 3.0
            if nxt in FRACTIONS:
                continue
            return 1.0
    return None


_MINUTES = re.compile(r"\b(min|mins|minute|minutes)\b", re.I)
_HOURS = re.compile(r"\b(hr|hrs|hour|hours)\b", re.I)
_SECONDS = re.compile(r"\b(sec|secs|second|seconds)\b", re.I)


def duration_minutes(text: str) -> float | None:
    """How long, in minutes. Understands "an hour and a half"."""
    if not text:
        return None
    value = first_number(text)
    if _HOURS.search(text):
        hours = value if value is not None else 1.0
        # "an hour and a half", "two hours thirty"
        tail = _HOURS.split(text, 1)[-1]
        extra = first_number(tail) if tail else None
        if extra is not None:
            hours += extra if extra < 1 else extra / 60.0
        return hours * 60.0
    if _SECONDS.search(text):
        return (value if value is not None else 30.0) / 60.0
    if _MINUTES.search(text) or value is not None:
        return value
    return None


def _chunks(tokens: list[str]) -> list[int]:
    """Split a run into the numbers a person would say as separate parts.

    "nineteen seventy nine" is nineteen, then seventy-nine — the way you say
    a year — not one number to be added up.
    """
    out: list[int] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in TENS:
            value = TENS[tok]
            if i + 1 < len(tokens) and tokens[i + 1] in UNITS:
                value += UNITS[tokens[i + 1]]
                i += 1
            out.append(value)
        elif tok in UNITS:
            out.append(UNITS[tok])
        elif tok.isdigit():
            out.append(int(tok))
        i += 1
    return out


def digit_variants(text: str) -> list[str]:
    """The same phrase with its number words written as digits.

    Two readings, because both happen: "twenty one" is 21 added up, and
    "nineteen seventy nine" is 1979 run together. Always extra searches,
    never a replacement — "Twenty One Pilots" is a band and "21 Pilots"
    is not.
    """
    if not text:
        return []
    words = text.split()
    runs: list[tuple[int, int]] = []      # start, end of each run of number words
    start = None
    for i, word in enumerate(words + [""]):
        w = word.lower().strip(",.")
        is_num = w in UNITS or w in TENS or w in SCALES
        if is_num and start is None:
            start = i
        elif not is_num and start is not None:
            runs.append((start, i))
            start = None
    if not runs:
        return []

    def rebuild(joiner) -> str:
        out, last = [], 0
        for a, b in runs:
            out.extend(words[last:a])
            toks = [w.lower().strip(",.") for w in words[a:b]]
            piece = joiner(toks)
            out.append(piece if piece else " ".join(words[a:b]))
            last = b
        out.extend(words[last:])
        return " ".join(out)

    def added(toks) -> str:
        value = _words_to_number(toks)
        return str(int(value)) if value is not None and value == int(value) else ""

    def joined(toks) -> str:
        parts = _chunks(toks)
        return "".join(str(p) for p in parts) if len(parts) > 1 else ""

    seen, out = {text.lower()}, []
    for joiner in (added, joined):
        variant = rebuild(joiner)
        if variant and variant.lower() not in seen:
            seen.add(variant.lower())
            out.append(variant)
    return out
