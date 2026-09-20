# Music Request Server

A local network music playback server that streams from YouTube Music through HTTP API, Siri voice commands, Shortcuts, and a web interface. No local music library required — every request is resolved by searching YouTube Music with `ytmusicapi` and played by `mpv`.

<p align="center">
  <img src="docs/screenshots/player.png" alt="The player: artwork, queue and a single box you type anything into" width="300">
  <img src="docs/screenshots/guest-capsule.png" alt="A guest link on a phone, choosing between playing here and the computer's speakers" width="300">
</p>

Ask for a song, an artist, an album or a vibe and it works out what comes next
— on the left. Hand somebody a link and they get their own queue on their own
phone, without touching yours — on the right.

<details>
<summary>More of it</summary>

<p align="center">
  <img src="docs/screenshots/lyrics.png" alt="Time-synced lyrics" width="270">
  <img src="docs/screenshots/settings-sound.png" alt="Sound settings" width="270">
  <img src="docs/screenshots/settings-queue.png" alt="Queue and radio settings" width="270">
</p>
</details>

## A few things it does that aren't obvious

**Ask for a playlist out loud.** "Make me a thirty minute grunge playlist",
"a two hour playlist of Nirvana and Soundgarden", "half an hour of jazz and
blues", "a playlist like Bohemian Rhapsody". The length can be counted or
said; the subject can be one genre, several, one band, several, or a single
record to build around. It fills to the length asked for, one act at a time
rather than five songs by the first one, and files it under a sensible name.

**About this song.** The ⓘ beside the settings gear — or the
Lyrics / About switch, on a screen wide enough to give the words their own
column — swaps the lyrics for where the record came from. Wikipedia's
background section and Last.fm's write-up, never anything invented: the
model that shortens them is only allowed to use what those said.

**Shared playlists.** Press ⇄ on one of your lists and everyone holding a
link can see it, play it and add to it. There is one copy — the house is
looking at the same list, and each row says who put it there. People can
take back their own additions and nothing else, and the list stays yours to
delete. Even a link that expires can put a song in one, because the list
outlives the evening.

**The volume follows the clock.** Quieter after eleven, eased off after
eight, back up at seven. The level you set is remembered as the level you
meant for that time of day, so turning it up at midnight makes midnight
louder rather than starting an argument. One toggle in settings turns it off.

## Playing somewhere else

The whole player runs in a browser, so anything with one is a speaker.

- **Your own phone.** The capsule at the top of the output picker moves the
  sound between this computer and the device you're holding. The song and the
  position come with you; it doesn't start again.
- **Somebody else's phone.** Make them a link in **Settings → Sharing**. A
  *phone-only* link plays on their device and nowhere else; a *full* link can
  also drive the computer's speakers. Either way they get their own queue,
  their own history and their own radio — they never see yours, and nothing
  they play reaches your Recently Played or your Last.fm.
- **Take it back.** Every link is named and revocable, one at a time or all at
  once. The list shows who's listening, to what, and how much they've asked
  for.

Links are signed passes rather than the key itself, they expire, and three
wrong guesses from one address earns a 24-hour ban.

### On a computer

Open any link in a browser window wider than 900px and the player rearranges
itself rather than stretching: the queue becomes a column down the right that
stays visible while you use the rest of it, the cover takes the height it's
given, and the search box moves to the top. The tray flyout stays the narrow
bar it's meant to be — **right-click the tray icon → Open desktop player** to
get the wide one on this machine.

A link playing on a laptop also gets the volume slider, which used to be
hidden from every link on the assumption that "playing on your own device"
meant a phone with hardware buttons.

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

`setup.ps1` also installs the Python packages. Then double-click
**`launcher.pyw`** (server + tray player), or run it from a terminal:

```bash
python launcher.pyw              # server, tray icon and player window
python launcher.pyw --headless   # server only, no window
python launcher.pyw --check      # the access checks
```

Settings live in `%LOCALAPPDATA%\MusicRequestServer\config.json`, created on
first run. If `api_key` is blank a random secret is generated. Open
`http://<pc-ip>:7420/` for the endpoint URL and the Siri Shortcut steps.

