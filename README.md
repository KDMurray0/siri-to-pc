# Music Request Server

A local network music playback server that streams from YouTube Music through HTTP API, Siri voice commands, Shortcuts, and a web interface. No local music library required — every request is resolved by searching YouTube Music with `ytmusicapi` and played by `mpv`.

## Quick Start

### Option 1 — download the release (no Python needed)

1. Grab the latest `MusicRequestServer-windows.zip` from the
   [Releases page](https://github.com/KDMurray0/siri-to-pc/releases) and unzip it anywhere.
2. Right-click `setup.ps1` → **Run with PowerShell** (or from a terminal):

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   It installs mpv, yt-dlp and Node.js via winget, then sets up your YouTube
   cookies and verifies a real download works.
3. Run `MusicRequestServer.exe`. It lives in the system tray.

### Option 2 — run from source

```powershell
git clone https://github.com/KDMurray0/siri-to-pc
cd siri-to-pc
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` also installs the Python packages and writes `src\config.json` for
you. Then double-click **`launcher.pyw`** (server + tray player), or run the
server on its own:

```bash
python src/app.py
```

If `api_key` is blank or missing, a random secret is generated on first run.
Open `http://<pc-ip>:5000/` to see the endpoint URL (with the key) and the Siri
Shortcut steps.

### What setup.ps1 does

| Step | Detail |
|------|--------|
| Prerequisites | Installs `mpv`, `yt-dlp`, `Node.js` via winget; skips anything already present |
| Python packages | `pip install -r requirements.txt` (source runs only — the .exe bundles them) |
| Config | Creates `src\config.json` from the example, sets `python_path`, `js_runtime`, `player_client` |
| Cookies | Tries `--cookies-from-browser` against each installed browser, falls back to a cookies file |
| Verify | Runs a real YouTube fetch and reports exactly what failed if anything did |

Useful flags: `-SkipCookies` (tools only), `-CookieBrowser firefox` (skip auto-detection).

### Build the .exe yourself

```bash
pip install pyinstaller
pyinstaller --noconfirm MusicRequestServer.spec
```

The build lands in `dist\MusicRequestServer\`. mpv, yt-dlp and Node still need
to be on PATH — run `setup.ps1` on the target machine to handle that.

## Prerequisites

> `setup.ps1` installs all of these for you — this list is what it does, and what
> to install by hand if you would rather.

- **mpv** media player (audio-only mode): `winget install --id shinchiro.mpv -e`
- **yt-dlp** (audio fetcher): `winget install --id yt-dlp.yt-dlp -e`
- **Node.js** — required so yt-dlp can solve YouTube's JavaScript signature challenge (otherwise no audio formats are returned): `winget install --id OpenJS.NodeJS.LTS -e`
- Python 3.10+ with packages from `requirements.txt` *(source runs only — the .exe bundles them)*
- A **logged-in YouTube session**, either read live from your browser or exported to a cookies file — YouTube blocks unauthenticated requests with a "confirm you're not a bot" error. See [YouTube Authentication](#youtube-authentication) below.

> **Important — use the same Python for everything.** All packages must be installed into the interpreter set in `config.json` → `python_path`. On this machine that is Python 3.12 (`C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`). The tray launcher itself may run under a different Python, so it launches `app.py` with `python_path` explicitly to avoid `ModuleNotFoundError`.

## Configuration

Edit `config.json` before first run:

```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "api_key": "",
  "python_path": "",
  "cookies_file": "C:\\path\\to\\youtube_cookies.txt",
  "js_runtime": "node",
  "player_client": "tv",
  "allowed_ips": [],
  "lock_ips": false,
  "announce": true,
  "tts_voice": "en-US-AriaNeural",
  "use_groq": false,
  "groq_api_key": "",
  "auto_queue": true
}
```

See `config.example.json` for the full list. `api_key` blank ⇒ auto-generated on first run.

| Field | Description |
|-------|-------------|
| `host` | Bind address. Use `0.0.0.0` for network access, `127.0.0.1` for local only |
| `port` | TCP port the server listens on |
| `api_key` | Secret key for authentication. **Change this from the default** |
| `python_path` | Explicit path to the Python interpreter that has all dependencies installed (e.g. `C:\...\Python312\python.exe`). The tray launcher runs `app.py` with this. **Must not be empty if the launcher's own Python lacks the packages.** |
| `cookies_file` | Path to a Netscape-format `youtube_cookies.txt` exported from a logged-in YouTube session. Required for playback. If the file is missing it is ignored (with a warning). |
| `cookies_from_browser` | Alternative to `cookies_file`: a browser name yt-dlp reads live cookies from, e.g. `firefox`. **Chrome/Edge do not work on Windows** (App-Bound Encryption). Leave empty if using `cookies_file`. |
| `js_runtime` | JavaScript runtime yt-dlp uses to solve the signature challenge. Set to `node` (yt-dlp only auto-enables Deno otherwise). Required for audio to resolve. |
| `player_client` | YouTube player client for yt-dlp. **Default `tv`** — it returns a progressive stream (itag 18) that downloads without a PO token. Leaving it empty lets yt-dlp pick `android_vr`, whose audio format currently 403s. |
| `use_groq` / `groq_api_key` / `groq_model` | Optional. With a free [Groq](https://console.groq.com) key, requests are parsed by an LLM (far better at casual phrasing than the regex grammar). Empty key = local parser. Model defaults to `llama-3.3-70b-versatile`. |
| `announce` / `tts_voice` | `announce` speaks the song when *you* request one (auto-queued ones stay silent). `tts_voice` is an [edge-tts](https://github.com/rany2/edge-tts) neural voice (default `en-US-AriaNeural`); falls back to the offline Windows voice if edge-tts/network is unavailable. |
| `lock_ips` | `false` (default) lets any LAN device connect. `true` enforces the `allowed_ips` whitelist. Toggle live from the player's Settings. |
| `auto_queue` | `true` (default) keeps playing forever, Spotify-style: when the queue is nearly empty it appends songs seeded from the recent listening *context* (several songs you didn't skip), ranked toward your taste. |
| `auto_queue_batch` | How many related songs to append per refill (default 5). |
| `auto_queue_threshold` | Refill when this many tracks remain (default 2 — two from the end). |
| `history_size` | How many recent plays to remember and keep out of the queue, for smart-shuffle no-repeats (default 100). |
| `queue_liked_boost` / `queue_same_artist_boost` / `queue_playthrough` / `queue_skip_penalty` / `queue_song_play` / `queue_jitter` / `queue_liked_seed_prob` / `queue_context_songs` | Smart-shuffle weights. `queue_same_artist_boost` (default 3.0, higher than `liked_boost`) biases the endless queue toward the *same band* you're playing, more than just the same genre. |
| `ytdl_raw_options` | Optional list of extra yt-dlp options as `"key=value"` strings (advanced). |
| `allowed_ips` | List of allowed client IPs. Empty `[]` allows all (for testing) |
| `artist_track_count` | Number of tracks to play when requesting an artist (default: 20) |
| `album_track_count` | Max tracks for album playback. `0` means unlimited (entire album, default: 0) |
| `search_cache_ttl` | Search cache time-to-live in seconds (default: 1800 = 30 min) |
| `search_cache_max_size` | Max entries in the search cache (default: 500) |

## YouTube Authentication

YouTube blocks unauthenticated `yt-dlp` requests with **"Sign in to confirm
you're not a bot."** Playback needs cookies from a logged-in YouTube session,
plus Node.js to solve the signature challenge.

**`setup.ps1` configures this for you** — it tries each installed browser and
falls back to a cookies file. The detail below is for doing it by hand or
debugging what the script reports.

### Option A — read cookies live from your browser (preferred)

```json
"cookies_from_browser": "firefox"
```

yt-dlp reads the session at each download, so nothing expires on disk. Two
things commonly stop it working:

- **The browser must be closed.** A running browser holds a lock on its cookie
  database and yt-dlp fails with *"Could not copy ... cookie database"*.
  `setup.ps1` detects this and offers to retry once you close it.
- **Chromium browsers may be unreadable.** Chrome v127+ (and Edge, Brave and
  friends built on it) encrypt cookies with App-Bound Encryption that yt-dlp
  cannot decrypt — *"Failed to decrypt with DPAPI"*. **Firefox has no such
  restriction and is the reliable choice:**

  ```powershell
  winget install --id Mozilla.Firefox -e
  ```

  Log into YouTube in Firefox once, then re-run `setup.ps1`.

### Option B — export a cookies file

Works with any browser, including Chrome, and needs no browser closed at
download time.

1. Install the **"Get cookies.txt LOCALLY"** extension (exports locally; nothing is uploaded).
2. Open a **private/Incognito window**, go to **youtube.com**, confirm you're logged in.
   *(Private-window cookies aren't rotated by normal browsing, so they last much longer.)*
3. Extension → **Export** (Netscape format) → save as `youtube_cookies.txt` in the project folder.
4. Point `config.json` → `cookies_file` at that path, or just re-run `setup.ps1`
   and it will pick the file up.

Exported cookies do expire — if the bot error returns, export again.

> **Account note:** yt-dlp uses your real Google session; there is a small risk
> YouTube flags the account. Consider a throwaway Google account.

> **Rate limits:** many rapid requests from one IP can trigger the bot check even
> with valid cookies. Normal use (a handful of songs) is fine; it clears on its own.

## How It Works

The server uses a two-stage architecture:

1. **Search stage:** When a request arrives, the spoken phrase is cleaned and parsed (dictation cleanup, grammar matching, transport detection). The query is sent to YouTube Music via `ytmusicapi`, which returns real music metadata (titles, artists, albums, video IDs). Album track listings come in correct order from YouTube Music's browse endpoints.

2. **Playback stage (download-then-play):** For each resolved video ID, the server runs `yt-dlp` (with your cookies and Node) to download the audio to a local cache file, then hands that file to a persistent `mpv` process over a Windows named-pipe IPC connection. The first track downloads before playback starts (~1–2 s); the rest are prefetched in the background. Downloading avoids the HTTP 403 that `mpv`/`ffmpeg` hit when fetching YouTube stream URLs directly.

This separation means search results have accurate metadata immediately, and playback is handled by a mature media player rather than COM automation.

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
- Opens a compact, **borderless, rounded, always-on-top player** (a `pywebview` flyout rendering `/player`) that behaves like a Windows flyout: album art, song and artist, a live progress bar, transport controls, a volume slider (0–150), like/save, search, the queue, and a full-height Settings sheet (themes, EQ, crossfade, normalize, announce, start-on-boot, IP lock, sleep timer). **Click anywhere outside it and it hides.**
- Adds a **system-tray icon** — click it (or "Show Player") to pop the player back up near the tray. The menu also opens the phone/browser page and can Quit.

> The launcher runs under whatever Python opens `.pyw` files. If that isn't the one with the deps, it re-execs itself under config `python_path` (which needs `pywebview`, `pystray`, and `Pillow`).

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

YouTube's 2026 anti-bot stack (SABR, PO tokens, session-bound URLs) means a stream URL that `yt-dlp` resolves will typically return **HTTP 403 when `mpv`/`ffmpeg` tries to fetch it directly** — regardless of format or client. `yt-dlp` itself downloads reliably, so this server **downloads each track, then plays the local file**. It's the consistent path; the first track takes a few seconds, and everything after is prefetched.

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
  setup.ps1              One-shot installer: prerequisites, cookies, config, verification
  launcher.pyw           Double-click entry: tray icon + webview player, starts the server
  MusicRequestServer.spec  PyInstaller build spec (-> standalone .exe)
  config.example.json    Copy to src/config.json and edit
  requirements.txt       Python dependencies
  README.md / CHANGELOG.md
  src/
    app.py               Flask routes + startup (run: python src/app.py)
    auth.py              API key (auto-gen) + IP allowlist checks
    paths.py             Source-vs-frozen file locations
    matching.py          Query parsing, grammar, natural-language commands
    interpret.py         Optional Groq LLM request parsing
    search.py            YouTube Music search + smart interpretation (ytmusicapi)
    player.py            Dual-mpv playback: download-then-play, crossfade, smart-shuffle
    config.json          Your settings (gitignored)
    templates/
      player.html        The desktop player UI
      setup.html         Siri-Shortcut setup instructions (served at /)
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Any missing prerequisite | mpv / yt-dlp / Node not installed or not on PATH | Run `setup.ps1` — it installs all three and verifies them |
| "mpv not found on PATH" | mpv is not installed or not on system PATH | `winget install --id shinchiro.mpv -e`, then restart the terminal |
| "yt-dlp not found on PATH" | yt-dlp is not installed | `winget install --id yt-dlp.yt-dlp -e` |
| `ModuleNotFoundError: No module named 'flask'` at launch | Launcher ran `app.py` under a Python that lacks the deps | Set `config.json` → `python_path` to the interpreter where you `pip install`ed everything |
| "Sign in to confirm you're not a bot" / nothing plays | No/expired cookies, or IP rate-limited | Re-run `setup.ps1`, or export fresh cookies (see [YouTube Authentication](#youtube-authentication)); if it was working, wait for the rate limit to clear |
| "Could not copy ... cookie database" | The browser is running and holds a lock on it | Close the browser fully, then re-run `setup.ps1` |
| "Failed to decrypt with DPAPI" | Chrome v127+ App-Bound Encryption | Use Firefox for `cookies_from_browser`, or switch to an exported cookies file |
| Player window shows a bare **"Not Found"** | Another server (often a stale copy) holds port 5000 — a `127.0.0.1` bind wins over ours | `Get-NetTCPConnection -LocalPort 5000 -State Listen`; close the extra instance. Current builds detect this and move to a free port |
| Requests ignore Groq and use the basic parser | Bad key, or the model is blocked/rate-limited at your Groq org | Check `server.log` for `[groq] HTTP ...`; enable a model at console.groq.com/settings/limits |
| Track starts then instantly stops / "only images available" | Node not installed or `js_runtime` not set | Install Node.js and set `config.json` → `js_runtime: "node"` |
| "ytmusicapi validation failed" | YouTube changed its internal API; ytmusicapi is out of date | Run `pip install --upgrade ytmusicapi` and check the package's GitHub for notes |
| No search results | Query too vague or artist name misspelled by dictation | Try a more specific phrase, e.g. "the song Yellow by Coldplay" |
| Region-locked or premium-only track | The chosen video is unavailable in your region | Check the spoken error message; the server tries alternate results automatically for song requests |
| No audio but mpv is running | Volume too low, wrong output device, or mpv audio backend issue | Check system volume mixer. Try `GET /api/control/volume?value=75` |
| "mpv IPC pipe did not appear" | mpv failed to start or the named pipe path is wrong | Run `mpv --version` manually. Check for conflicting pipe names. |
| Siri gets no response | PC is sleeping | Disable sleep mode or wake the PC first |
| Playback starts but track name is wrong | Wrong search result chosen | The server takes the top ytmusicapi result filtered to the requested artist. Be more specific in your query. |

## Legal Note

This server streams audio from YouTube Music, which is outside YouTube's terms of service for playback. It is intended for personal use only. `ytmusicapi` is an unofficial client that communicates with reverse-engineered YouTube Music endpoints and may need updating when YouTube changes its internals.