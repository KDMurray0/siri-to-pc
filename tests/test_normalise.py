"""Normalisation is what stops the same song being queued twice under two
different names, so it's worth being suspicious of.

    python -m pytest tests -q          (or: python tests/test_normalise.py)

Two things it has to get right, and they pull against each other:

  same song, different listing  -> same key   (dedupe works)
  different songs               -> different keys (nothing gets swallowed)

The second is the dangerous one. An over-eager rule silently drops music you
asked for, and it looks like the queue skipping rather than a bug. This file
has bitten twice already: "with" once truncated Bullet With Butterfly Wings to
"bullet", and article-stripping was added because The Smashing Pumpkins and
Smashing Pumpkins were dodging each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs.models import Track, is_derivative, norm_title  # noqa: E402

# --- pairs that must collapse together ---------------------------------
SAME = [
    ("the same song listed twice",
     ("Song 2", "Blur"), ("Song 2", "Blur")),
    ("case and spacing",
     ("SONG 2", "BLUR"), ("song 2", "blur")),
    ("punctuation",
     ("Don't Stop Me Now", "Queen"), ("Dont Stop Me Now", "Queen")),
    ("the definite article",
     ("1979", "The Smashing Pumpkins"), ("1979", "Smashing Pumpkins")),
    ("a remaster tag",
     ("Bigmouth Strikes Again", "The Smiths"),
     ("Bigmouth Strikes Again (2017 Master)", "The Smiths")),
    ("a year-stamped remaster",
     ("Would?", "Alice In Chains"), ("Would? (2022 Remaster)", "Alice In Chains")),
    ("a featured artist",
     ("Teardrop", "Massive Attack"),
     ("Teardrop (feat. Elizabeth Fraser)", "Massive Attack")),
    ("feat. written out",
     ("Nothing Burns", "Snoh Aalegra"),
     ("Nothing Burns featuring Someone", "Snoh Aalegra")),
    ("a bracketed descriptor",
     ("Enemy", "Imagine Dragons"),
     ("Enemy (Opening Title Version)", "Imagine Dragons")),
    ("a second credited artist",
     ("Enemy", "Imagine Dragons"), ("Enemy", "Imagine Dragons, JID")),
    ("trailing whitespace",
     ("Creep ", " Radiohead"), ("Creep", "Radiohead")),
    ("an em dash",
     ("Song 2", "Blur"), ("Song—2", "Blur")),
]

# --- pairs that must stay apart ----------------------------------------
DIFFERENT = [
    ("two songs by one band",
     ("Song 2", "Blur"), ("The Universal", "Blur")),
    ("one title, two bands",
     ("Creep", "Radiohead"), ("Creep", "Stone Temple Pilots")),
    ("a title that merely starts the same",
     ("Song 2", "Blur"), ("Song 2 You", "Blur")),
    ("the word 'with' mid-title",
     ("Bullet With Butterfly Wings", "The Smashing Pumpkins"),
     ("Bullet", "The Smashing Pumpkins")),
    ("the word 'live' mid-title",
     ("Live Forever", "Oasis"), ("Forever", "Oasis")),
    ("numbered sequels",
     ("Song 2", "Blur"), ("Song 3", "Blur")),
    ("a band whose name starts with 'the' inside a word",
     ("Hysteria", "Theory of a Deadman"), ("Hysteria", "Muse")),
    ("mix as part of the actual name",
     ("Remix Culture", "Someone"), ("Culture", "Someone")),
]


def check(label, a, b, want_same):
    ka, kb = norm_title(*a), norm_title(*b)
    ok = (ka == kb) if want_same else (ka != kb)
    verdict = "ok  " if ok else "FAIL"
    if not ok:
        print(f"  {verdict} {label}")
        print(f"        {a!r} -> {ka!r}")
        print(f"        {b!r} -> {kb!r}")
    return ok, label, ka, kb


def test_same_song_collapses():
    bad = [c for c in (check(l, a, b, True) for l, a, b in SAME) if not c[0]]
    assert not bad, f"{len(bad)} pairs failed to collapse"


def test_different_songs_stay_apart():
    bad = [c for c in (check(l, a, b, False) for l, a, b in DIFFERENT) if not c[0]]
    assert not bad, f"{len(bad)} pairs wrongly collapsed"


def test_empty_input_is_not_a_key():
    # an empty key must be falsy, or every untitled track dedupes against
    # every other one and the queue silently loses them
    assert norm_title("", "") == ""
    assert norm_title("", "Blur") == ""
    assert norm_title("   ", "Blur") == ""
    assert norm_title("(Live)", "Blur") == ""


def test_key_matches_norm_title():
    t = Track(title="Song 2", artist="Blur")
    assert t.key() == norm_title("Song 2", "Blur")


def test_primary_artist():
    cases = [
        ("The Smashing Pumpkins", "smashing pumpkins"),
        ("Smashing Pumpkins", "smashing pumpkins"),
        ("Imagine Dragons, JID", "imagine dragons"),
        ("  Blur  ", "blur"),
        ("Theory of a Deadman", "theory of a deadman"),
        ("", ""),
    ]
    for raw, want in cases:
        got = Track(title="x", artist=raw).primary_artist()
        assert got == want, f"{raw!r} -> {got!r}, wanted {want!r}"


def test_derivative_detection():
    derivative = [
        "Song 2 (Live)", "Creep - Acoustic", "Zombie (Remix)",
        "Toxicity karaoke", "In The End (Sped Up)", "Duality 8D",
        "Numb (Lyric Video)", "Song 2 [Live at Wembley]",
        "Wonderwall — Acoustic", "Creep (Slowed + Reverb)",
        "Everlong (Acoustic Version)", "Bodies nightcore",
        "Toxicity (Live from Armenia)", "Song 2 (Radio Edit)",
    ]
    # real releases whose titles happen to contain one of those words
    genuine = [
        "Live Forever", "Live and Let Die", "Alive", "Remixology",
        "Song 2", "The Universal", "Coverdale Page", "Editors",
        "Cover Me", "Live Wire", "Living on a Prayer", "Mixed Emotions",
        "The Livery Stable", "Editorial",
    ]
    for t in derivative:
        assert is_derivative(t), f"{t!r} should count as derivative"
    for t in genuine:
        assert not is_derivative(t), f"{t!r} is a real release, not a derivative"


def main() -> int:
    checks = [("must collapse", SAME, True), ("must stay apart", DIFFERENT, False)]
    failed = 0
    for heading, cases, want in checks:
        print(f"\n{heading}:")
        for label, a, b in cases:
            ok, *_ = check(label, a, b, want)
            if ok:
                print(f"  ok   {label}")
            else:
                failed += 1

    print("\nother checks:")
    for fn in (test_empty_input_is_not_a_key, test_key_matches_norm_title,
               test_primary_artist, test_derivative_detection):
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")

    print(f"\n{failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
