# Ideas

Things worth building, and where to get better information about songs. Every
source here was actually called before being written down, with no key and no
account — the timings are from those calls.

The rule this follows: **out of the box should be good enough, and a little
setup should make it good.** A missing key should cost quality, never function.

## Built since this list started

- **Apple's genre search** (`attribute=genreTerm`) — a real query against the
  whole catalogue, popularity-ordered, no key, ~0.5s, has an answer for grime
  and bossa nova alike. Now the main keyless route for genre requests.
- **Deezer BPM** — drives crossfade length, and nothing else. A big tempo jump
  gets a short overlap, a small one gets the full fade.
- **Why a track is queued** — asked / artist / your pick / genre / liked /
  similar, on each queue row.
- **Skips read by depth** — bailing early is "not this song"; bailing late is
  "enough of this", which loosens artist cohesion instead of blaming the band.

Tried and dropped, so nobody re-treads them: MusicBrainz tag lookups (accurate
but obscure — Mudhoney B-sides), YouTube's curated genre shelves (only exist
for a dozen broad words), Deezer's genre index (returns the same global top
ten for every genre), and interleaving all four sources (slower, and put each
source's worst guess at the top).

## Still worth getting

### The file we already downloaded

The cheapest source, still ignored. yt-dlp takes `--embed-metadata
--embed-thumbnail`, and ffprobe is already a dependency:

- artist, album, date and track number as YouTube Music itself filed them
- the real cover art embedded at full size, rather than a channel avatar
- whatever tags the user already has, for anything in the local library

No network at all, and it works offline.

### Mood from the audio, which we already analyse

`spectrum.py` decodes every track and runs a filter bank over it. The same pass
could produce, for nothing:

- **energy and brightness** from the band balance — a real mood number
- **tempo**, from onset intervals in the low bands, for tracks Deezer misses
- **true peak and loudness**, for normalisation that doesn't need ReplayGain

This is the one signal that could separate Song 2 from The Universal without
asking anyone. Tags call them 0.86 alike because tags describe Blur; the
waveforms are nothing like each other. Worth trying as a same-artist tie-break.

### ListenBrainz similar artists

`labs.api.listenbrainz.org/similar-artists/json?artist_mbids=...` — keyless,
and for Blur returns Radiohead, Nirvana, Oasis, Foo Fighters. The same shape of
signal as Last.fm's, so it would close the last real gap between having a key
and not having one.

## Refinements

**A status panel.** Every bad queue this project has produced was a service
degrading quietly — a genre lookup returning nothing, cookies going stale, a
radio call throwing and being swallowed. One screen saying what answered, how
recently, and how old the cookies are would have caught most of them the day
they started.

**"More like this, but not this artist."** The anchor made the radio stick;
the opposite request has no way to be asked for.

**Duplicate detection across sources.** The same song from the library, from
YouTube and from a playlist import is three entries. The normalisation suite
has the tools now.

**Sleep timer that fades.** It stops dead mid-track; thirty seconds of fade is
kinder to fall asleep to.

**Offline mode.** With the library indexed and playlists downloaded this could
run with no network — today that shows up as a cascade of failures rather than
a state.

**Per-device volume.** Volume is global; headphones and speakers want different
numbers, and the output picker is already there to hang it off.

## Notes for later

`getComputedStyle` reports paint properties wrongly in a browser pane that
isn't compositing — an inline `background: #ff0000` reads back as transparent
while layout values stay correct. Screenshot the real window for anything
visual.

Heredocs in this environment eat backslashes, which has three times written
literal backspace bytes (0x08) into regexes and silently broken them. Write
files with the editor tool when the content has escapes in it.

Test more than one example. Genre lookup worked on grunge and britpop and was
broken for reggae, jazz and hip-hop; the artist filter looked right on Blur and
was letting a song called "Blur" by someone else through.
