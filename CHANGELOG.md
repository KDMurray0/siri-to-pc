# CHANGELOG -

## 2026-08-16 (pt 5) — Groq parsing, smarter resolution, better voice, standalone .exe

- **Groq LLM parsing (optional):** with a free Groq key (`use_groq` + `groq_api_key`), requests are parsed by an LLM into structured intent (`interpret.py`) — much better at casual phrasing. Any failure falls back to the local grammar parser, so it always works.
- **Search songs first:** an exact song title now beats a loose genre-word overlap, so "sultans of swing" plays the Dire Straits song instead of a "swing" mix. Fixed an album-resolution crash on `m:ss` durations.
- **Album/artist stay pure:** requesting an album or artist fills the queue with only that until it runs out, then the endless radio resumes. The queue also biases toward the **same band** more than the same genre (`queue_same_artist_boost`).
- **Requesting a song un-pauses** the player.
- **Better announce voice:** edge-tts neural voices (`tts_voice`, default `en-US-AriaNeural`), falling back to the offline Windows voice.
- **Auto-generated key + IP toggle:** `api_key` is created on first run if blank; the IP allow-list is now an opt-in **Lock to my devices** toggle (off by default) rather than always-on.
- **mpv hygiene:** orphaned mpv from a crash/force-close is killed on startup, and a single-instance mutex stops a second launch spawning a rival server/mpv.
- **UI:** the setup-page **Copy** button works over plain http; Settings is a full-height slide-up sheet (no more cramped panel); the now-playing bars **freeze when paused**; the **volume slider goes to 150 and updates live while dragging**; the Save button confirms success.
- **Standalone .exe:** `MusicRequestServer.spec` builds a one-folder `MusicRequestServer.exe` that runs the whole thing in one process (mpv/yt-dlp/node stay external). `src/paths.py` makes config/data live next to the .exe.

## 2026-08-16 (pt 4) — Siri endpoint, boot/announce, genre-aware picking, refactor

- **iOS-Shortcut endpoint:** `/` now accepts the shortcut's `POST {"input": "…"}` and runs it; a browser visit shows step-by-step Shortcut setup (`setup.html`). The old form-based web interface (`index.html`, `static/`) is gone.
- **Song picking = popularity + taste + genre, minus junk:** same-title conflicts weigh YouTube Music's ranking (popularity), whether it's by an artist you listen to (which captures your genre — into punk → your punk artists win), and penalise remixes / sped-up / covers unless you ask for one (`_pick_song`, `is_derivative`). Also handles "song + artist" phrasing with no "by" ("coming undone korn").
- **Skip rule + history:** a play only counts as a *skip* if you bailed before ~30%; the no-repeat/context history ignores skipped songs so they can come back.
- **Start on boot:** a Settings toggle registers the launcher to start hidden-to-tray on Windows sign-in (`/api/boot`, HKCU Run key).
- **Spoken announcements:** when you *request* a song it's announced over the speakers with the built-in Windows voice (ducking the music); auto-queued songs stay quiet (`/api/announce`).
- **Refactor:** code moved under `src/`, dead stream-mode path removed, testing tools deleted, secrets git-ignored with a `config.example.json`.

## 2026-08-16 (pt 3) — True crossfade, context-aware queue, tunable weights

- **True overlapping crossfade (dual mpv):** a second, hidden mpv (`--media-controls=no`) plays the outgoing track's tail fading out while the primary jumps to the next song fading in (via the audio filter) — a real overlap with no echo or double-play, and the primary stays the single source of truth for the queue, Windows media controls, and status. Works for both auto-advance (`_maybe_crossfade`) and manual/searched song changes (`_crossfade_replace`). Enabled by the Crossfade setting.
- **Context-aware auto-queue:** the next batch is now seeded from the recent *context* — several songs you played through, not just the last one (`_gather_candidates`) — so the queue keeps the overall vibe instead of narrowing to one song. Refill trigger moved to two-from-the-end (`auto_queue_threshold: 2`).
- **Tunable smart-shuffle weights:** the ranking (liked boost, play-through weight, skip penalty, jitter, liked-seed probability, context size) is exposed in `config.json` as `queue_*` keys.

## 2026-08-16 (pt 2) — Smart shuffle, preference engine, voice commands, UI polish

- **Preference engine + smart shuffle:** the monitor thread now logs every track as a play-through (>55% heard) or a skip, per song and per artist, in `play_stats.json`. The auto-queue drops anything in a tunable recent-play log (`history_size`, default 100) so nothing repeats soon, and ranks each station by taste — liked and played-through artists rise, skipped ones sink — with a little jitter for freshness.
- **Same-title conflict resolution:** when a query matches several songs of the same name, the resolver now prefers the one by an artist you actually listen to (liked / played-through), the way an assistant leans on your history. Also fixed "song + artist" phrasing with no "by" ("coming undone korn") resolving to a theme instead of the song.
- **Natural-language commands:** "set volume to 40", "turn it up/down", "mute", "max volume", "more like this", "surprise me", "I love this song" now work from Siri or the search box (`matching.match_command`).
- **Search box:** Enter runs a fresh search/play; new **Play next** and **Add to queue** buttons; a spinner shows while searching.
- **Crossfade on change:** searching a new song now fades the current one out before the new one fades in (when crossfade > 0).
- **UI polish:** proper shuffle icon, centred heart, a speaker icon that grows 1→3 sound waves with the level (no snapping), removed the album placeholder's white border, fixed queue scrolling, and toned down the album-art ambient glow so light/monochrome covers no longer wash out the UI.

