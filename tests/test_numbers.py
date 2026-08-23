"""Numbers as people actually dictate them.

Siri writes the same number differently depending on the sentence, and it
mishears small words constantly — "for" for "four", "to" for "two". Every
number the app reads goes through one parser so the quirks are handled once.

    python tests/test_numbers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs.resolve.numbers import duration_minutes, first_number  # noqa: E402

NUMBERS = [
    # plain
    ("5", 5), ("45", 45), ("1290", 1290),
    ("1,290", 1290),                       # dictation puts commas in
    # words
    ("five", 5), ("forty five", 45), ("forty-five", 45),
    ("fourty five", 45),                   # siri spells it wrong, often
    ("one hundred", 100), ("a hundred", 100),
    ("two hundred and fifty", 250),
    ("one thousand", 1000),
    # Deliberately NOT treated as numbers: "for", "to", "too", "won", "ate".
    # They're too common in normal phrasing — mapping them broke "for 30
    # minutes" (read as 34) and "set the volume to 65" (read as 67).
    ("for", None), ("to", None), ("play it for me", None),
    # labelled
    ("no. 5", 5), ("no 5", 5), ("number 5", 5), ("num 5", 5),
    ("#5", 5), ("track 7", 7), ("song 2", 2),
    # articles and vague amounts
    ("a", 1), ("an", 1), ("a couple", 2), ("a few", 3),
    # embedded in a sentence
    ("play something for twenty minutes", 20),
    ("set the volume to 65", 65),
    ("skip forward 3 tracks", 3),
    # nothing to find
    ("play some grunge", None),
    ("", None),
]

DURATIONS = [
    ("for 30 minutes", 30), ("for thirty minutes", 30),
    ("for 30 mins", 30), ("for 30 min", 30),
    ("for an hour", 60), ("for 1 hour", 60), ("for one hour", 60),
    ("for two hours", 120), ("for 2 hrs", 120),
    ("for half an hour", 30),
    ("for an hour and a half", 90),
    ("for 90 seconds", 1.5),
    ("in 10 minutes", 10),
    ("in a couple of minutes", 2),
    ("for 45", 45),                        # bare number means minutes
    ("play some jazz", None),
]


def check(label, got, want, tol=0.01):
    ok = (got is None and want is None) or (
        got is not None and want is not None and abs(got - want) <= tol)
    if not ok:
        print(f"  FAIL {label!r:42s} -> {got!r}, wanted {want!r}")
    return ok


def test_numbers():
    bad = [c for c in (check(t, first_number(t), w) for t, w in NUMBERS) if not c]
    assert not bad, f"{len(bad)} number(s) misread"


def test_durations():
    bad = [c for c in (check(t, duration_minutes(t), w) for t, w in DURATIONS)
           if not c]
    assert not bad, f"{len(bad)} duration(s) misread"


def main() -> int:
    failed = 0
    print("numbers:")
    for text, want in NUMBERS:
        got = first_number(text)
        if check(text, got, want):
            print(f"  ok   {text!r:42s} -> {got}")
        else:
            failed += 1
    print("\ndurations (minutes):")
    for text, want in DURATIONS:
        got = duration_minutes(text)
        if check(text, got, want):
            print(f"  ok   {text!r:42s} -> {got}")
        else:
            failed += 1
    print(f"\n{failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