### What setup.ps1 does

| Step | Detail |
|------|--------|
| Prerequisites | Installs `mpv`, `yt-dlp`, `Node.js` via winget; skips anything already present |
| Python packages | `pip install -r requirements.txt` (source runs only — the .exe bundles them) |
| Config | Creates your config if there isn't one, and sets `python_path`. Everything else comes from the app's own defaults |
| Cookies | Tries `--cookies-from-browser` against each installed browser, falls back to a cookies file |
| Verify | Runs a real YouTube fetch through the same client fallback chain the app uses, and reports exactly what failed |

Useful flags: `-SkipCookies` (tools only), `-CookieBrowser firefox` (skip auto-detection).

### Build the .exe yourself

One command. Runs the checks, builds, closes the running copy, installs over
`dist\MusicRequestServer`, and starts it again:

```powershell
.\build.ps1
```

`-NoRestart` leaves it closed, `-CheckOnly` just runs the checks, and
`-SkipChecks` goes straight to building. It stages the build in `%TEMP%`
first because a running exe holds a lock on its own folder.

By hand, if you'd rather:

```bash
pip install pyinstaller
pyinstaller --noconfirm MusicRequestServer.spec
```

Then check the thing you actually built, which the test suite never sees:

```
dist\MusicRequestServer\MusicRequestServer.exe --selftest
```

It boots the server inside the bundle, renders the player, calls ten
endpoints, loads every store and parses a spoken phrase — the failures that
only exist frozen, like a hidden import PyInstaller didn't spot or a template
it didn't collect. Non-zero exit if anything is wrong.

**Run the `dist` folder, not `build`.** PyInstaller creates both:

| Folder | What it is |
|--------|------------|
| `dist\MusicRequestServer\` | **The actual app.** Run `MusicRequestServer.exe` from here. This is what ships. |
| `build\` | Scratch working files from the compile. Nothing to run; safe to delete anytime. |

mpv, yt-dlp and Node still need to be on PATH — run `setup.ps1` on the target
machine to handle that.

## Prerequisites

> `setup.ps1` installs all of these for you — this list is what it does, and what
> to install by hand if you would rather.

- **mpv** media player (audio-only mode): `winget install --id shinchiro.mpv -e`
- **yt-dlp** (audio fetcher): `winget install --id yt-dlp.yt-dlp -e`
- **Node.js** — required so yt-dlp can solve YouTube's JavaScript signature challenge (otherwise no audio formats are returned): `winget install --id OpenJS.NodeJS.LTS -e`
- Python 3.10+ with packages from `requirements.txt` *(source runs only — the .exe bundles them)*
- A **logged-in YouTube session**, either read live from your browser or exported to a cookies file — YouTube blocks unauthenticated requests with a "confirm you're not a bot" error. See [YouTube Authentication](#youtube-authentication) below.

> **Important — use the same Python for everything.** All packages must be installed into the interpreter set in `config.json` → `python_path`. On this machine that is Python 3.12 (`C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`). If `launcher.pyw` is started by a Python that lacks them, it relaunches itself under `python_path` to avoid `ModuleNotFoundError`.

## Configuration

Most settings are changed from the player's Settings tab. To edit the file
directly, close the app first and open
`%LOCALAPPDATA%\MusicRequestServer\config.json`. Anything you leave out takes
the app's default; `config.example.json` shows the commonly changed ones with
their real default values. `api_key` blank ⇒ generated on first run.

| Field | Description |
|-------|-------------|
| `host` | Bind address. Use `0.0.0.0` for network access, `127.0.0.1` for local only |
| `port` | TCP port the server listens on (default `7420`) |
| `api_key` | Secret key for authentication. **Change this from the default** |
| `python_path` | Explicit path to the Python interpreter that has all dependencies installed (e.g. `C:\...\Python312\python.exe`). `launcher.pyw` relaunches under this if its own Python lacks the packages. |
| `cookies_file` | Path to a Netscape-format `youtube_cookies.txt` exported from a logged-in YouTube session. Required for playback. If the file is missing it is ignored (with a warning). |
| `cookies_from_browser` | Alternative to `cookies_file`: a browser name yt-dlp reads live cookies from, e.g. `firefox`. **Chrome/Edge do not work on Windows** (App-Bound Encryption). Leave empty if using `cookies_file`. |
| `js_runtime` | JavaScript runtime yt-dlp uses to solve the signature challenge. Set to `node` (yt-dlp only auto-enables Deno otherwise). Required for audio to resolve. |
| `player_client` / `player_client_fallbacks` | YouTube player clients for yt-dlp, tried in order: default `web_embedded`, then `web`, `mweb` and yt-dlp's own choice. YouTube breaks these periodically; the app remembers whichever last worked and starts there. |
| `use_groq` / `groq_api_key` / `groq_model` | Optional. With a free [Groq](https://console.groq.com) key, requests are parsed by an LLM (far better at casual phrasing than the regex grammar). Empty key = local parser. Model defaults to `openai/gpt-oss-20b`; the Settings tab lists the models your key can use. |
| `announce` / `tts_voice` | `announce` speaks the song when *you* request one (auto-queued ones stay silent). `tts_voice` is an [edge-tts](https://github.com/rany2/edge-tts) neural voice (default `en-US-AriaNeural`); falls back to the offline Windows voice if edge-tts/network is unavailable. |
| `lock_ips` | `false` (default) lets any LAN device connect. `true` enforces the `allowed_ips` whitelist. Toggle live from the player's Settings. |
| `auto_queue` | `true` (default) keeps playing forever, Spotify-style: when the queue is nearly empty it appends songs seeded from the recent listening *context* (several songs you didn't skip), ranked toward your taste. |
| `auto_queue_batch` | How many related songs to append per refill (default 5). |
| `auto_queue_threshold` | Refill when this many tracks remain (default 2 — two from the end). |
| `history_size` | How many recent plays to remember and keep out of the queue, for smart-shuffle no-repeats (default 100). |
| `artist_gap` / `artist_gap_slip` | Prefer not to repeat an artist within this many tracks (default 4), but let it through anyway this often (0.15) — a rule that never bends feels mechanical. |
| `sleep_fade` | Seconds the sleep timer takes to fade out (default 20). It always puts the volume back afterwards. |
| `lastfm_seeded` | Set once, after taste has been given a head start from your Last.fm top artists. Clear it to import again. |
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

### Option B — export a cookies file (the reliable one)

Works with any browser, needs nothing closed at download time, and is what
`setup.ps1` falls back to. If Option A gives you trouble, just do this.

1. Install the **"Get cookies.txt LOCALLY"** extension (Chrome, Edge or Firefox
   web store). It exports locally — nothing is uploaded.
2. Open a **private / incognito window**, go to **youtube.com**, and sign in.
   *(Private-window cookies aren't rotated by normal browsing, so they last far longer.)*
3. Click the extension icon → **Export** → **Netscape format**.
4. Save it with **exactly this name, in exactly this place**:

   ```
   youtube_cookies.txt
   ```

   | Running | Put the file here |
   |---------|-------------------|
   | The release `.exe` | Next to `MusicRequestServer.exe`, in the same folder as `setup.ps1` |
   | From source | The repo root — next to `launcher.pyw` |

   The name matters: lowercase, underscores, `.txt`. Windows may hide the
   extension — if you end up with `youtube_cookies.txt.txt` it won't be found.
5. Re-run `setup.ps1`. It finds the file, tests it against a real download, and
   writes `cookies_file` into your config.

If no cookies can be set up automatically, `setup.ps1` drops a clearly-labelled
**placeholder** `youtube_cookies.txt` at that exact path and opens the folder, so
there's no guessing where it goes — just overwrite the placeholder with your
real export.

Exported cookies do expire. When the bot error comes back, export again and
re-run `setup.ps1`; it will tell you whether the new file works.

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

All endpoints return JSON and take the key in the `X-Music-Key` header. Anything that changes something is a `POST` with its parameters as a JSON body; reads are `GET`. A shared link's token may also ride in the URL as `?token=`, because a link has nowhere else to put it.

**Compatibility mode.** Installs from before this change keep working exactly as they did — `GET` for everything and `?key=SECRET` in the URL — until you turn off *Allow old GET requests* (`allow_legacy_get_mutations`) and *Allow the key in a link* (`allow_key_in_url`) under Settings → Security. New installs start with both off. The examples below are the new form.

### Play a request
```
POST /api/play
X-Music-Key: SECRET
{"q": "Yellow by Coldplay", "type": "auto", "shuffle": 0, "mode": "play"}
```

Parameters:
- `q` or `song`: The search phrase (required)
- `artist`: Optional artist filter
- `type`: `auto`, `song`, `album`, or `artist` (default: `auto`)
- `shuffle`: `0` or `1` (default depends on type)
- `mode`: `play`, `next`, or `queue` (default: `play`)

### Play a specific video
```
POST /api/play/video/YOUTUBE_VIDEO_ID      (X-Music-Key: SECRET, body {})
```

### Status
```
GET /api/status                            (X-Music-Key: SECRET)
```

Returns current track, player state, playlist position, and recent requests.

### Transport controls
```
POST /api/control/pause                    body {}
POST /api/control/resume                   body {}
POST /api/control/next                     body {}
POST /api/control/previous                 body {}
POST /api/control/volume                   body {"value": 75}
POST /api/control/shuffle                  body {}
```

### Health check
```
GET /api/ping
```

Returns `{"status": "ok"}` instantly.

## Windows Firewall

Allow the server port on **private networks only**:

```powershell
New-NetFirewallRule -DisplayName "Music Request Server" -Direction Inbound -LocalPort 7420 -Protocol TCP -Action Allow -Profile Private -RemoteAddress 192.168.1.0/24
```

**Never** allow on Public profile. Replace `192.168.1.0/24` with your subnet or your phone's specific IP.

For maximum security, scope to just your phone:
```powershell
New-NetFirewallRule -DisplayName "Music Request Server" -Direction Inbound -LocalPort 7420 -Protocol TCP -Action Allow -Profile Private -RemoteAddress 192.168.1.50
```

## HTTPS with a real certificate

A certificate this machine signs for itself is worthless — Safari refuses it
outright, iOS offers no exception for a bare IP, and the desktop window needs
a browser flag to load its own player. A certificate signed by an authority,
for a name that points at your house, works everywhere with no warnings.
`certificate.ps1` gets one from Let's Encrypt and keeps it renewed.

You need a name first. The Sharing tab's **A name that follows you** keeps a
Dynu hostname pointed at your address; set that up before this.

1. **Dynu API credentials.** Dynu control panel → **API Credentials**. That
   page gives an OAuth2 **Client ID** and **Secret** — not your account
   password, and not the older API key.

2. **Get the certificate.** In PowerShell, in the project folder:

   ```powershell
   .\certificate.ps1 -Domain music.example.dynu.net -ClientId YOUR_CLIENT_ID
   ```

   It asks for the secret without echoing it, installs Posh-ACME if it isn't
   there, proves the name is yours through a DNS record Dynu writes for it,
   and puts the certificate in `%LOCALAPPDATA%\MusicRequestServer\certs`.
   Nothing needs to be reachable from the internet while this runs, and no
   port has to be open.

   Add `-Staging` for a rehearsal: it proves the DNS side works without
   spending one of the five certificates a week Let's Encrypt allows per
   name. A staging certificate is not trusted, so switch it off again.

3. **Restart the player once.** It finds the files by itself and serves
   https on its own port; links and QR codes change to `https://` and the
   Sharing tab says how many days are left on the certificate.

