"""Bounded owner audit trail; never store request bodies or credentials."""
import json
import threading
import time
from ..paths import data_dir, write_atomic
from ..config import config

_lock = threading.RLock()


def entries():
    try:
        rows = json.loads((data_dir() / "audit.json").read_text("utf-8"))
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)
                and isinstance(r.get("at"), (int, float))
                and isinstance(r.get("action"), str)
                and isinstance(r.get("actor"), str)]
    except (OSError, ValueError):
        return []


def record(action, actor="owner", status=200):
    with _lock:
        cutoff = time.time() - max(1, min(365, int(config.get("audit_log_days", 30)))) * 86400
        rows = [r for r in entries() if r["at"] >= cutoff][-999:]
        rows.append({"at": time.time(), "action": str(action)[:120],
                     "actor": str(actor)[:40], "status": int(status)})
        write_atomic(data_dir() / "audit.json", json.dumps(rows))
