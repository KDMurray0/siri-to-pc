"""Music Request Server - Flask application entry point.

A thin HTTP layer that parses spoken phrases, resolves them through
YouTube Music (ytmusicapi), and hands the resulting video IDs to mpv
for streaming playback.
"""

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template

from auth import require_auth, get_config, check_ip, check_api_key
from player import PlayerManager
from matching import build_search_plan, match_transport, match_command
from paths import data_dir, resource_dir
import search as search_module
import interpret


# ── App globals ───────────────────────────────────────────────────

app = Flask(__name__, template_folder=os.path.join(resource_dir(), "templates"))
_config = get_config()
player_manager = PlayerManager()
recent_requests_file = os.path.join(data_dir(), "recent_requests.json")


# ── Logging filter: redact API key from werkzeug request logs ─────

class ApiKeyRedactor(logging.Filter):
    """Filter that redacts the API key from log messages."""

    def __init__(self):
        super().__init__()
        self._key = None

    def _get_key(self):
        if self._key is None:
            try:
                config = get_config()
                self._key = config.get("api_key", "")
            except Exception:
                self._key = ""
        return self._key

    def filter(self, record):
        key = self._get_key()
        if not key:
            return True

        # Redact msg (the format string)
        if isinstance(record.msg, str) and key in record.msg:
            record.msg = record.msg.replace(key, "[REDACTED]")

        # FIX: werkzeug puts the request URL in record.args as a tuple.
        # We must redact the key from each arg element too.
        if isinstance(record.args, tuple):
            new_args = []
            for arg in record.args:
                if isinstance(arg, str) and key in arg:
                    new_args.append(arg.replace(key, "[REDACTED]"))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        elif isinstance(record.args, str) and key in record.args:
            record.args = record.args.replace(key, "[REDACTED]")

        return True


# Attach filter to Flask's werkzeug logger (no custom handler needed)
werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.addFilter(ApiKeyRedactor())


def save_recent_requests(req_list):
    """Persist recent requests to a small JSON file."""
    try:
        with open(recent_requests_file, "w") as f:
            json.dump(req_list[-50:], f)  # Keep last 50
    except Exception:
        pass