Renewal is a scheduled task that checks nightly. When it writes a new
certificate the server picks it up within a minute, without interrupting
what's playing.

What still uses plain http: this machine, on loopback only. A certificate
belongs to a name, and the desktop window asking for it at `127.0.0.1` would
be a name mismatch — so the window, the tray and the app's own health checks
talk to a plaintext port bound to `127.0.0.1` that nothing else can reach.

**Devices on your own wifi** have to resolve the name to reach it. Most
routers handle a LAN device asking for your public address (NAT hairpinning);
some don't. If yours doesn't, add a second Dynu hostname pointing at this
machine's LAN address and cover both:

```powershell
.\certificate.ps1 -Domain music.example.dynu.net -AlsoCover home.music.example.dynu.net -ClientId YOUR_CLIENT_ID
```

Some DNS servers refuse to return private addresses (rebinding protection),
in which case that trick won't work either and the LAN falls back to the
address with no encryption.

## Signing in with Google

A link is a credential anybody can forward, and it can't tell two people
apart. An account is a person: their queue, their history and their playlists
follow them to whatever device they pick up, and taking somebody's access away
is a change to their account rather than a hunt for who else has the link.

Links don't go away — one is still how a person gets in the first time. What
a link stops being is the identity.

Needs HTTPS first: Google will not send anybody back to a plain http address
that isn't localhost.

