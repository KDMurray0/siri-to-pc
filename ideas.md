# Ideas

Things worth building, and why. Not a roadmap — a list with reasons, so a
future me can tell the good ones from the ones that only sound good.

Each entry says what it is, what evidence there is for it, and roughly what it
would cost. Struck-through entries are ones that were tried and didn't work;
they stay here so they don't get suggested again.

---

## Still to build

### 1. Say why a track scored what it did

**What:** the queue shows which lane picked each row. It can't show *why that
row beat the others*. A debug view — score, and the terms that made it (lane
weight, tag similarity, era gap, affinity, kinship, taste, fair share) —
behind a setting.

**Evidence:** every queue problem this project has had was diagnosed by
writing a throwaway harness outside the app: the funk-at-12% tag, the era
signal running backwards, `electronic` vouching for Avicii, the affinity list
never being fetched, PRIMARY_GENRE handing a free point to whichever seed was
named first. Each took a separate script to see. The app has all those
numbers at the moment it ranks and throws them away.

**Cost:** small. `_rank` already computes every term; keep them in a dict on
the Candidate behind a config flag, expose on `/api/queue`, render on hover.

**Risk:** a dict per candidate on a 300-candidate pool. Only build it when the
flag is on.

---

### 2. "Not this, here"

**What:** a per-queue dismissal, distinct from a skip. Skipping says "not
now"; this says "this doesn't belong in *this* queue" and drops the artist
from the current run without touching long-term taste.

**Evidence:** the ranker can be wrong in a way no signal catches. A Pyramid
Song queue kept reaching for Nickelback because Nickelback genuinely is
tagged alternative rock, is the right era, and has nothing against it. That
took two code changes to fix and there will always be another one. One click
would have fixed that evening's listening immediately.

**Cost:** small. A per-anchor exclusion set in the queue, cleared when the
anchor changes, and a button through `/api/queue/{op}`.

**Watch out for:** it must not leak into taste. Disliking a band in a
Radiohead queue is not disliking the band. The undo-skip work already built
the withdrawal half of this.

---

## Built

Kept here with what actually shipped, because the reasoning is worth more
than the checkbox.

- **3. Space the same artist out.** `artist_gap` (4) prefers somebody who
  hasn't been on for a few records; `artist_gap_slip` (0.15) lets it through
  anyway about one time in seven. The hard run cap sits underneath and
  doesn't bend. *The adjacency I first cited as evidence was a harness
  artefact — `longrun.py` takes `pool[0]` and never calls `_take_candidate`.
  Check which code path a log came from before believing it.* Also fixed:
  shuffle mode never shuffled, because `random.shuffle(usable[:12])` shuffles
  a copy.
- **4. Seed from more than one thing.** "nirvana and foo fighters", "some
  thrash and black metal". The noun said once at the end is carried back; an
  ampersand never splits; the whole phrase is checked against real artists
  before a split is believed. Every comparison takes the *nearest* anchor,
  not the average. Needed three separate fixes to stop the first-named seed
  running away with the queue — the similarity blend, PRIMARY_GENRE, and the
  genre lane depths — plus a fair-share term, because the pool is picked from
  by score and being present in it isn't enough. Measured 6/6 on Dolly Parton
  + Massive Attack.
- **5. Diagnostics.** `/api/health` plus a readout under Settings → System.
- **6. Play from the cache when the network is down.** Measured first: a
  refill spent 11.6s failing and returned nothing, so playback stopped at the
  end of the track and requests claimed the song didn't exist. No route out
  is now told apart from being told no.
- **7. Seed taste from Last.fm.** Once, on the first start after connecting,
  bounded to the tiebreaker taste is meant to be.
- **8. Search answers that outlive the process.** A week on disk instead of
  half an hour in memory.
- **9. Fade the sleep timer.**
- **10. Undo a skip.** Withdraws the song count, the artist count and the
  fatigue bump.
- **11. Back the profile up.** *Two bugs of my own, both found by
  round-tripping it rather than reading it.*
- **12. One queue per outside service.** The pacing moved from the background
  workers down to the calls, because the blocking lookups skipped it
  entirely — which is why MusicBrainz kept 503ing.
- **13. `--selftest` in the shipped build.** Found a real regression on its
  first run.

## Tried, didn't work — don't re-suggest

- ~~**BPM as an energy proxy for ranking.**~~ Anti-correlated on this
  catalogue: Creed at 127 and Chop Suey at 129 scored 0.96 alike, while
  Disturbed at 180 scored 0.00 against both. `tempo.py` stays because
  crossfade uses it, which is a different job.
- ~~**AcousticBrainz classifiers.**~~ Still online and keyless, but wrong here:
  Nickelback's *How You Remind Me* comes back `genre_dortmund: electronic`
  at p=1.00 and `ismir04_rhythm: VienneseWaltz` at p=0.88.
- ~~**ListenBrainz for similarity.**~~ Needs a recording MBID resolved first —
  two hops for a job Last.fm affinity does in one — and duplicates a signal
  already in use.
- ~~**Wikidata for genre and influences.**~~ Genres are too broad to be a
  search term (Michael Jackson: pop, rock, soul, disco, samba) and
  `influenced by` looks backwards — it returns Fred Astaire and Jackie
  Wilson, which would actively hurt a Billie Jean queue.
- ~~**Apple/iTunes genre lookup.**~~ Measured at 44% precision and deleted.