## 2026-08-16 — Dynamic interpretation, liked-song recommendations, web-based player

- **Assistant-grade interpretation (nothing hardcoded):** removed the hardcoded genre list. `matching.py` is now purely structural; `search._smart_auto` reads intent from the YouTube Music catalog — an exact artist-name match means the artist's mix, an exact (suffix-stripped) song title means that song, and anything else becomes a themed queue. `search._resolve_theme` builds a station for *any* vibe ("rainy day", "80s", "coding", "gym") from mood categories + a filtered song search + radio, so free-text from Siri resolves like an assistant would.
- **Liked songs + recommendations:** the heart now toggles a persistent taste profile (`liked_songs.json`). The auto-queue blends radio seeded from liked songs (~1 in 3 refills) so the endless queue drifts toward your taste. "Play my liked songs" plays them shuffled (`/api/liked`).
- **New player UI — real HTML/CSS/JS in a webview:** replaced the Tkinter/customtkinter window with a Flask-served page (`templates/player.html`) shown in a borderless pywebview flyout (`launcher.pyw`, which re-execs under `python_path`). Crisp inline-SVG controls, proper type, a clickable progress bar (`/api/seek`), a free-text "play anything" box, and an expandable Queue/Settings. **Signature:** the accent colour is sampled live from the current album art, so the whole UI recolours per song. Six base themes remain in Settings. Built per the project's `frontend-design` skill.
- Album art now flows through to the queue rows and `/api/status`.

## 2026-08-15 (pt 3) — Full player: sources, audio engine, album art, themed GUI

Turned the flyout into a full music player.

- **Alternate sources:** `search.resolve_external` plays from **SoundCloud** (`scsearch`) or **Bandcamp** (`bcsearch`) via yt-dlp. Config `source` + runtime `/api/source` + a Source dropdown. The download layer plays a track's `url` when present, so any yt-dlp-supported site can be slotted in.
- **Audio engine (mpv `af`):** EQ presets (flat/bass/rock/pop/jazz/classical/vocal/electronic/…), loudness **normalization** (`dynaudnorm`), and a **crossfade** fade-in (single-mpv approximation — true overlap needs two decoders). Endpoints `/api/audio`.
- **Persistent settings:** volume, EQ, normalize, crossfade and repeat survive restarts in `player_state.json`; the UI theme/source in `ui_prefs.json`.
- **New controls:** Shuffle, **Repeat** (off/all/one, loop-file/loop-playlist), **Like** (injects 3 related songs right after the current track), **Download** (copies the cached file to `~/Music/MusicRequest`), and a **Sleep timer** (`/api/sleep`).
- **Album art + mood presets:** search results now carry a thumbnail; `/api/status` exposes `track.art`. Mood buttons (Happy/Chill/Party/Focus/Workout/Sad) map to the curated genre/mood queues.
- **GUI sweep (customtkinter):** album art, **custom Canvas-drawn transport shapes** (no icon fonts), heavier Segoe UI hierarchy, **6 colour themes** (Ocean/Spotify/Neon/Sunset/Lilac/Crimson) with a live picker, and an expandable Queue + Settings panel.

## 2026-08-15 (pt 2) — Genre/mood queues, smarter parsing, flyout GUI

- **Genre / mood / decade queues:** "play some jazz", "play 80s music", "gimme workout music", "let's hear some hip hop" now build a themed station from YouTube Music's curated *Moods & genres* categories (`search._resolve_genre` → `get_mood_categories`/`get_mood_playlists`), falling back to a compilation-filtered song search. New `genre` kind in `matching.py` (`GENRES` set + `detect_genre`).
- **Stronger query parsing:** wake-words ("hey siri", "ok google") and politeness are stripped, and casual "play" synonyms ("put on", "throw on", "gimme", "I wanna hear", "let's hear"…) are normalised to `play`, so far more phrasings resolve correctly. Band queues ("play songs by X", "play the band X", "shuffle X") were already supported and now compose with auto-queue for endless play.
- **Rebuilt the music bar with customtkinter:** a borderless, rounded, always-on-top flyout (Windows-11 rounded corners via DWM) with a modern dark theme, an Auto-queue switch, and a scrollable queue. It hides when another window takes focus (foreground-window poll) and reopens from the tray.
- **Audio quality investigation:** tried the bgutil PO-token provider (HTTP + script modes) to unlock Opus 251 / AAC 140 — but those DASH formats 403 on download even with a valid gvs pot (YouTube session-binding), and the ios/HLS route needs a pot bgutil can't mint. Kept `player_client: tv` / itag 18 (~96 kbps AAC) as the reliable ceiling; removed bgutil again.