1. **Write down your own address first.** Settings → Access → *Your own
   email*. The account matching it becomes the owner. Being the first person
   to sign in doesn't do it — otherwise whoever found the address first would
   own the server.

2. **Make a Google client.** [Google Cloud Console](https://console.cloud.google.com/)
   → APIs & Services → Credentials → **Create credentials → OAuth client ID**
   → *Web application*.

   - **Authorised JavaScript origins:** `https://music.example.dynu.net:7420`
   - **Authorised redirect URIs:** `https://music.example.dynu.net:7420/auth/google/callback`

   Settings → Access shows the exact redirect URI with a Copy button — paste
   that, it has to match character for character.

3. **Paste the client ID and secret** into Settings → Access. The secret is
   kept on this machine and never handed back to any page.

Then hand somebody a link as usual. On their phone, Settings → **Sign in**
turns that link into an account with exactly the reach the link had: a
phone-only link makes an account that plays on their own device, a full link
makes one that can drive the speakers too. You can change that afterwards, or
block them, from the same panel.

What's checked when somebody comes back from Google: that the sign-in was one
this server started, that the token was issued to this server's client id by
Google, that it hasn't expired, that it answers this particular sign-in, and
that Google has verified the email address. The token is fetched from Google
directly over TLS using the client secret, so nothing the browser carried is
taken on trust.

The session is a signed cookie holding an account id and nothing else. It
lasts 30 days, is HttpOnly, and is marked Secure whenever the server is on
https.

## Network Setup

### Static DHCP lease

Assign a static IP to your PC in your router's DHCP settings. This ensures the server is always at the same address.

Also assign a static IP to your phone so the `allowed_ips` list stays valid.

### Auto-start at logon

Create a scheduled task that runs at logon:

```powershell
$action = New-ScheduledTaskAction -Execute "pythonw" -Argument "C:\path\to\siri-to-pc\launcher.pyw --headless"
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

With `auto_queue` on (default), playback never stops. Tracks are downloaded ahead of time so there's no gap between songs.

YouTube's own radio is one of the things it draws on, not the whole of it — on its own it drifts, because every step is a reasonable hop from the last one and thirty reasonable hops is a long way from where you started. So candidates come from several places at once and get ranked together:

| lane | where the records come from |
| --- | --- |
| `near` | what people actually play alongside the song you asked for (Last.fm similar tracks, asked for by name) |
| `anchor` | YouTube's radio for the song you asked for |
| `radio` | YouTube's radio for what's on now |
| `root` | the genre of the song you asked for, kept open so the seam doesn't run out |
| `kin` | records by the artists Deezer files next to this one |
| `artist` | the band's own catalogue |
| `theme` | the genre, if you asked for one by name |

Everything is then scored against **the song you asked for**, not just the one that's playing — measuring only against what's on is how a metal request ends up playing pop-punk half an hour later, each step looking fine. On top of that a track has to name the right genre to get in, it loses points for coming from a different era (MusicBrainz start years, adjusted so a person's birthday and a band's formation date mean the same thing), and it gains them for being somebody the anchor's artist belongs next to.

Every row in the queue tells you which of these picked it.

### Asking for two things at once

```
play songs by nirvana and foo fighters
play some thrash and black metal
```

Both get played, dealt out in turn, and the radio keeps steering by both — a
record that sounds like either one is a good answer, so neither gets buried by
whichever you happened to say first. If one of them has less to offer the
queue leans to the other rather than stalling.

Nobody says "thrash metal and black metal" out loud, so the noun said once at
the end is carried back. Plenty of acts have "and" in the middle of their
name, so before a split is believed the whole phrase is checked against real
artists — Simon and Garfunkel, Florence and the Machine and Nick Cave and the
Bad Seeds all stay in one piece, and so does drum and bass. An ampersand
never splits anything: Earth, Wind & Fire is one band.

### When the internet goes away

Playback carries on from what's already downloaded rather than stopping at
the end of the track, and a request tells you the truth ("I can't reach the
internet") instead of claiming the song doesn't exist. It notices within
about three failed calls and stops making them, so a refill costs nothing
instead of eleven seconds of timeouts.

Toggle the whole thing live from the music bar.

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
5. Add **"Get Contents of URL"** action with the URL `http://192.168.1.XXX:7420/`
   - **Method:** POST
   - **Headers:** `X-Music-Key` = your API key
   - **Request Body:** JSON, one field `input` = Dictated Text

   Replace `192.168.1.XXX` with your PC's IP. The player's *Set up the Shortcut* page shows the exact address and key. A shared link's token can go in the URL instead (`http://…:7420/?token=TOKEN`), which is what the links you hand out do.
6. Add **"Get Dictionary Item"** action (input: URL response, key: `message`)
7. Add **"Speak Text"** action (input: the message value)
8. Optionally add **"Show Notification"** for visual feedback

### Pre-named shortcut (one phrase to Siri)

Create a shortcut named "Play Some Fleetwood Mac":

1. Name the Shortcut exactly what you'll say to Siri
2. Add **"Get Contents of URL"**, set up as above, with `input` fixed to `play some Fleetwood Mac`
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
  launcher.pyw           Tray icon + the flyout player window
  MusicRequestServer.spec  PyInstaller build spec (-> standalone .exe)
  src/mrs/
    config.py            One config file, typed defaults, atomic writes
    paths.py             Single data directory (+ migration from older layouts)
    events.py            Pub/sub bus behind the SSE stream
    models.py            Track / Candidate / Plan, and the dedupe key
    player.py            Orchestrator: mpv + queue + audio + taste
    requests.py          One entry point for every request (Siri, UI, API, alarm)
    server.py            Boot sequence, port selection, uvicorn
    selftest.py          --selftest: checks the frozen build, not the source
    core/
      mpv.py             Named-pipe IPC (overlapped I/O, watchdog)
      queue.py           Candidate pool -> download workers -> playlist
      context.py         What could play next, and how it's scored
      gate.py            One queue per outside service, so nothing floods them
      tags.py            Last.fm: what a song sounds like, and what sits beside it
      kin.py             Deezer: which artists belong next to each other
      era.py             MusicBrainz: roughly when an artist's records come from
      tempo.py           BPM, for the crossfade
      radio.py           Live stations
      playlists.py       Saved playlists
      backup.py          Copy the profile out, and put one back
      downloader.py      yt-dlp: retries, client fallback, cache, pinning
      taste.py           Play-throughs vs skips, likes, ranking
      audio.py           EQ, normalise, crossfade, level metering
      cookies.py         Cookie testing and opportunistic refresh
      library.py         Local file index
      extras.py          Last.fm, alarms, casting
    resolve/
      parser.py          One parsing decision (grammar for controls, LLM otherwise)
      resolver.py        Turning a parsed plan into actual tracks
      conjunction.py     Reading "and": two things, or one name with "and" in it
      grammar.py         Local phrase parsing
      numbers.py         Spoken numbers: "half an hour", "one point five hours"
      llm.py             Groq
      catalog.py         YouTube Music, SoundCloud, Bandcamp
      spotify.py         Spotify links via spotdl
      lyrics.py          LRCLIB
    web/
      api.py             FastAPI routes + SSE
      templates/         player.html, remote.html, setup.html
```

Config and state live in `%LOCALAPPDATA%\MusicRequestServer\` — one location,
whether you run the .exe or from source.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Any missing prerequisite | mpv / yt-dlp / Node not installed or not on PATH | Run `setup.ps1` — it installs all three and verifies them |
| "mpv not found on PATH" | mpv is not installed or not on system PATH | `winget install --id shinchiro.mpv -e`, then restart the terminal |
| "yt-dlp not found on PATH" | yt-dlp is not installed | `winget install --id yt-dlp.yt-dlp -e` |
| `ModuleNotFoundError` (e.g. `fastapi`) at launch | `launcher.pyw` ran under a Python that lacks the deps | Set `config.json` → `python_path` to the interpreter where you `pip install`ed everything |
| "Sign in to confirm you're not a bot" / nothing plays | No/expired cookies, or IP rate-limited | Re-run `setup.ps1`, or export fresh cookies (see [YouTube Authentication](#youtube-authentication)); if it was working, wait for the rate limit to clear |
| "Could not copy ... cookie database" | The browser is running and holds a lock on it | Close the browser fully, then re-run `setup.ps1` |
| "Failed to decrypt with DPAPI" | Chrome v127+ App-Bound Encryption | Use Firefox for `cookies_from_browser`, or switch to an exported cookies file |
| Player window shows a bare **"Not Found"** | Another server (often a stale copy) holds the port — a `127.0.0.1` bind wins over ours | `Get-NetTCPConnection -LocalPort 7420 -State Listen` (or your configured port); close the extra instance. Current builds detect this and move to a free port |
| Requests ignore Groq and use the basic parser | Bad key, or the model is blocked/rate-limited at your Groq org | Check `server.log` for `[groq] HTTP ...`; enable a model at console.groq.com/settings/limits |
| Track starts then instantly stops / "only images available" | Node not installed or `js_runtime` not set | Install Node.js and set `config.json` → `js_runtime: "node"` |
| Every download fails with "the page needs to be reloaded" | The YouTube player client in your config stopped working | Set `player_client` to `web_embedded`. The app also falls through to the clients in `player_client_fallbacks` automatically |
| A 30-second version plays instead of the song | A preview/snippet upload was picked | `min_duration` (default 60s) rejects these; lower it only if you play genuinely short tracks |
| "ytmusicapi validation failed" | YouTube changed its internal API; ytmusicapi is out of date | Run `pip install --upgrade ytmusicapi` and check the package's GitHub for notes |
| No search results | Query too vague or artist name misspelled by dictation | Try a more specific phrase, e.g. "the song Yellow by Coldplay" |
| Region-locked or premium-only track | The chosen video is unavailable in your region | Check the spoken error message; the server tries alternate results automatically for song requests |
| No audio but mpv is running | Volume too low, wrong output device, or mpv audio backend issue | Check system volume mixer. Try `GET /api/control/volume?value=75` |
| "mpv IPC pipe did not appear" | mpv failed to start or the named pipe path is wrong | Run `mpv --version` manually. Check for conflicting pipe names. |
| Siri gets no response | PC is sleeping | Disable sleep mode or wake the PC first |
| Playback starts but track name is wrong | Wrong search result chosen | The server takes the top ytmusicapi result filtered to the requested artist. Be more specific in your query. |

## Legal Note

This server streams audio from YouTube Music, which is outside YouTube's terms of service for playback. It is intended for personal use only. `ytmusicapi` is an unofficial client that communicates with reverse-engineered YouTube Music endpoints and may need updating when YouTube changes its internals.
### Start before sign-in

**Settings → System → Start before sign-in** registers a scheduled task that
starts the server with the machine rather than with the desktop, so links keep
working while the computer sits at the lock screen. Windows asks permission
once; the task runs as you, with no stored password (S4U).

Two things worth knowing:

- **This computer's own speakers stay silent until you sign in.** Nothing
  running before a user session gets given an audio device — the player is
  there, it will queue and download, and no sound comes out of the machine.
  Links play on their own devices exactly as always, which is the point of it.
- **Signing in hands over.** The early copy has no tray icon and no window, so
  the ordinary player takes the port off it when you log in. That's why turning
  this on also turns on *Start with Windows*: without something to hand over
  to, you would sign in to a server with no icon playing to nobody.
