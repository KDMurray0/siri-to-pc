# Music Request Server

A local network music playback server that streams from YouTube Music through HTTP API, Siri voice commands, Shortcuts, and a web interface. No local music library required — every request is resolved by searching YouTube Music with `ytmusicapi` and played by `mpv`.

## Quick Start

```bash
pip install -r requirements.txt
copy config.example.json src\config.json
```

Edit `src\config.json` (set `api_key`, `python_path`, and `cookies_file`), then just **double-click `launcher.pyw`** — it starts the server and opens the player. Or run the server directly:

```bash
python src/app.py
```

The player is a desktop flyout (from the tray). To control it from your phone, open `http://<pc-ip>:5000/` on the same network for step-by-step **Siri Shortcut** setup.

## Prerequisites

- **mpv** media player (audio-only mode): `winget install mpv`
- **yt-dlp** (audio fetcher): `pip install -U yt-dlp`
- **Node.js** — required so yt-dlp can solve YouTube's JavaScript signature challenge (otherwise no audio formats are returned): `winget install OpenJS.NodeJS`
- Python 3.10+ with packages from `requirements.txt`
- A **YouTube cookies file** (`youtube_cookies.txt`) from a logged-in session — YouTube now blocks unauthenticated requests with a "confirm you're not a bot" error. See [YouTube Authentication](#youtube-authentication) below.

> **Important — use the same Python for everything.** All packages must be installed into the interpreter set in `config.json` → `python_path`. On this machine that is Python 3.12 (`C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`). The tray launcher itself may run under a different Python, so it launches `app.py` with `python_path` explicitly to avoid `ModuleNotFoundError`.

## Configuration

Edit `config.json` before first run:

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "api_key": "CHANGE-THIS-TO-A-RANDOM-SECRET",
  "python_path": "",
  "allowed_ips": [],
  "artist_track_count": 20,
  "album_track_count": 0,
  "search_cache_ttl": 1800,
  "search_cache_max_size": 500
}
```

| Field | Description |
|-------|-------------|
| `host` | Bind address. Use `0.0.0.0` for network access, `127.0.0.1` for local only |
| `port` | TCP port the server listens on |
| `api_key` | Secret key for authentication. **Change this from the default** |
| `python_path` | Explicit path to the Python interpreter that has all dependencies installed (e.g. `C:\...\Python312\python.exe`). The tray launcher runs `app.py` with this. **Must not be empty if the launcher's own Python lacks the packages.** |
| `cookies_file` | Path to a Netscape-format `youtube_cookies.txt` exported from a logged-in YouTube session. Required for playback. If the file is missing it is ignored (with a warning). |
| `cookies_from_browser` | Alternative to `cookies_file`: a browser name yt-dlp reads live cookies from, e.g. `firefox`. **Chrome/Edge do not work on Windows** (App-Bound Encryption). Leave empty if using `cookies_file`. |
| `js_runtime` | JavaScript runtime yt-dlp uses to solve the signature challenge. Set to `node` (yt-dlp only auto-enables Deno otherwise). Required for audio to resolve. |
| `playback_mode` | `download` (default, recommended) downloads each track then plays the local file — robust against YouTube's stream 403s. `stream` hands URLs to mpv's ytdl hook (legacy; may 403). |
| `player_client` | YouTube player client for yt-dlp. **Default `tv`** — it returns a progressive stream (itag 18) that downloads without a PO token. Leaving it empty lets yt-dlp pick `android_vr`, whose audio format currently 403s. |
| `auto_queue` | `true` (default) keeps playing forever, Spotify-style: when the queue is nearly empty it appends songs seeded from the recent listening *context* (several songs you didn't skip), ranked toward your taste. |
| `auto_queue_batch` | How many related songs to append per refill (default 5). |
| `auto_queue_threshold` | Refill when this many tracks remain (default 2 — two from the end). |
| `history_size` | How many recent plays to remember and keep out of the queue, for smart-shuffle no-repeats (default 100). |
| `queue_liked_boost` / `queue_playthrough` / `queue_skip_penalty` / `queue_song_play` / `queue_jitter` / `queue_liked_seed_prob` / `queue_context_songs` | Smart-shuffle weights — how hard the queue biases toward liked and played-through songs vs. discovery. |
| `ytdl_raw_options` | Optional list of extra yt-dlp options as `"key=value"` strings (advanced). |
| `allowed_ips` | List of allowed client IPs. Empty `[]` allows all (for testing) |
| `artist_track_count` | Number of tracks to play when requesting an artist (default: 20) |
| `album_track_count` | Max tracks for album playback. `0` means unlimited (entire album, default: 0) |
| `search_cache_ttl` | Search cache time-to-live in seconds (default: 1800 = 30 min) |
| `search_cache_max_size` | Max entries in the search cache (default: 500) |

## YouTube Authentication

As of 2026, YouTube blocks unauthenticated `yt-dlp` requests with **"Sign in to confirm you're not a bot."** Playback therefore requires cookies from a logged-in YouTube session, plus Node.js to solve the signature challenge.

### Export a cookies file (using your existing Chrome login)

1. In your browser, install the extension **"Get cookies.txt LOCALLY"** (exports locally; nothing is uploaded).
2. Open an **Incognito/Private window**, go to **youtube.com**, and confirm you're logged in. *(Incognito cookies aren't rotated by normal browsing, so they last much longer.)*
3. Click the extension → **Export** (Netscape format) → save the file as:
   ```
   <project folder>\youtube_cookies.txt
   ```
4. Point `config.json` → `cookies_file` at that path (already set by default).

> **Chrome/Edge live extraction (`cookies_from_browser`) does not work on Windows** — Chrome v127+ encrypts cookies with App-Bound Encryption that yt-dlp cannot decrypt ("Failed to decrypt with DPAPI"). Use the exported `cookies_file`, or install **Firefox**, log into YouTube there, and set `cookies_from_browser: firefox`.

> **Account note:** yt-dlp uses your real Google session; there is a small risk YouTube flags the account. Consider a throwaway Google account. Cookies also expire — if playback starts failing with the bot error again, re-export the file.

> **Rate limits:** many rapid requests from one IP can temporarily trigger the bot check even with valid cookies. Normal use (a handful of songs) is fine; it clears on its own after a while.

## How It Works

The server uses a two-stage architecture:

1. **Search stage:** When a request arrives, the spoken phrase is cleaned and parsed (dictation cleanup, grammar matching, transport detection). The query is sent to YouTube Music via `ytmusicapi`, which returns real music metadata (titles, artists, albums, video IDs). Album track listings come in correct order from YouTube Music's browse endpoints.

2. **Playback stage (download-then-play):** For each resolved video ID, the server runs `yt-dlp` (with your cookies and Node) to download the audio to a local cache file, then hands that file to a persistent `mpv` process over a Windows named-pipe IPC connection. The first track downloads before playback starts (~1–2 s); the rest are prefetched in the background. Downloading avoids the HTTP 403 that `mpv`/`ffmpeg` hit when fetching YouTube stream URLs directly.

This separation means search results have accurate metadata immediately, and playback is handled by a mature media player rather than COM automation. (A legacy direct-`stream` mode still exists via `playback_mode`, but YouTube may 403 it.)

**Note:** Streaming audio from YouTube Music is outside YouTube's terms of service for playback. This server is intended for personal use only. Also, `ytmusicapi` is an unofficial client against a reverse-engineered API and may need updating when YouTube changes its internals.

## API Endpoints

All endpoints return JSON and require authentication via `key` query parameter or `X-Api-Key` header.

### Play a request
```
GET /api/play?key=SECRET&q=Yellow+by+Coldplay&type=auto&shuffle=0&mode=play
```

Parameters:
- `q` or `song`: The search phrase (required)
- `artist`: Optional artist filter
- `type`: `auto`, `song`, `album`, or `artist` (default: `auto`)
- `shuffle`: `0` or `1` (default depends on type)
- `mode`: `play`, `next`, or `queue` (default: `play`)

### Play a specific video
```
GET /api/play/video/YOUTUBE_VIDEO_ID?key=SECRET
```

### Status
```
GET /api/status?key=SECRET
```

Returns current track, player state, playlist position, and recent requests.

### Transport controls
```
GET /api/control/pause?key=SECRET
GET /api/control/next?key=SECRET
GET /api/control/previous?key=SECRET
GET /api/control/volume?value=75&key=SECRET
GET /api/control/shuffle_toggle?key=SECRET
```

### Health check
```
GET /api/ping?key=SECRET
```

Returns `{"status": "ok"}` instantly.

## Windows Firewall

Allow the server port on **private networks only**:

```powershell
New-NetFirewallRule -DisplayName "Music Request Server" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow -Profile Private -RemoteAddress 192.168.1.0/24
```

**Never** allow on Public profile. Replace `192.168.1.0/24` with your subnet or your phone's specific IP.

For maximum security, scope to just your phone:
```powershell
New-NetFirewallRule -DisplayName "Music Request Server" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow -Profile Private -RemoteAddress 192.168.1.50
```

## Network Setup

### Static DHCP lease

Assign a static IP to your PC in your router's DHCP settings. This ensures the server is always at the same address.

Also assign a static IP to your phone so the `allowed_ips` list stays valid.

### Auto-start at logon

Create a scheduled task that runs at logon:

```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "C:\path\to\music_request_server\app.py"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "Music Request Server" -Action $action -Trigger $trigger -User "$env:USERNAME" -RunLevel Limited
```

The server will start when you log in and run headless (no console window). mpv runs with `--no-video` so there is no player window to hide.

### System tray launcher + music bar (`launcher.pyw`)

Double-click `launcher.pyw` to launch everything. It:

- **Auto-starts the server** as a subprocess using the interpreter in config `python_path`.
- Opens a compact, **borderless, rounded, always-on-top "music bar"** (built with customtkinter) that behaves like a Windows flyout: current song and artist, a live progress bar, ⏮ / play-pause / ⏭, a volume slider, an **Auto-queue** switch, and an expandable **Queue** list. It polls `/api/status` and drives the `/api/control/*` endpoints. **Click anywhere outside it and it hides.**
- Adds a **system-tray icon** — click it (or "Show Player" in the menu) to pop the bar back up near the tray. The menu also has Start / Stop / Quit and options to open the web page or copy the server URL / API key.

> The launcher runs under whatever Python opens `.pyw` files (often a different install from `python_path`). That interpreter needs `customtkinter`, `pystray`, and `Pillow`: `pip install customtkinter pystray Pillow`.

## Auto-Queue (endless play)

With `auto_queue` on (default), playback never stops: when the queue runs low the server asks YouTube Music for a *radio* seeded by the current track and appends related songs — the same idea as Spotify's autoplay. Tracks are downloaded ahead of time so there's no gap between songs. Toggle it live from the music bar, or with:

```
GET /api/autoqueue?key=SECRET            # toggle
GET /api/autoqueue?key=SECRET&enabled=0  # off
```

## Windows Media Integration

mpv is launched with `--media-controls=yes`, so the player registers with the **Windows System Media Transport Controls**:

- The **volume/media flyout, lock screen, and "now playing"** show the current song and artist (embedded into each downloaded file's tags).
- The keyboard/hardware **media keys work**: Play/Pause toggles playback, and Next/Previous move through the queue — even with no window focused.

## A note on streaming vs. downloading

YouTube's 2026 anti-bot stack (SABR, PO tokens, session-bound URLs) means a stream URL that `yt-dlp` resolves will typically return **HTTP 403 when `mpv`/`ffmpeg` tries to fetch it directly** — regardless of format or client. `yt-dlp` itself downloads reliably, so this server **downloads each track, then plays the local file** (`playback_mode: download`). It's the consistent path; the first track takes a few seconds, and everything after is prefetched. The legacy `stream` mode is kept for completeness but is expected to 403.

## Important: Sleep and Lock

- **Locking Windows** (Win+L) does NOT stop the server or interrupt playback. Music keeps playing.
- **Sleep/hibernate** WILL stop playback and make the server unreachable.
- If you lock your PC, music continues. If the PC sleeps, it doesn't.
- Disable sleep mode when using this as a voice-controlled music player, or set a long timeout.

## Siri Shortcut Setup

### Basic "Say what to play" shortcut

1. Open the Shortcuts app on iPhone
2. Tap **+** to create a new shortcut
3. Name it something Siri hears clearly, e.g., "Play Music" or "Request Song"
4. Add **"Dictate Text"** action
5. Add **"URL Encode"** action (input: Dictated Text)
6. Add **"Get Contents of URL"** action with this URL:
   ```
   http://192.168.1.XXX:5000/api/play?key=YOUR_SECRET&q=[URL_ENCODED_TEXT]&auto=1
   ```
   Replace `192.168.1.XXX` with your PC's IP and `YOUR_SECRET` with your API key.
7. Add **"Get Dictionary Item"** action (input: URL response, key: `message`)
8. Add **"Speak Text"** action (input: the message value)
9. Optionally add **"Show Notification"** for visual feedback

### Pre-named shortcut (one phrase to Siri)

Create a shortcut named "Play Some Fleetwood Mac":

1. Name the Shortcut exactly what you'll say to Siri
2. Add **"Get Contents of URL"** with a hardcoded URL:
   ```
   http://192.168.1.XXX:5000/api/play?key=YOUR_SECRET&q=Fleetwood+Mac&auto=1&type=artist
   ```
3. Add **"Get Dictionary Item"** for `message`
4. Add **"Speak Text"** or **"Show Notification"**

Now "Hey Siri, Play Some Fleetwood Mac" works as one utterance.

### Notes about Shortcuts

- Shortcuts **can** call plain `http://` URLs on the LAN. No HTTPS needed at home.
- If Siri reports a failure, check that your PC is awake (not sleeping/locked out).
- The Shortcut blocks while waiting for a response. Keep API calls fast (< 1 second).

## Voice Query Examples

Say any of these to Siri (through a Shortcut):

| What you say | Resolves to |
|---|---|
| "Play songs by Fleetwood Mac" | Artist: Fleetwood Mac, shuffled |
| "Play the album Rumours" | Album: Rumours, in order |
| "Play the song Go Your Own Way" | Song: Go Your Own Way |
| "Play Yellow by Coldplay" | Auto-detects (usually song) |
| "Play some Muse" | Artist: Muse, shuffled |
| "Shuffle Coldplay" | Artist: Coldplay, shuffle on |
| "Add Daft Punk next" | Album/artist: Daft Punk, mode=next |
| "Play some jazz" | Genre: a curated jazz station |
| "Play 80s music" | Decade: an 80s mix |
| "Gimme some workout music" | Mood: an energetic workout mix |
| "Put on some Fleetwood Mac" | Artist: Fleetwood Mac (casual phrasing) |

## Project Structure

```
itunes_request_server/
  launcher.pyw          Double-click entry: tray icon + webview player, starts the server
  config.example.json   Copy to src/config.json and edit
  requirements.txt      Python dependencies
  README.md / CHANGELOG.md
  src/
    app.py              Flask routes + startup (run: python src/app.py)
    auth.py             API key + IP allowlist checks
    matching.py         Query parsing, grammar, natural-language commands
    search.py           YouTube Music search + smart interpretation (ytmusicapi)
    player.py           Dual-mpv playback: download-then-play, crossfade, smart-shuffle
    config.json         Your settings (gitignored)
    templates/
      player.html       The desktop player UI
      setup.html        Siri-Shortcut setup instructions (served at /)
```

## Testing

Run the test suite (no mpv or YouTube connection required):

```bash
cd tests
python test_matching.py
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| "mpv not found on PATH" | mpv is not installed or not on system PATH | Run `winget install mpv` and restart terminal |
| "yt-dlp not found on PATH" | yt-dlp is not installed | Run `pip install yt-dlp` |
| `ModuleNotFoundError: No module named 'flask'` at launch | Launcher ran `app.py` under a Python that lacks the deps | Set `config.json` → `python_path` to the interpreter where you `pip install`ed everything |
| "Sign in to confirm you're not a bot" / nothing plays | No/expired cookies, or IP rate-limited | Export a fresh `youtube_cookies.txt` (see [YouTube Authentication](#youtube-authentication)); if it was working, wait for the rate limit to clear |
| Track starts then instantly stops / "only images available" | Node not installed or `js_runtime` not set | Install Node.js and set `config.json` → `js_runtime: "node"` |
| Plays via `yt-dlp` but mpv gives HTTP 403 | `playback_mode: stream` hitting YouTube's PO-token/SABR gate | Use `playback_mode: "download"` (default) |
| "ytmusicapi validation failed" | YouTube changed its internal API; ytmusicapi is out of date | Run `pip install --upgrade ytmusicapi` and check the package's GitHub for notes |
| No search results | Query too vague or artist name misspelled by dictation | Try a more specific phrase, e.g. "the song Yellow by Coldplay" |
| Region-locked or premium-only track | The chosen video is unavailable in your region | Check the spoken error message; the server tries alternate results automatically for song requests |
| No audio but mpv is running | Volume too low, wrong output device, or mpv audio backend issue | Check system volume mixer. Try `GET /api/control/volume?value=75` |
| "mpv IPC pipe did not appear" | mpv failed to start or the named pipe path is wrong | Run `mpv --version` manually. Check for conflicting pipe names. |
| Siri gets no response | PC is sleeping | Disable sleep mode or wake the PC first |
| Playback starts but track name is wrong | Wrong search result chosen | The server takes the top ytmusicapi result filtered to the requested artist. Be more specific in your query. |

## Legal Note

This server streams audio from YouTube Music, which is outside YouTube's terms of service for playback. It is intended for personal use only. `ytmusicapi` is an unofficial client that communicates with reverse-engineered YouTube Music endpoints and may need updating when YouTube changes its internals.