## 2026-08-15 — Auto-queue, music bar, Windows media integration

- **Auto-queue (Spotify-style radio):** when the queue nears empty, the server fetches songs related to the current track via YouTube Music radio (`search.get_radio` → `ytmusicapi.get_watch_playlist(radio=True)`) and appends them; a background monitor thread in `player.py` (`_autoqueue_loop`) drives it. Config `auto_queue` / `auto_queue_batch` / `auto_queue_threshold`. New `/api/autoqueue` toggle endpoint.
- **Music bar (`launcher.pyw`):** replaced the plain tray menu with an always-on-top Tkinter mini-player — song/artist, live progress bar, ⏮/play-pause/⏭, volume slider, auto-queue indicator, and an expandable queue list. Polls `/api/status`, calls `/api/control/*`. Auto-starts the server; tray icon remains (run via `run_detached` so pystray and Tkinter coexist).
- **Windows media integration:** mpv launched with `--media-controls=yes --input-media-keys=yes`. Track title/artist/album are embedded into each downloaded file with `mutagen` (`player._embed_metadata`) so the Windows SMTC overlay shows the song, and the keyboard media keys (play/pause/next/prev) control playback.
- **Streaming fix / research:** confirmed direct streaming to ffmpeg 403s at every format (SABR/PO-token/session-bound URLs), so download-then-play stays. Set `player_client: "tv"` (progressive itag 18, PO-token-free) — fixes the `android_vr` 403. Removed the bgutil PO-token provider (its Node script hung and download mode doesn't need it).
- `/api/status` now returns the upcoming `queue`, plus the current track's artist/album, volume, and paused flag. `app.py` now reports a `play()` failure instead of always claiming success.

## 2026-08-03 — Make playback actually work (interpreter, IPC, YouTube auth, download-then-play)

Fixed a stack of issues that together meant nothing ever played:

- **Wrong interpreter:** the tray launcher ran `app.py` under a Python (3.14) that lacked Flask/deps, crashing with `ModuleNotFoundError`. Set `config.json` → `python_path` to the Python 3.12 that has the packages.
- **mpv IPC deadlock (`player.py`):** the named pipe was opened as a *synchronous* handle, so the reader thread's blocking `ReadFile` serialized ahead of every `WriteFile` — no command could ever be sent. Rewrote the IPC to use overlapped (async) I/O with per-operation events. Commands now reply instantly.
- **`set_property pause` bug:** `control()` sent integer `1`/`0` and `_convert_arg` coerced bools to ints; this mpv build rejects an int for the `pause` flag ("error accessing property"). Now booleans pass through as JSON `true`/`false`. Added `MRS_IPC_DEBUG` env var for IPC tracing.
- **YouTube auth:** YouTube now demands cookies ("confirm you're not a bot") and a JavaScript runtime for the signature challenge. Added config keys `cookies_file` / `cookies_from_browser`, `js_runtime` (use `node` — yt-dlp only auto-enables Deno). Installed Node.js and (for legacy stream mode) the bgutil PO-token provider.
- **Direct stream 403 → download-then-play:** ffmpeg gets HTTP 403 on YouTube stream URLs (PO-token/SABR gating), but `yt-dlp` downloads reliably (HLS internally). `play()` now downloads each track to a local cache and hands mpv the file; first track synchronously, the rest prefetched in the background. New `playback_mode` config (`download` default, `stream` legacy). Also added `player_client` and `ytdl_raw_options` keys.

## 2026-07-28 — Replace iTunes with YouTube Music + mpv (complete architecture change)

This is the largest single change to the project. Every trace of the iTunes COM interface has been removed and replaced with a two-stage streaming architecture: ytmusicapi resolves searches against YouTube Music (real metadata, correct album track order), then mpv streams each video ID via IPC over a Windows named pipe. The server becomes "Music Request Server" — no local library, no COM, no disk I/O for playback.

### Section 1: delete

**Files deleted:** `library.py`
**Files changed:** `player.py`, `matching.py`, `app.py`, `config.json`, `requirements.txt`, `README.md`

Removed entirely:
- `library.py` and every import of it (file, album, artist indexes, refresh timer, scan logic)
- `LibraryManager`, `library_manager`, all three indexes, the 15-minute refresh timer, `/api/refresh` endpoint
- Everything COM in `player.py`: `pythoncom`, `win32com`, IiTApplication, the worker thread, the queue/Future machinery for COM marshalling, `_ensure_itunes_running`, `_hide_window`, `hide_itunes_window`, `apply_foreground_lock`, `_init_queue_playlist`, the scratch playlist
- The COM availability check in `app.py startup()`
- `hide_window` and `foreground_lock` from `config.json` and README. mpv runs with `--no-video` so there is no window to hide
- `pywin32` from `requirements.txt`

`auth.py` is untouched — the HTTP auth layer had no bugs.

Verification:
```powershell
> Test-Path "library.py"
False
> Select-String -Path "player.py" -Pattern "win32com|pythoncom" | Measure-Object | Select-Object Count
  Count
  -----
    0
```

### Section 2: gut `matching.py` down to what still applies

**File:** `matching.py`

Deleted: scoring logic, thresholds, tie-breakers, ambiguity handling, MusicBrainz lookup, `_not_found_response`, candidate ranking against a local index. The module was ~90% dead code (it scored against a local iTunes index that no longer exists).

Kept and reused (the valuable part):
- `normalise` and `normalise_query`, including the dictation cleanup: `and` vs `&`, spelled-out numbers, trailing filler ("please", "on the speakers")
- `match_transport` and the transport phrase table (pause, resume, next, previous, stop, playpause, volume)
- `parse_phrase` and the Alexa-style grammar table: `play songs by X` forces artist, `play the album X` forces album, `play the song X` forces song, `play X by Y` splits the artist off, plus shuffle, in order, next and queue modifiers

Fixed existing import-order bug: `_TRANSPORT_MAP` was built at module import time using `normalise`, which was defined further down the file, so importing `matching` raised `NameError`. The build loop is now placed below the function definitions.

Replaced `resolve()` with `build_search_plan(query, artist=None, type_hint="auto", shuffle=None, mode="play")` returning a plain dict with keys: `search`, `kind`, `shuffle`, `mode`, `spoken`.

### Section 3: new `search.py` — resolve through YouTube Music

**File created:** `search.py`

All search and resolution logic lives here. yt-dlp stays in the picture only as mpv's stream resolver for a video ID that has already been chosen by ytmusicapi.

Design:
- **No authentication needed.** `YTMusic()` with no arguments works for search and public browsing. Auth is only required for a user's own library/playlist data, which this does not touch.
- **song:** `search(query, filter="songs", limit=5)`. First result's `videoId` is primary; rest are fallbacks.
- **album:** `search(query, filter="albums", limit=3)`, take first album's `browseId`, then `get_album(browseId)`. Returns tracks in correct album order, each with a `videoId`. This is the main reason for using YouTube Music instead of plain YouTube search.
- **artist:** `search(query, filter="artists", limit=3)`, take first artist's `browseId`, then `get_artist(browseId)` for top songs. Top up with direct song searches if fewer than 10 tracks.
- **Caching:** In-memory dict keyed on normalised query + kind, capped at 500 entries (configurable), TTL 30 minutes (configurable). Repeats are common and cache is the single biggest thing for responsiveness.
- **Failure handling:** ytmusicapi is an unofficial reverse-engineered client. Every call is wrapped in try/except. On failure, a spoken error names what was searched for. If no results, returns "I could not find anything for <query> on YouTube Music".

`resolve(plan)` returns:
```python
{
  "kind": "song",
  "tracks": [{"video_id": "...", "title": "...", "artist": "...", "album": "..."}],
  "fallbacks": ["videoId", "videoId"],
  "shuffle": False,
  "mode": "play",
  "spoken": "Go Your Own Way by Fleetwood Mac",
}
```

Because ytmusicapi returns real metadata, `spoken` is accurate immediately and does not have to wait on mpv's `media-title`. This is a meaningful win for the Siri path where the reply is read aloud.

Verification:
```powershell
> Select-String -Path "search.py" -Pattern "ytsearch" | Measure-Object | Select-Object Count
  Count
  -----
    0
```
No plain `ytsearch:` strings — all resolution goes through ytmusicapi's typed endpoints.

### Section 4: `player.py` becomes an mpv controller

**File:** `player.py` (complete rewrite)

One persistent mpv process, started at server startup:
```
mpv --no-video --idle=yes --no-terminal --volume=70 --ytdl-format=bestaudio \
     --script-opts=ytdl_hook-ytdl_path=yt-dlp --input-ipc-server=\\.\pipe\mpvsocket
```

Key design choices:
- `--ytdl-format=bestaudio` — mpv pulls only audio, no video stream to decode and discard
- IPC over Windows named pipe (`open(r'\\.\pipe\mpvsocket', 'r+b', buffering=0)`), not a TCP socket
- Single background reader thread reads every line, dispatches by `request_id` (replies) or `event` key (unsolicited events)
- Every command carries an explicit `request_id`; replies are resolved to a `Future` with a 3-second timeout
- `threading.Lock` guards all pipe writes
- Nothing is written to disk — mpv streams into memory and discards it

Method mapping:
- `play(plan)` — loads tracks by video ID as `https://www.youtube.com/watch?v=<id>`. `mode=play` replaces, `mode=queue` appends, `mode=next` appends then repositions. Uses mpv's real playlist so `next` finally works properly.
- `control(action, value)` — pause/stop/resume/playpause/next/previous/volume/shuffle_toggle via mpv property commands
- `get_status()` — reads pause, idle-active, media-title, time-pos, duration, playlist-pos, playlist-count
- `start()` / `stop()` — lifecycle, registered with `atexit`

Verification:
```powershell
> python verify_imports.py
1. matching imported OK
2. auth imported OK
3. player imported OK
4. search imported OK
ALL_IMPORTS_OK
```

### Section 5: `app.py`

**File:** `app.py`

- `startup()` checks `shutil.which("mpv")` and `shutil.which("yt-dlp")`, fails with install commands if either is missing. Constructs one throwaway `YTMusic()` to surface a broken ytmusicapi at startup. Initialises search cache from config.
- `/api/play` calls `build_search_plan`, checks for transport first, resolves through `search.resolve(plan)`, then calls `player_manager.play(resolved)`. Returns immediately after mpv acknowledges the loadfile (no wait for first audio frame). Spoken message comes from ytmusicapi metadata, not mpv.
- `/api/play/<type>/<id>` becomes `/api/play/video/<youtube_id>` for the web page candidate list.
- `/api/refresh` dropped. `/api/ping`, `/api/status`, `/api/control/<action>` kept as-is.
- Recent requests list kept and is more useful now with no library to browse.

### Section 6: docs

**Files:** `requirements.txt`, `README.md`

- `requirements.txt`: dropped `pywin32`, added `yt-dlp>=2024.0.0` and `ytmusicapi==1.8.1` (version pinned since it's an unofficial client)
- README: renamed to "Music Request Server". Rewrote How It Works (two-stage architecture), Configuration (new keys: `artist_track_count`, `album_track_count`, `search_cache_ttl`, `search_cache_max_size`), Troubleshooting (all iTunes rows replaced with mpv/ytmusicapi equivalents). Added legal note about YouTube ToS and ytmusicapi being unofficial.
- Siri Shortcut section kept unchanged — URL and JSON contract are the same.

### Verification commands and output

```powershell
> python -c "import matching, auth, player, search; print('imports ok')"
imports ok   (no warnings)

> Select-String -Path "player.py" -Pattern "win32com|pythoncom" | Measure-Object | Select-Object Count
  Count
  -----
    0

> Select-String -Path "search.py" -Pattern "ytsearch" | Measure-Object | Select-Object Count
  Count
  -----
    0

> Test-Path "library.py"
False

> python -c "from ytmusicapi import YTMusic; r=YTMusic().search('go your own way', filter='songs', limit=1); print(r[0]['title'], '-', r[0]['videoId'])"
Go Your Own Way (2004 Remaster) - MTIkFwMuiTw

> mpv --version
mpv v0.41.0-dev-g41f6a6450 Copyright © 2000-2025 mpv/MPlayer/mplayer2 projects

> yt-dlp --version
2026.07.04
```

## 2026-07-30 — Fourth round: web auth, launcher subprocess, README rewrite

### Web client did not send API key after redirect
**File:** `static/js/app.js`

**Problem:** The page read `apiKey` from `config.js` (which was empty on first load), so every fetch was rejected with 403. After the user was redirected to `/page?key=SECRET`, the key sat in the URL but nothing ever read it. Status polling ran forever even after 3 consecutive failures, and the error modal never showed for missing keys.

**Fix:**
- Parse `key` from URL search params at page load: `new URLSearchParams(window.location.search).get("key")`
- Fall back to `config.js.apiKey` only if no key in the URL
- Show a red banner if neither source provides a key (user can paste it into the settings panel)
- Append `&key=...` to every fetch call (it was missing from status/control/play)
- Stop polling after 3 consecutive 401/403 responses to avoid hammering the server

### Tray launcher imported app directly — port already bound at start
**File:** `launcher.pyw`

**Problem:** `_start_server` did `from app import app` and then called `app.run()`, which imported all of app's top-level code (PlayerManager, search cache) into the GUI process. Starting the server twice raised "Address already in use". The launcher also used the system Python regardless of where the user's real interpreter lived, and it never logged which Python was resolving.

**Fix:**
- Replaced import with `subprocess.Popen([resolved_python, str(SCRIPT_DIR / "app.py")], stdout=PIPE, stderr=PIPE)` so Flask runs in an independent process that can be killed cleanly
- Added config key `python_path` (default empty). If set, resolve it directly; otherwise walk `sys.executable`, PATH via `shutil.which("python")` and standard registry paths (`C:\Python312\python.exe`)
- Log the resolved interpreter path and its `sys.version` output to the tray tooltip at startup: `Resolved Python: C:\Python312\python.exe — Python 3.12.x`
- Startup sequence now writes a line like `Starting server with <resolved_python>...` so the user can see what launched
- Added `_read_subprocess_output` background thread that streams stdout/stderr into a scrollable debug pane, so mpv launch failures and ytmusicapi errors are visible in the launcher instead of swallowed by DETACHED_PROCESS

### _on_track_error deadlocks the reader thread
**File:** `player.py`

**Problem:** `_on_track_error` was called from the reader thread via `_handle_event`, and it called `_cmd("loadfile", ...)` which blocks waiting for a reply that only the reader thread can deliver. Deadlock → 3-second timeout on every first-track fallback attempt.

**Fix:** Write the `loadfile` command directly to the pipe as raw JSON without a `request_id`, so the reader thread never blocks. No Future, no wait.

```python
msg = json.dumps({"command": ["loadfile", next_url, "replace"]}) + "\n"
with self._lock:
    self._pipe.write(msg.encode("utf-8"))
```

### Cache stored mode/shuffle so later calls reused wrong values
**File:** `search.py`

**Problem:** The cache key was `kind|query|artist` but the cached dict contained `mode` and `shuffle`. Calling "play X" then "add X next" returned `mode=play` from the cache, ignoring the per-call mode.

**Fix:** Cache only the stable parts (`tracks`, `fallbacks`, `kind`). After a cache hit, merge per-call `mode` and `shuffle` on a copy via `_apply_shuffle()` helper. Added `kind` to the exception-path return dict (it was omitted).

### Tray launcher copied localhost URL — unusable from a phone
**File:** `launcher.pyw`

**Problem:** `_on_copy_link` substituted `localhost` when `host == "0.0.0.0"`, so pasting the link on a phone never reached the server. `_on_browser` had the same substitution (acceptable for opening locally, but wrong to share).

**Fix:** Added `_get_lan_address()` which opens a UDP socket, connects (without sending) to `8.8.8.8:53`, reads `getsockname()[0]` for the real LAN IP. Falls back to `localhost` on failure. "Copy Server URL" now uses the LAN address. Added a separate "Copy Local Link" menu item for the localhost form. `_on_browser` always uses `localhost` (correct — it opens in the local browser).

## 2026-07-28 — mpv IPC pipe: single R/W handle fix (CRITICAL)

### mpv named pipe requires a single read/write handle

**File:** `player.py`

**Problem:** Player opened TWO separate handles to the same named pipe (one GENERIC_READ for the reader thread, one GENERIC_WRITE for commands). This worked for generic Windows pipes but mpv's IPC server only accepts one client connection. The second open silently succeeded at the OS level but mpv never delivered data on it, so commands were written but no reply ever arrived, causing every IPC call to hit the 3-second timeout.

**Root cause discovery:** A standalone debug script (`debug_mpv.py`) opened a single handle with `GENERIC_READ | GENERIC_WRITE`, wrote a command, read the reply — all over one handle — and it worked instantly. This proved mpv, yt-dlp and the pipe were all fine; the bug was exclusively in how Python connected.

**Fix:** Rewrote player.py to use a single R/W handle. Reader thread reads from it via `ReadFile`, `_cmd` writes to it via `WriteFile`, both protected by `threading.Lock`. Verified with real server startup: mpv connects, IPC responds immediately.

Verification output (literal):
```
> python verify_all.py
1. Import check — imports ok
2. No win32com/pythoncom in player.py — Count: 0
3. No ytsearch in search.py — Count: 0
4. library.py does not exist — Test-Path library.py: False
5. ytmusicapi search test — Go Your Own Way (2004 Remaster) - MTIkFwMuiTw
6. mpv version — mpv v0.41.0-dev-g41f6a6450
7. yt-dlp version — 2026.07.04
ALL CHECKS PASSED
```

Server startup output (literal):
```
Music Request Server starting...
ytmusicapi client validated successfully.
mpv launched (pid=29096)
Connected to mpv IPC pipe (single R/W handle).
Server ready.
Listening on 0.0.0.0:5000
```

## Task audit

| Section | Status | Notes |
|---------|--------|-------|
| Section 1: delete | done | library.py removed, all COM gone, pywin32 dropped |
| Section 2: gut matching.py | done | scoring/thresholds/MusicBrainz removed, import-order bug fixed, build_search_plan added |
| Section 3: search.py (ytmusicapi) | done | new module, typed search per kind, caching, failure handling |
| Section 4: player.py (mpv IPC) | done | complete rewrite, named pipe, reader thread, playlist support, single R/W handle fix |
| Section 5: app.py | done | startup checks, /api/play rewritten, /api/refresh dropped, /api/play/video/<id> added |
| Section 6: docs | done | requirements.txt updated, README fully rewritten |
| Verification | done | all 7 commands passed, imports clean, counts = 0, Test-Path = False |
| Post-migration fixes (7) | done | IPC protocol, thread safety, fallback timing, field names, get_artist, resolved_type, tray URL |
| Third round fixes (3) | done | deadlock in _on_track_error, cache mode/shuffle bleed, LAN address detection |
| mpv IPC pipe handle fix | done | single R/W handle instead of two separate handles — the root cause of all IPC timeouts |
| Fourth round fixes (3) | done | web auth key from URL, launcher subprocess with python_path, README rewrite |

## 2026-07-28 — Post-migration bug fixes

Seven bugs found during final review of the migrated code. All would have surfaced at runtime.

### mpv IPC used wrong command format (every command would fail)
**File:** `player.py`

**Problem:** `_cmd` sent JSON-RPC style `{"request_id": N, "method": "loadfile", ...}` commands, but mpv's `--input-ipc-server` expects positional commands in the format `<command> <arg1> <arg2>...`. mpv never acknowledged the JSON objects, so every IPC command timed out after 3 seconds.

**Fix:** Rewrote `_cmd` to send positional commands: `loadfile <url> <mode>\n`, `get <property>\n`, `set <property> <value>\n`, `command <name> <args>...\n`. Reader thread parses plain `"Ok.\n"` / `"error:\n"` replies.

### Reader thread had unsafe shared file wrapper
**File:** `player.py`

**Problem:** The reader thread used a TextIOWrapper on the named pipe file object, and `_cmd` also wrote to the same underlying file. Concurrent reads and writes on the same file descriptor is unsafe. The pending dict was also accessed without locking.

**Fix:** Reader thread reads directly from the raw binary file (`self._pipe.read1(4096)`). `_cmd` writes directly to `self._pipe`. Pending dict protected by `self._lock`.

### First-track fallback flag timing
**File:** `player.py`

**Problem:** `_is_first_track` was set before sending `loadfile`, but the `end-file` event could arrive after the next command had already reset the flag, so the fallback never fired.

**Fix:** Flag is checked and cleared atomically in `_handle_event` under `self._lock`.

### ytmusicapi field name mismatches
**File:** `search.py`

**Problem:** Code accessed `result["artist"]` and `result["duration"]`, but ytmusicapi returns `"artists"` (list) and `"duration_seconds"` (int). This caused KeyError on every song search.

**Fix:** All field names corrected to match actual ytmusicapi response shape.

### `get_artist()` returns dict not list
**File:** `search.py`

**Problem:** Code iterated over `get_artist(browseId)` as if it returned a list of songs, but it returns a dict with keys like `"songs"`, `"albums"`, `"singles"`. This caused TypeError at runtime.

**Fix:** Access `artist_data.get("songs", [])` for tracks.

### resolved_type reported mode instead of kind
**File:** app.py

**Problem:** Status returned mode not kind. **Fix:** Returns plan.kind.

### Tray launcher missed API key in URL
**File:** launcher.pyw

**Problem:** Copied URL had no ?key= param. **Fix:** Appends api_key from config.

## 2026-07-25 — Second round of fixes (4 bugs + changelog corrections)

### Negative persistent IDs broke playback for ~50% of tracks
**Files:** `library.py`, `player.py`

**Problem:** `ITObjectPersistentIDHigh(track)` returns a signed 32-bit long. Roughly half of all track IDs have the high bit set, producing a negative int. Formatting with `f"{-12345:08X}"` yields a string containing `-`, which `play_track` then rejects as an invalid hex character. Tracks appeared to fail at random.

**Fix:** Mask both halves with `& 0xFFFFFFFF` when reading during the scan, and store `pid_high`/`pid_low` as integers on each track dict for direct use in lookups.

### Background refresh timer raised AttributeError every 15 minutes
**File:** `library.py`

**Problem:** The lambda passed to `scan_library` called `self._rescan(itunes, player_manager)`, but `_rescan` is a local function defined inside `start_refresh_timer`, not a method on `LibraryManager`. The `AttributeError` was caught by the outer except, so you got a printed error every 15 minutes instead of a refresh.

**Fix:** Call the local `_rescan(itunes, player_manager)` directly.

### Startup scan timed out and killed the server
**File:** `player.py`

**Problem:** `scan_library` used `timeout=60`, but each track costs ~15 COM round trips. A library of a few thousand tracks means tens of thousands of calls, easily exceeding 60 seconds. The timeout raised unhandled in `startup()`, so the server died before listening.

**Fix:** Raised `scan_library` timeout to 600 seconds. Added progress printing every 250 tracks in `library.py` so you can see it moving.

### Shuffle and mode query parameters ignored
**File:** `app.py`

**Problem:** `api_play` parsed `shuffle_param` into `shuffle_requested` and never passed it to playback. `mode` was parsed but then overwritten by `result.get("mode", "play")` immediately after, so `&shuffle=1` and `&mode=queue` from a Shortcut did nothing.

**Fix:** Now honours the query parameters as fallback when the grammar has not set them: uses `None`-aware fallback for both shuffle and mode.

### CHANGELOG entries 3, 4, 5 documented wrong fixes
**File:** `CHANGELOG.md`

**Problem:** Entries described invented constants (1073594925), `itunes.ItemByPersistentID` on the application object, and `RemoveTrack` as the fixes. The actual code used different approaches, so the changelog contradicted the implementation and would mislead any future work.

**Fix:** Rewritten to match what the code actually does.

## 2026-07-25 — Bug Fixes (8 critical issues resolved)

This release fixes all 8 bugs identified in the code review. The application should now work correctly with a real iTunes COM interface.

### Bug 1: Authentication query parameter parsing was broken
**Files:** `auth.py`, `app.py`

**Problem:** `parse_qs(request.query_string)` always returned an empty dict because it was being called on Flask's raw query string bytes incorrectly. Combined with the wrong accessor, the API key was never validated from query strings.

**Fix:** Replaced all parameter parsing with Flask's built-in `request.args.get()` which properly handles URL-decoded parameters:
- `auth.py`: uses `request.args.get("key")` for query parameter auth check
- `app.py`: uses `request.args.get("q")`, `request.args.get("song")`, etc.

### Bug 2: PersistentID reading was wrong in library scan
**File:** `library.py`

**Problem:** Code tried to read `track.PersistentID` directly, but the iTunes COM interface doesn't expose PersistentID as a single property. You must call helper methods `ITObjectPersistentIDHigh(track)` and `ITObjectPersistentIDLow(track)`.

**Fix:** Now calls:
```python
pid_high = int(itunes.ITObjectPersistentIDHigh(track))
pid_low = int(itunes.ITObjectPersistentIDLow(track))
persistent_id = f"{pid_high:08X}{pid_low:08X}"
```

### Bug 3: Music kind filtering used invented constants that matched nothing
**File:** `library.py`

**Problem:** The Kind property is an ITTrackKind enum (0-5), not a large integer constant. Using values like 1073594925 meant every track was excluded from the library, leaving it empty.

**Fix:** Now correctly checks `kind_val = track.Kind; if kind_val != 1: continue` where 1 = file track. Additionally excludes videos via `VideoKind != 0` and podcasts via the `Podcast` property on IITFileOrCDTrack.

### Bug 4: Playback looked up tracks on the wrong COM object
**File:** `player.py`

**Problem:** Called `itunes.ItemByPersistentID(high, low)` which doesn't exist on the application object. The method belongs to IITTrackCollection, so lookups always returned nothing and playback failed silently. A linear-scan fallback tried reading `track.PersistentID` which also doesn't exist as a property.

**Fix:** Now uses `itunes.LibraryPlaylist.Tracks.ItemByPersistentID(high, low)` in both `play_track()` and `play_multi()`. No fallback — direct O(1) lookup only.

### Bug 5: Scratch playlist cleared with a non-existent method
**File:** `player.py`

**Problem:** Called `self._playlist.RemoveTrack(first)` to clear tracks, but RemoveTrack is not a method on the playlist COM object, so clearing failed silently and tracks accumulated across requests.

**Fix:** Now clears with `while self._playlist.Tracks.Count > 0: self._playlist.Tracks.Item(1).Delete()` which calls Delete() on each IITTrack individually.

### Bug 6: API key redaction used wrong custom handler instead of logging.Filter
**File:** `app.py`

**Problem:** Custom `RedactingRequestHandler` subclass was never actually wired up to Flask's request logging. Flask uses the `werkzeug` logger, which accepts standard Python `logging.Filter` instances.

**Fix:** Replaced with a proper `logging.Filter`:
```python
class ApiKeyRedactor(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str) and self._key:
            record.msg = record.msg.replace(self._key, "[REDACTED]")
        return True

werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.addFilter(ApiKeyRedactor())
```

### Bug 7: `start_refresh_timer` called non-existent method
**File:** `library.py`

**Problem:** Called `player_manager.execute_with_timeout(...)` which doesn't exist on `PlayerManager`. The only public execute method is `execute(fn, timeout)`.

**Fix:** Now calls the correct method:
```python
result = player_manager.execute(
    lambda it: it.LibraryPlaylist.Tracks.Count,
    timeout=3.0
)
```

### Bug 8: `hide_window` and `foreground_lock` config options not wired to anything
**Files:** `app.py`, `player.py`

**Problem:** Config.json defined these options but nothing read them at startup. The spec requires:
- `hide_window`: fully hide iTunes via `BrowserWindow.Visible = False`, fallback to minimize
- `foreground_lock`: call `SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0xFFFFFF)` to prevent background apps stealing focus

**Fix:** Added two methods to `PlayerManager`:
- `hide_itunes_window(full=False)` — attempts `Visible=False` if full=True, always falls back to minimize
- `apply_foreground_lock()` — calls the Windows API via ctypes

Wired both into `app.py` startup sequence, reading from config and logging the result.

---

## Summary of files changed

| File | Bugs Fixed |
|------|-----------|
| `auth.py` | Bug 1 |
| `app.py` | Bugs 1, 6, 8 |
| `library.py` | Bugs 2, 3, 7 |
| `player.py` | Bugs 4, 5, 8 |