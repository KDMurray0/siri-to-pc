# Music Request Server — feature inventory & refactor scope

**Status: for review. Nothing is being rebuilt until you sign this off.**

This is what the app does today, how every interaction currently behaves, what's
actually broken, and what I'd propose changing. Mark it up however you like —
strike things out, reorder, add. The "Proposed" sections are suggestions, not
decisions.

Current size: **6,725 lines** across 11 files (`player.py` 2,133 · `app.py` 964 ·
`player.html` 925 · `search.py` 815 · `launcher.pyw` 690 · `matching.py` 405).

---

## 1. What it does today

### 1.1 Request pipeline

| Stage | Detail |
|---|---|
| Input | iOS Shortcut `POST /` · web search box · `GET /api/play` · media keys |
| Parse | Local grammar (`matching.py`) → Groq LLM (`interpret.py`) → local fallback |
| Resolve | `ytmusicapi` search: song / album / artist / genre / mood, taste-ranked |
| Fetch | `yt-dlp` downloads to `%TEMP%\mrs_audio_cache` (streaming 403s, so download-then-play) |
| Play | `mpv` over a Windows named pipe (`\\.\pipe\mpvsocket`), async overlapped I/O |
| Fallback | YouTube id → alternate ids → SoundCloud → skip to next track |

### 1.2 Playback

- Play / pause / next / previous / seek / shuffle / repeat (off · all · one)
- Volume 0–150 (mpv boost above 100), persisted across restarts
- **Crossfade** — off/3s/6s/10s, dual-mpv: incoming track preloads on a second
  mpv, old ramps down while new ramps up, then hands back to the primary
- **Normalize** — `dynaudnorm` targeting consistent RMS
- **EQ presets** — flat, bass, treble, vocal, etc.
- **Sleep timer** — 15 / 30 / 60 min
- **Crash recovery** — watchdog restarts mpv and resumes the current track if the
  IPC pipe dies
- Windows media keys + SMTC (song title shows in the Windows volume overlay)
- Spoken "now playing" announcements (edge-tts neural voice, ducks the music)

### 1.3 Queue

- **Endless auto-queue** ("radio") seeded from the current track + recent context
- Target depth **12**, grows +1 per song played this session, capped at 26
- Refills immediately below 5 remaining; otherwise paced 8–60s by how full it is
- Skipping a lot deepens the buffer and speeds up refills
- **Artist lock** — same-artist candidates get a dominant score boost, plus the
  current artist's catalogue is pulled into the candidate pool
- **De-dupe** — by video id *and* by normalized `artist|title`
- Album/artist requests play pure (no radio) until the queue is exhausted
- Editable: reorder ↑↓, remove ✕, click a row to jump
- History section (already-played) collapses behind a toggle

### 1.4 Taste engine

- Tracks play-throughs vs skips per song and per artist (`play_stats.json`)
- **A song counts as "played" at 30% listened** — that's the completion threshold
- Liked songs (`liked_songs.json`) bias both search resolution and the queue
- "More like this" / "Radio from this" on demand
- Recently-played list and top-artists chips

### 1.5 UI (tray flyout, 400×640)

- Album art with an accent colour sampled live from the artwork
- Synced lyrics (LRCLIB) — click a line to seek there
- Panels: **Queue · Lyrics · Recent · Lists** + settings gear
- Search box with a live dropdown of up to 12 song matches
- Playlists: save the current track, list, play, delete
- 7 themes, persisted server-side
- Mini mode, pin/always-on-top, drag-to-move, click-through game overlay

### 1.6 Platform

- Single-file `.exe` (PyInstaller), server runs in-process
- Tray icon: show player · open in browser · quit
- Start-on-boot, LAN access with API key, optional IP allowlist
- `setup.ps1` installer; `/api/diag` health check

---

## 2. Every UI interaction (this is the part to scrutinise)

