"""Authentication and authorization for iTunes Request Server."""

import hmac
import json
import os
import secrets
from flask import request, jsonify

_PLACEHOLDER_KEY = "CHANGE-THIS-TO-A-LONG-RANDOM-SECRET"

# Written on first run when no config.json exists. Secret filled in below.
_DEFAULT_CONFIG = {
    "api_key": "",
    "allowed_ips": [],
    "lock_ips": False,
    "host": "0.0.0.0",
    "port": 5000,
    "cookies_from_browser": "",
    "cookies_file": "",
    "js_runtime": "node",
    "player_client": "tv",
    "ytdl_raw_options": [],
    "announce": True,
    "tts_voice": "en-US-AriaNeural",
    "auto_queue": True,
    "auto_queue_batch": 5,
    "auto_queue_threshold": 2,
    "history_size": 100,
    "source": "youtube",
    "use_groq": False,
    "groq_api_key": "",
    "groq_model": "llama-3.3-70b-versatile",
}


def ensure_config():
    # Load config.json; create it and/or a random api_key if missing, then save.
    from paths import config_path
    path = config_path()
    changed = False
    if os.path.isfile(path):
        with open(path, "r") as f:
            cfg = json.load(f)
    else:
        cfg = dict(_DEFAULT_CONFIG)
        changed = True

    key = (cfg.get("api_key") or "").strip()
    if not key or key == _PLACEHOLDER_KEY:
        cfg["api_key"] = secrets.token_urlsafe(24)
        changed = True

    if changed:
        try:
            with open(path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass
    return cfg


def load_config():
    """Load configuration from config.json (auto-creating key/file as needed)."""
    return ensure_config()


_config = None


def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config():
    global _config
    _config = load_config()


def save_config_value(key, value):
    """Persist a single setting to config.json and update the in-memory copy."""
    from paths import config_path
    cfg = get_config()
    cfg[key] = value
    try:
        with open(config_path(), "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return cfg


def check_ip():
    """Check if the requesting IP is allowed. Returns True if allowed."""
    config = get_config()

    # IP lock explicitly disabled -> any device on the LAN may connect.
    if config.get("lock_ips") is False:
        return True

    allowed_ips = config.get("allowed_ips", [])

    # Empty list means allow all
    if not allowed_ips:
        return True

    client_ip = request.remote_addr

    # Handle IPv6-mapped IPv4 addresses
    if client_ip and "::ffff:" in client_ip:
        client_ip = client_ip.split("::ffff:")[-1]

    return client_ip in allowed_ips


def check_api_key():
    """Check API key from query parameter or header. Returns True if valid."""
    config = get_config()
    api_key = config.get("api_key", "")

    if not api_key:
        # No key configured means no authentication required
        return True

    # Check header first
    header_key = request.headers.get("X-Api-Key", "")
    if header_key and hmac.compare_digest(header_key, api_key):
        return True

    # Check query parameter using request.args.get() — safe, returns str or None
    query_key = request.args.get("key")
    if query_key and hmac.compare_digest(query_key, api_key):
        return True

    return False


def strip_api_key(text):
    """Strip API key from a string for safe logging."""
    config = get_config()
    api_key = config.get("api_key", "")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
        text = text.replace(
            hmac.new(api_key.encode(), api_key.encode(), digestmod="sha256").hexdigest(),
            "[REDACTED]"
        )
    return text


def require_auth(f):
    """Decorator to require authentication on an endpoint."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_ip():
            return jsonify({"status": "error", "message": "Not authorised"}), 403
        if not check_api_key():
            return jsonify({"status": "error", "message": "Not authorised"}), 403
        return f(*args, **kwargs)

    return decorated_function


def unauthorised_response():
    """Return a standard unauthorised response."""
    return jsonify({"status": "error", "message": "Not authorised"}), 403