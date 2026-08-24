# Ideas

Things worth building, and why. Not a roadmap — a list with reasons, so a
future me can tell the good ones from the ones that only sound good.

Each entry says what it is, what evidence there is for it, and roughly what it
would cost. Struck-through entries are ones that were tried and didn't work;
they stay here so they don't get suggested again.

---

## Queue

### 1. Say why a track scored what it did

**What:** the queue already shows which lane picked each row. It can't show
*why that row beat the others*. A debug view — score, and the terms that made
it (lane weight, tag similarity, era gap, affinity, kinship, taste) — behind a
setting.

**Evidence:** every queue problem this project has had was diagnosed by writing
a throwaway harness outside the app: the funk-at-12% tag, the era signal
running backwards, `electronic` vouching for Avicii, the affinity list never
being fetched. Each took a separate script to see. The app has all those
numbers at the moment it ranks and throws them away.

**Cost:** small. `_rank` already computes every term; keep them in a dict on the
Candidate behind a config flag, expose on `/api/queue`, render on hover.

**Risk:** the dict costs memory per candidate on a 300-candidate pool. Only
build it when the flag is on.

---

### 2. "Not this, here"

**What:** a per-queue dismissal, distinct from a skip. Skipping says "not now";
this says "this doesn't belong in *this* queue" and drops the artist from the
current run without touching long-term taste.

**Evidence:** the ranker can be wrong in a way no signal catches. A Pyramid
Song queue kept reaching for Nickelback because Nickelback genuinely is tagged
alternative rock, is the right era, and has no negative signal against it. It
took two code changes to fix, and there will always be another one. One click
would have fixed that night's listening instantly.

**Cost:** small. A per-anchor exclusion set in `Queue`, cleared when the anchor
changes. Plumb a button through `/api/queue/{op}`.

**Watch out for:** it must not leak into `taste.py`. Disliking a band in a
Radiohead queue is not disliking the band.

---

### 3. Space the same artist out

**What:** a minimum gap between two tracks by one artist — say four — rather
than the current per-refill count limit.

**Evidence:** runs regularly put two tracks by one act close together (Glen
Campbell at 3 and 4, Portishead at 2 and 3, Soft Cell at 4 and 5). A mild
same-artist bias is wanted, and back-to-back isn't the same thing as mild.
The existing `artist_counts` limit works per refill, so two from one artist
can still land adjacent.

**Cost:** small, and it belongs in `Queue` at pick time, not in `_rank` —
ranking doesn't know the running order.

---

### 4. Seed from more than one song

**What:** "play something like Aphex Twin and Boards of Canada". The anchor is
a single Track everywhere; make it a small list and average the comparisons.

**Evidence:** `_run_fit` already scores against the last ten records, so the
machinery for comparing against a set exists. The anchor is the only thing
still stuck at one.

**Cost:** medium. `anchor` is threaded through `build`, `_rank`, `era`, `kin`
and `prime_near`. Worth doing as one change, not incrementally.

---

## Backend

### 5. A diagnostics page — *endpoint done, page not*

**What:** one screen showing what the external services are doing — cache
sizes (tags, eras, kin, catalog), how many lookups are queued, circuit-breaker
state on the YouTube client, recent failure counts and the last error per
service.

**Evidence:** three separate bugs this session were invisible from inside the
app and only showed up because a test harness printed the intermediate value:
MusicBrainz 503ing on the anchor and silently disabling the era check for a
whole run; Deezer returning a 140-fan stub for Michael Jackson; the affinity
cache never being populated for the anchor. All three were "a service quietly
returned nothing", which is exactly the failure a status page catches.

**Cost:** small-to-medium. The stores already hold the numbers; it's mostly a
read-only endpoint and a template.

**Done so far:** `/api/health` returns all of it — per-store counts, queued
lookups, whether each worker is alive, the circuit breaker, cache size on
disk, queue depth. What's left is somewhere to *look* at it: a block in the
settings sheet, refreshed off the existing SSE stream.