| Control | Today's behaviour |
|---|---|
| Album art / title | Drag anywhere to move the window (native drag loop) |
| Progress bar | Click or drag to seek |
| Play button | Toggle; icon swaps play/pause |
| Prev / Next | Crossfades when crossfade is on |
| Shuffle | Shuffles the current queue immediately |
| Repeat | Cycles off → all → one |
| Heart | Like/unlike; also queues 3 similar songs |
| Download ↓ | Copies the current track to `~/Music/MusicRequest`, flashes a tick |
| Volume | Drag, or **scroll wheel** over the slider/icon |
| Search box | Types → 340ms debounce → dropdown (opens **upward**) |
| Search Enter | Smart play (whole phrase through the parser) |
| Search row click | Plays that exact track |
| Play next / Add to queue / Radio | Appear under the search box once you type |
| Queue row click | Jump to that track |
| Queue row ↑↓✕ | Reorder / remove |
| **Queue tab (tap again)** | Expands the queue (hides art + secondary row) |
| Lyrics tab | Loads lyrics, auto-scrolls, click a line to seek |
| Lyrics line click | Seeks 0.3s early so you don't miss the word |
| Recent tab | History rows + top-artist chips, click to play |
| Lists tab | Playlists; uses a `prompt()` for naming |
| Gear | Slide-up settings sheet |
| Pin 📌 | Always-on-top, stops auto-hide |
| Shrink ⤢ | Mini mode |
| **Mini hover** | Expands 344×80 → 344×108, reveals transport + vertical volume |
| Click outside | Auto-hides (unless pinned) |
| Ctrl+Alt+M | Toggles click-through while a fullscreen game is up |
| Keyboard | Space, ←/→, ↑/↓, N, P, L, / |

---

## 3. Known bugs

### 3.1 Confirmed this session

| # | Bug | Root cause (verified) |
|---|---|---|
| B1 | **Groq "saved but no response"** | **You run from source, which reads `src/config.json` — and that file has an empty key, `use_groq: false`, and the retired `llama-3.1-8b-instant`. The working key is in `%LOCALAPPDATA%`, which only the .exe reads.** Two configs, silently diverged. |
| B2 | Downloads failing → queue stalls | Your log: `YouTube failed … falling back to SoundCloud` → `Skipping (no source)`. Both sources failed and the queue didn't recover. |
| B3 | Crossfade never completes | Your log shows `[crossfade] begin N->N+1` with **no matching `handoff` line** — it starts and dies mid-way. |
| B4 | Mini mode broken without art | Screenshot 4: no artwork, text and controls collapse into the middle. |
| B5 | `server.log` unbounded | Currently **3.8 MB**, no rotation. |

### 3.2 Reported by you, not yet root-caused

| # | Bug | My working hypothesis |
|---|---|---|
| B6 | 30-second versions of songs | Either YouTube preview streams, or the resolver picking preview/short uploads. Needs a repro to be sure. |
| B7 | Overlapping searches | A new request doesn't cancel the in-flight one; they race and the loser can still mutate the queue. |
| B8 | Mini expands off-centre | Resize is anchored top-left, so it grows downward instead of around its centre. |
| B9 | "No cookies found" in the log | The warning fires only when neither cookie option is set — it was true before `setup.ps1` ran. `cookies_file` is set correctly now; needs re-checking on a fresh run. |
| B10 | Groq **and** local parser both running | Real, by design: `match_transport` → `match_command` run **first**, and only if neither matches does Groq get a look. So "skip this" never reaches Groq. Worth deciding deliberately. |
| B11 | Cookie file stores every site | The export contains TikTok etc. Should be filtered to YouTube/Google domains only. |

### 3.3 Queue questions you asked

- **"Are you building the queue from songs listened to 30% through?"** — Yes.
  `ratio >= 0.30` marks a song completed; completed songs feed the context seed
  and the taste weights. Skips are recorded separately and penalise the artist.
- **"How are failed downloads handled?"** — Badly. A failed track is skipped, but
  the batch loop `break`s on `appended >= batch`, and a run of failures can end
  with nothing appended and no retry until the next interval. This is B2 and I
  think it's your "the program just stops trying".

---

## 4. Proposed work

### 4.1 Fixes (I'd do these regardless)

1. **One config, one location** — kill the source/frozen split (B1). Single file,
   migrated on first run, with the path printed at startup.
2. **Download resilience** (B2) — retry with backoff, try the next candidate,
   never let a run of failures leave the queue empty; surface it in the UI.
