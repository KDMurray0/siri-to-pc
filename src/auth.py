"""Authentication and authorization for iTunes Request Server."""

import hmac
import os
from flask import request, jsonify


def load_config():
    """Load configuration from config.json."""
    import json
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)


_config = None


def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config():
    global _config
    _config = load_config()


def check_ip():
    """Check if the requesting IP is allowed. Returns True if allowed."""
    config = get_config()
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