def load_recent_requests():
    """Load recent requests from file."""
    try:
        with open(recent_requests_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


recent_requests = load_recent_requests()

# Runtime-changeable default source (youtube | soundcloud | bandcamp)
_runtime_source = [(_config.get("source") or "youtube").lower()]


def _lan_address():
    """Best-guess LAN IP for the setup instructions."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def log_request(query, result):
    """Log a request to the recent requests list."""
    entry = {
        'time': datetime.now(timezone.utc).isoformat(),
        'query': query,
        'status': result.get('status', 'unknown'),
        'resolved_type': result.get('resolved_type'),
        'message': result.get('message', ''),
    }
    recent_requests.append(entry)
    save_recent_requests(recent_requests)


# ── Startup ───────────────────────────────────────────────────────

def startup():
    """Initialise the application: check binaries, validate ytmusicapi, start mpv."""
    print("Music Request Server starting...")

    # Check mpv is on PATH
    if not shutil.which("mpv"):
        print("=" * 60)
        print("ERROR: mpv not found on PATH!")
        print("Install with: winget install mpv")
        print("=" * 60)
        sys.exit(1)

    # Check yt-dlp is on PATH (needed by mpv's ytdl hook)
    if not shutil.which("yt-dlp"):
        print("=" * 60)
        print("ERROR: yt-dlp not found on PATH!")
        print("Install with: pip install yt-dlp")
        print("=" * 60)
        sys.exit(1)

    # Validate ytmusicapi connectivity
    try:
        search_module.validate_client()
        print("ytmusicapi client validated successfully.")
    except Exception as e:
        print("=" * 60)
        print(f"ERROR: ytmusicapi validation failed: {e}")
        print("=" * 60)
        sys.exit(1)

    # Groq LLM parsing — on whenever a key is present.
    _groq_key = (_config.get("groq_api_key") or "").strip()
    interpret.configure(
        api_key=_groq_key,
        model=_config.get("groq_model") or None,
        enabled=bool(_groq_key),
    )
    if interpret.available():
        print(f"Groq interpretation enabled ({interpret._cfg['model']}).")
    else:
        print("Groq interpretation off — using local parser.")

    # Initialise search cache from config
    search_module.init_cache(
        max_size=_config.get("search_cache_max_size", 500),
        ttl=_config.get("search_cache_ttl", 1800),
    )

    # Pass track-count config to search module
    search_module.set_config(
        artist_track_count=_config.get("artist_track_count", 20),
        album_track_count=_config.get("album_track_count", 0),
    )

    # Start mpv and connect IPC pipe (pass config for cookie options)
    player_manager.start(_config)

    # Wire the auto-queue (Spotify-style radio) provider: when the queue is
    # about to run dry, fetch related songs seeded by the current track.
    def _radio_provider(seed_video_id, exclude_ids):
        try:
            return search_module.get_radio(
                seed_video_id,
                limit=_config.get("auto_queue_batch", 5),
                exclude_ids=exclude_ids,
            )
        except Exception:
            return []

    player_manager.set_autoqueue_provider(_radio_provider)
    # The "like" button seeds the queue with songs related to the current track.
    player_manager.set_like_provider(_radio_provider)
    print("Server ready.")


# ── Routes ────────────────────────────────────────────────────────

# Playback controls Groq may return as kind="command".
_GROQ_CMD = {
    "pause": "pause", "stop": "pause", "resume": "resume", "play": "resume",
    "next": "next", "skip": "next", "previous": "previous", "back": "previous",
}


def _strip_play(text):
    """Drop a leading play-verb so a variant search keeps its 'remix'/'live'."""
    return re.sub(r'^\s*(?:play|put\s+on|throw\s+on|start(?:\s+playing)?)\s+',
                  '', (text or "").strip(), flags=re.IGNORECASE)


def _run_play(query, artist=None, type_hint="auto", shuffle_requested=None,
              mode="play", source=None):
    """Core request handler — returns a response dict. Shared by /api/play
    (GET) and the iOS-shortcut endpoint at / (POST)."""
    try:
        query = (query or "").strip()

        # ── Check for transport commands BEFORE search ──
        if query:
            transport = match_transport(query)
            if transport:
                action = transport["action"]
                try:
                    result = player_manager.control(action)
                    msg = result.get("message", "OK")
                    log_request(query, {"status": "ok", "resolved_type": None, "message": msg})
                    return {"status": "ok", "resolved_type": None, "message": msg}
                except Exception as e:
                    return {"status": "error", "message": f"Could not control playback: {e}"}

        # ── Natural-language commands (volume, like, more like this, surprise) ──
        if query:
            cmd = match_command(query)
            if cmd:
                c = cmd["command"]
                try:
                    if c == "volume":
                        r = player_manager.control("volume", cmd["value"])
                    elif c == "volume_delta":
                        r = player_manager.adjust_volume(cmd["delta"])
                    elif c == "like":
                        r = player_manager.control("like")
                    elif c == "more_like_this":
                        r = player_manager.queue_similar()
                    elif c == "surprise":
                        liked = player_manager.get_liked()
                        if liked:
                            plan = {"tracks": [{"video_id": t["video_id"], "title": t.get("title", ""),
                                                "artist": t.get("artist", ""), "album": t.get("album", ""),
                                                "thumbnail": t.get("thumbnail", "")} for t in liked],
                                    "fallbacks": [], "shuffle": True, "mode": "play"}
                            player_manager.play(plan)
                            r = {"message": "Surprising you with your liked songs"}
                        else:
                            r = {"message": None}  # fall through to a search below
                        if r.get("message") is None:
                            query = "today's biggest hits"  # discovery fallback
                            cmd = None
                    if cmd:
                        msg = r.get("message", "OK")
                        log_request(query, {"status": "ok", "message": msg})
                        return {"status": "ok", "message": msg}
                except Exception as e:
                    return {"status": "error", "message": f"Could not do that: {e}"}

        # "play my liked songs" / "play my likes" -> the taste profile
        if query and re.sub(r'[^a-z ]', '', query.lower()).strip() in (
                "play my liked songs", "play liked songs", "play my likes",
                "play my favourites", "play my favorites", "play my liked",
                "my liked songs", "liked songs", "play favourites", "play favorites"):
            liked = player_manager.get_liked()
            if not liked:
                return {"status": "not_found",
                        "message": "You haven't liked any songs yet"}
            plan = {"tracks": [{"video_id": t["video_id"], "title": t.get("title", ""),
                                "artist": t.get("artist", ""), "album": t.get("album", ""),
                                "thumbnail": t.get("thumbnail", "")} for t in liked],
                    "fallbacks": [], "shuffle": True, "mode": "play"}
            player_manager.play(plan)
            resp = {"status": "played", "resolved_type": "liked",
                    "message": f"Playing your {len(liked)} liked songs, shuffled"}
            log_request(query, resp)
            player_manager.announce(resp["message"])
            return resp

        if not query and not artist:
            return {"status": "error",
                    "message": "Please provide a query using the q or song parameter"}

        # Groq parse for ambiguous "auto" requests; any failure -> local parser.
        gi = None
        if query and type_hint == "auto" and not artist:
            try:
                gi = interpret.interpret(query)
            except Exception:
                gi = None

        if gi and gi.get("kind") == "command":
            act = _GROQ_CMD.get(gi.get("command"))
            if act:
                try:
                    r = player_manager.control(act)
                    msg = r.get("message", "OK")
                    log_request(query, {"status": "ok", "message": msg})
                    return {"status": "ok", "message": msg}
                except Exception:
                    gi = None
            else:
                gi = None

        if gi and gi.get("kind") in ("song", "album", "artist", "genre"):
            q = gi["query"]
            if gi["kind"] == "song" and gi.get("variant"):
                q = _strip_play(query)          # keep "remix"/"live"/"acoustic"
            plan = {
                "query": q,
                "artist": (gi.get("artist") or None),
                "kind": gi["kind"],
                "shuffle": shuffle_requested if shuffle_requested is not None
                           else gi.get("shuffle"),
                "mode": mode,
                "spoken": q,
            }
        else:
            # Build search plan from the spoken phrase (local grammar fallback)
            plan = build_search_plan(
                query,
                artist=artist,
                type_hint=type_hint,
                shuffle=shuffle_requested,
                mode=mode,
            )

        # Feed the resolver your taste so same-titled songs resolve toward
        # artists you actually listen to (liked + recently played through).
        try:
            search_module.set_preferences(player_manager.preferred_artists())
        except Exception:
            pass

        # Alternate source (SoundCloud / Bandcamp) via yt-dlp search.
        source = (source or _runtime_source[0]).lower()
        if source in ("soundcloud", "bandcamp"):
            tracks = search_module.resolve_external(plan["query"], source=source, limit=5)
            resolved = {
                "tracks": tracks[:1] if type_hint == "song" else tracks,
                "fallbacks": [t["video_id"] for t in tracks[1:]],
                "shuffle": plan.get("shuffle", False),
                "mode": mode,
                "kind": "song",
                "spoken": (f"Playing {tracks[0]['title']} from {source}"
                           if tracks else f"Nothing found on {source}"),
                "error": None if tracks else "no results",
            }
        else:
            # Resolve through YouTube Music
            resolved = search_module.resolve(plan)

        if resolved.get("error"):
            return {"status": "error", "resolved_type": None,
                    "message": resolved.get("spoken", f"No results found for {query}")}

        if not resolved.get("tracks"):
            return {"status": "not_found", "resolved_type": None,
                    "message": f"I could not find anything for {query} on YouTube Music",
                    "candidates": []}

        # Play through mpv
        try:
            play_result = player_manager.play(resolved)
        except Exception as e:
            return {"status": "error", "message": f"Could not play: {e}"}

        # Surface a playback failure (e.g. YouTube download blocked) instead of
        # falsely reporting success.
        if isinstance(play_result, dict) and play_result.get("status") == "error":
            fail = {
                "status": "error",
                "resolved_type": resolved.get("kind"),
                "message": play_result.get(
                    "message",
                    f"Could not play {query}. YouTube may be blocking the "
                    "download — check that cookies are valid.",
                ),
            }
            log_request(query, fail)
            return fail

        response = {
            "status": "played",
            "resolved_type": resolved.get("kind"),
            "message": resolved.get("spoken", f"Playing {query}"),
            "tracks": len(resolved.get("tracks", [])),
            "candidates": [
                {
                    "id": t["video_id"],
                    "name": t.get("title", ""),
                    "artist": t.get("artist", ""),
                    "album": t.get("album", ""),
                }
                for t in resolved.get("tracks", [])[:5]
            ],
        }

        log_request(query, response)
        player_manager.announce(response.get("message"))
        return response

    except Exception as e:
        return {"status": "error", "message": f"Something went wrong: {e}"}


@app.route("/api/play", methods=["GET"])
@require_auth
def api_play():
    """Main playback endpoint (GET)."""
    shuffle_param = request.args.get("shuffle")
    return jsonify(_run_play(
        query=(request.args.get("q") or request.args.get("song") or ""),
        artist=request.args.get("artist"),
        type_hint=(request.args.get("type") or "auto").lower(),
        shuffle_requested=(shuffle_param == "1") if shuffle_param else None,
        mode=(request.args.get("mode") or "play").lower(),
        source=request.args.get("source"),
    ))


@app.route("/api/play/video/<youtube_id>", methods=["GET"])
@require_auth
def api_play_video(youtube_id):
    """Play a specific YouTube video ID (for the web page candidate list)."""
    try:
        plan = {
            "tracks": [{"video_id": youtube_id}],
            "fallbacks": [],
            "shuffle": False,
            "mode": "play",
            "spoken": f"Playing video {youtube_id}",
        }
        player_manager.play(plan)
        return jsonify({
            "status": "played",
            "resolved_type": "song",
            "message": f"Playing video {youtube_id}",
            "target": {"id": youtube_id, "type": "video"},
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/status", methods=["GET"])
@require_auth
def api_status():
    """Get current playback status, including the upcoming queue."""
    try:
        status = player_manager.get_status()
        status["queue"] = player_manager.get_queue()
        return jsonify({
            **status,
            'recent_requests': recent_requests[-10:],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/autoqueue", methods=["GET"])
@require_auth
def api_autoqueue():
    """Enable/disable or toggle the Spotify-style auto-queue."""
    try:
        val = request.args.get("enabled")
        if val is None:
            enabled = not player_manager._autoqueue_enabled
        else:
            enabled = val in ("1", "true", "on", "yes")
        player_manager.set_autoqueue_enabled(enabled)
        return jsonify({"status": "ok", "auto_queue": enabled})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/audio", methods=["GET"])
@require_auth
def api_audio():
    """Set EQ preset / normalization / crossfade (fade) seconds."""
    try:
        eq = request.args.get("eq")
        norm = request.args.get("normalize")
        cross = request.args.get("crossfade")
        settings = player_manager.set_audio(
            eq=eq,
            normalize=(norm in ("1", "true", "on")) if norm is not None else None,
            crossfade=int(cross) if cross is not None else None,
        )
        return jsonify({"status": "ok", **settings})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/announce", methods=["GET"])
@require_auth
def api_announce():
    """Toggle spoken 'now playing' announcements."""
    try:
        val = request.args.get("enabled")
        enabled = (val in ("1", "true", "on")) if val is not None else True
        return jsonify({"status": "ok", "announce": player_manager.set_announce(enabled)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/sleep", methods=["GET"])
@require_auth
def api_sleep():
    """Start/cancel a sleep timer (minutes; 0 cancels)."""
    try:
        minutes = int(request.args.get("minutes") or 0)
        return jsonify({"status": "ok", **player_manager.set_sleep_timer(minutes)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/download", methods=["GET"])
@require_auth
def api_download():
    """Save the currently-playing track to the user's Music folder."""
    try:
        return jsonify({"status": "ok", **player_manager.export_current()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/settings", methods=["GET"])
@require_auth
def api_settings():
    """Return persisted audio/player settings."""
    try:
        return jsonify({"status": "ok", "source": _runtime_source[0],
                        "start_on_boot": _get_boot(),
                        "lock_ips": bool(_config.get("lock_ips", False)),
                        **player_manager.get_settings()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/lockips", methods=["GET"])
@require_auth
def api_lockips():
    """Toggle the IP allow-list. Off (default) lets any LAN device connect."""
    try:
        from auth import save_config_value
        val = request.args.get("enabled")
        enabled = (val in ("1", "true", "on")) if val is not None \
            else not bool(_config.get("lock_ips", False))
        save_config_value("lock_ips", bool(enabled))
        _config["lock_ips"] = bool(enabled)
        return jsonify({"status": "ok", "lock_ips": bool(enabled)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/seek", methods=["GET"])
@require_auth
def api_seek():
    """Seek to an absolute position (seconds)."""
    try:
        return jsonify({"status": "ok", **player_manager.seek(request.args.get("pos", 0))})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/liked", methods=["GET"])
@require_auth
def api_liked():
    """Return the persistent liked-songs list."""
    try:
        return jsonify({"status": "ok", "liked": player_manager.get_liked()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


_BOOT_NAME = "MusicRequestServer"
_BOOT_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _boot_command():
    # Frozen build: the .exe is itself the launcher.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    py = _config.get("python_path") or sys.executable
    pyw = py.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = py
    launcher = os.path.join(os.path.dirname(os.path.dirname(__file__)), "launcher.pyw")
    return f'"{pyw}" "{launcher}" --hidden'


def _get_boot():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BOOT_RUN_KEY) as k:
            winreg.QueryValueEx(k, _BOOT_NAME)
        return True
    except Exception:
        return False


def _set_boot(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BOOT_RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, _BOOT_NAME, 0, winreg.REG_SZ, _boot_command())
        else:
            try:
                winreg.DeleteValue(k, _BOOT_NAME)
            except FileNotFoundError:
                pass


@app.route("/api/boot", methods=["GET"])
@require_auth
def api_boot():
    """Enable/disable launching on Windows sign-in (hidden to the tray)."""
    try:
        val = request.args.get("enabled")
        enabled = (val in ("1", "true", "on")) if val is not None else not _get_boot()
        _set_boot(enabled)
        return jsonify({"status": "ok", "start_on_boot": _get_boot()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/source", methods=["GET"])
@require_auth
def api_source():
    """Set the default music source for future requests."""
    try:
        val = (request.args.get("value") or "youtube").lower()
        if val not in ("youtube", "soundcloud", "bandcamp"):
            val = "youtube"
        _runtime_source[0] = val
        return jsonify({"status": "ok", "source": val})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/control/<action>", methods=["GET"])
@require_auth
def api_control(action):
    """Send a transport control command."""
    try:
        value = request.args.get("value")
        result = player_manager.control(action, value)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/ping", methods=["GET"])
def api_ping():
    # Unauthenticated health check (no sensitive data) so the launcher can
    # detect the server is up without the key.
    return jsonify({"status": "ok", "uptime": time.monotonic()})


@app.route("/player")
def player():
    """The desktop player UI (shown in the launcher's webview flyout)."""
    return render_template("player.html", api_key=_config.get("api_key", ""))


@app.route("/", methods=["GET", "POST"])
def index():
    """iOS-Shortcut endpoint + setup help.

    The shortcut POSTs JSON ``{"input": "<dictated text>"}`` (with the key in
    the ``?key=`` query). A browser GET shows how to build that shortcut.
    """
    if request.method == "POST":
        if not check_ip() or not check_api_key():
            return jsonify({"status": "error", "message": "Not authorised"}), 403
        data = request.get_json(silent=True) or {}
        query = (data.get("input") or data.get("q")
                 or request.form.get("input") or request.values.get("input") or "")
        return jsonify(_run_play(query=query))
    return render_template("setup.html", host=_lan_address(),
                           port=_config.get("port", 5000),
                           api_key=_config.get("api_key", ""))


# ── Main ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = get_config()

    # Run startup sequence
    startup()

    host = config.get("host", "0.0.0.0")
    port = config.get("port", 5000)

    print(f"Listening on {host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)