3. **Crossfade** (B3) — instrument the whole handoff and fix the mid-fade death.
4. **Mini mode** (B4, B8) — sane layout with no artwork; centre-anchored resize.
5. **Cancel in-flight searches** (B7) — generation token; a new request wins.
6. **Cookie hygiene** (B11) — filter the jar to YouTube/Google domains on import.
7. **Log rotation** (B5).
8. **Prefer originals** (B6) — reject sub-60s results unless the track really is
   that short, and penalise "preview"/"snippet" uploads.

### 4.2 Your requests

| Request | My take |
|---|---|
| **Cookie test on boot + hourly refresh** | Built and ready (uncommitted). Tests at boot, re-extracts from a browser when they die. Honest limit: a **running Chromium browser locks its cookie DB**, so refresh only lands when it's closed. Firefox works while open. |
| **"Find cookies" button in settings** | Yes — runs the same routine on demand, with live status. |
| **Opt-in: close Chrome to grab cookies** | Yes, opt-in only. Watch for Chrome exiting and grab them then; optionally offer to close it *with a confirmation*, never silently. |
| **Open YouTube on first boot to get cookies** | **This can't work** — opening the site doesn't hand cookies to yt-dlp, and having the browser open is precisely what blocks extraction. The equivalent that *does* work: detect no valid cookies → prompt → user closes Chrome → auto-grab. |
| **Loading states: Finding / Downloading / Loading** | Yes. Needs a progress channel (see async below). |
| **Right-click → restart** | Yes, tray + window context menu. |
| **Spotify playlist link → spotdl** | Yes: paste a playlist URL, resolve names via the Spotify API or `spotdl`, then feed them through the normal resolver. Note `spotdl` is already installed in your Python and **conflicts with our `ytmusicapi` pin** — I'd shell out to it rather than import it. |
| **Async server (FastAPI/Sanic)** | Agree with the direction, with a caveat: the real blocker isn't Flask, it's that `yt-dlp` and mpv IPC are blocking calls on worker threads. Async only helps if those move to a proper job queue. I'd do **FastAPI + a task queue + SSE/WebSocket push**, which also gives you live progress and kills the 1s status polling. That's the single biggest structural win here. |

### 4.3 Future ideas (mine — say no freely)

- **Multi-room / cast** to other machines running the same app
- **Local library** indexing so owned files play instantly with no download
- **Per-person profiles** (separate taste per household member)
- **Offline cache pinning** — keep favourites permanently
- **Last.fm scrobbling**
- **Alarm / wake-up playlist**
- **Real audio visualiser** — WASAPI loopback capture + FFT (needs numpy back in
  the build; currently excluded to keep it small)
- **Web remote** — the full player UI on your phone, not just the setup page
- **Undo** for queue edits
- **Gapless playback** for albums

---

## 5. Refactor shape (if you want the deep version)

Current pain: `player.py` is 2,133 lines doing mpv IPC, downloads, queue, taste,
crossfade, playlists, stats and TTS. `app.py` mixes routing with orchestration.

Proposed split:

```
core/     mpv_client   (IPC only)
          downloader   (yt-dlp, retries, cache, fallbacks)
          queue        (ordering, dedupe, refill policy)
          taste        (stats, ranking)
          audio        (eq / normalize / crossfade)
resolve/  parser       (local grammar + LLM, one interface)
          catalog      (ytmusicapi, soundcloud, spotify)
web/      api          (FastAPI routes)
          events       (SSE/WebSocket push)
ui/       player.html  (split into modules)
```

Same behaviour, testable pieces, and the async/progress work becomes possible
rather than bolted on.

---

## 6. Screenshots

Attached alongside this document:

1. `01-full.png` — main player, idle
2. `02-settings.png` — settings sheet (note: **Groq shows unset**, which is B1)
3. `03-lyrics.png` — lyrics panel
4. `04-mini.png` — mini mode, hovered (**B4 visible** — no art, layout collapses)
5. `05-mini-idle.png` — mini mode, not hovered

---

## 7. What I need from you

1. Anything in §1/§2 that should **change or go**
2. Priority order for §3 bugs
3. Yes/no on each §4.2 item
4. Anything from §4.3 worth keeping
5. Whether you want the **full §5 refactor** or targeted fixes only
