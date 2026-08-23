# Ideas

Things worth building, and where to get better information about songs. Every
source in the first section was actually called before being written down here,
with no key and no account — the response times are from those calls.

## Better song information, without keys

The queue is only as good as what it knows about a track. Right now that's a
title, an artist, sometimes an album, plus Last.fm tags when a key is set.
These need nothing.

### Deezer public API — the best of them

`https://api.deezer.com/search?q=artist:"Blur" track:"Song 2"` then
`/track/{id}`. No key, no signup. 0.31s.

    bpm      129.6
    gain     -8.6
    release  2000-10-23
    rank     896833
    cover    cdn-images.dzcdn.net/.../cover_xl (1000px)

**BPM is the interesting one.** Nothing else here knows tempo, and tempo is
what separates Song 2 from The Universal far more reliably than any tag does.
It would give:

- "play something upbeat" / "something calm" as real requests
- a queue that doesn't slam from 75bpm to 160bpm between tracks
- a genuine sort for a workout playlist

`gain` is a real ReplayGain figure, which beats mpv guessing at normalisation.
`rank` is a popularity number, useful for picking the actual release over a
reupload when two results look the same.

### iTunes Search API — genre and era, keyless

`https://itunes.apple.com/search?term=...&entity=song`. No key. 0.34s.

    genre        Rock
    released     1997-02-10
    track no     4/39
    artwork      swap 100x100 in the url for 1200x1200
    explicit     notExplicit

Genre straight from Apple, with a release date. That's most of what the theme
feature currently asks Last.fm for, without the key. Worth wiring as the
fallback when `lastfm_api_key` is empty, and the explicit flag would make a
"clean only" setting possible.

### ListenBrainz similar-artists — a keyless Last.fm replacement

`https://labs.api.listenbrainz.org/similar-artists/json?artist_mbids=<mbid>&algorithm=session_based_...`

For Blur: Radiohead, Nirvana, Oasis, Red Hot Chili Peppers, Foo Fighters,
Muse, Arctic Monkeys, Gorillaz.

This is the same shape of signal as `track.getSimilar` — built from what people
actually listen to together — and it needs no key at all. It works on MBIDs, so
it pairs with MusicBrainz below. If this covers enough of the catalogue, Last.fm
stops being needed for the anchor and for scoring, and becomes scrobbling only.

### MusicBrainz + Cover Art Archive — canonical facts

`https://musicbrainz.org/ws/2/recording?query=...&fmt=json`. No key, but it
wants a real User-Agent and no more than one request a second. 0.26s.

    exact length   170373 ms
    recording mbid 105d6b67-...
    release mbid   dd866e86-...  ->  coverartarchive.org/release/<mbid>/front

Exact track length is worth having: the duration-based queue currently trusts
whatever YouTube reports, which includes the silence and the outro on a video.
The release MBID gives real album art from Cover Art Archive rather than a
YouTube channel avatar, which is what half our covers actually are.

### The file we already downloaded

The cheapest source, and currently ignored. yt-dlp can be asked for
`--embed-metadata --embed-thumbnail`, and then the tags are readable locally
with mutagen — or with ffprobe, which is already a dependency:

- artist, album, date, track number, as YouTube Music itself filed them
- the real cover embedded in the file, at full size
- for anything in the local library, whatever tags the user already has

No network at all, and it works offline for the library.

### Local analysis — we already built the tooling

`spectrum.py` already decodes every track with ffmpeg and runs a filter bank
over it. The same pass could produce, for nothing:

- **tempo**, from onset intervals in the low bands
- **energy and brightness**, from the band balance — a real "mood" number
- **true peak and loudness**, for normalisation that doesn't need ReplayGain

That would make "something chill" or "something heavy" work with no network
and no key, on any track, including ones nothing else has heard of.

## Refinements worth making

**Tempo-aware transitions.** With BPM available, don't put a 150bpm track
straight after a 70bpm one unless asked. Crossfade length could follow tempo
too.

**"More like this, but not this artist."** The anchor work made the radio
stick; the opposite request — same feel, new bands — has no way to be asked
for yet.

**Explain the queue.** Each track already carries a `reason` (artist, radio,
anchor, theme, liked). Showing it on hover would make the radio's behaviour
legible instead of mysterious, and make it obvious when it's gone wrong.

**A skip is data.** Skipping in the first ten seconds means something different
from skipping at two minutes. The first is "not this song", the second is
"I've had enough of this" — they should feed the queue differently.

**Duplicate detection across sources.** The same song from the library, from
YouTube and from a playlist import are three different entries today.

**Sleep timer that fades.** It stops dead at the moment; a thirty-second fade
would be kinder.

**Offline mode.** With the library indexed and playlists downloaded, the app
could work with no network at all — worth a visible state rather than a series
of failures.

**Per-device volume memory.** Volume is global; headphones and speakers want
different numbers.

## Notes for later

`getComputedStyle` reports paint properties wrongly in a browser pane that
isn't compositing — an inline `background: #ff0000` reads back as transparent
while layout values stay correct. Screenshot the real window for anything
visual.

Heredocs in this environment eat backslashes, which has twice written literal
backspace bytes (0x08) into regexes and silently broken them. Write files with
the editor tool instead when the content has escapes in it.