---

### 6. Play from the cache when the network is down

**What:** if YouTube is unreachable, fall back to the already-downloaded files
rather than stopping.

**Evidence:** downloads are cached to disk already, and `library.py` indexes
local files, so both halves exist. Right now a network blip during a refill
means the queue starves and playback stops at the end of the track.

**Cost:** medium. Needs a real "are we offline" signal rather than guessing
from one failure — the circuit breaker in `catalog._yt` is the natural place.

---

### 7. Use the listening history the user already has

**What:** `extras.py` scrobbles to Last.fm. It could also *read* — the user's
own top artists and loved tracks — and seed `taste.py` on first run.

**Evidence:** taste starts empty and takes weeks to learn anything, and the
credentials for the read are already configured and working.

**Cost:** small. `user.getTopArtists` and `user.getLovedTracks`, once, into the
existing taste store.

**Watch out for:** taste is deliberately bounded to ±1 as a tiebreaker. Import
should respect that and not create a huge prior.

---

### 8. Retire the 30-minute catalog cache in favour of one that persists

**What:** `catalog._cache` is in-memory, 400 entries, 30 minutes. Searches for
the same artist repeat across restarts.

**Evidence:** the related-artists and played-alongside lanes both search by
name, and those names are stable for a given anchor. Everything else with this
access pattern (tags, eras, kin) is already on disk and it works well.

**Cost:** small, and mostly copying the pattern out of `kin.py`.

**Watch out for:** search results genuinely do change, unlike an artist's
birth year. Needs a real expiry, not "forever".

---

## Player

### 9. Fade the sleep timer out

**What:** the sleep timer stops. It could take the last thirty seconds down
gently instead.

**Evidence:** `audio.py` already does crossfade and volume ramps, so the
mechanism is there.

**Cost:** small.

---

### 10. Undo a skip

**What:** the last skipped track goes back to the front, and the skip is
withdrawn from `taste.py`.

**Evidence:** skips feed the ranker with a penalty. A mis-hit on the media key
teaches it something false, and there's currently no way to take it back.

**Cost:** small.

---

### 11. Back the profile up

**What:** one button that writes config, playlists, play stats and the three
caches to a zip, and one that reads it back.

**Evidence:** a playlist called ".." resolved to the data directory and the
delete endpoint rmtree'd it — the api key, the cookies, weeks of play stats
and every cache, from one call. That's fixed, but the amount of state now
sitting in one folder with no copy of it anywhere is the actual lesson. The
tag, era and kin caches alone are thousands of lookups that took days of
listening to accumulate.

**Cost:** small. It's one directory and everything in it is already json.

**Watch out for:** config.json holds the api key and the Last.fm session. An
export is a credential file — say so, and don't put it in Downloads by
default.

---

### 12. A global budget on outside calls

**What:** one place that counts calls per service per minute and makes the
lanes back off when they're over, rather than each store pacing itself on its
own.

**Evidence:** a cold refill makes roughly thirty network calls across four
services, and each one paces itself in isolation — Last.fm at 5/sec,
MusicBrainz at 1/sec, Deezer at 3/sec, YouTube behind a circuit breaker. They
have no idea about each other, so the actual burst when a new song starts is
whatever they all happen to do at once. It has been fine so far, but the only
reason we know is that the breaker hasn't opened much.

**Cost:** medium. The pacing logic is duplicated in four stores already;
pulling it into one limiter would remove that duplication as well.

---

### 13. Self-test in the shipped build

**What:** `MusicRequestServer.exe --selftest` — boot the app, hit the health
endpoint, check every store loads and every worker starts, print a verdict,
exit.

**Evidence:** the test suite lives outside the repo and runs against source.
Nothing checks the *frozen* build beyond it launching, and PyInstaller
problems are exactly the kind that only show up frozen — a missing hidden
import, a template not collected, a data file in the wrong place.

**Cost:** small. The boot check in the test suite already does this; it needs
an argv flag and somewhere to print.

---

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
