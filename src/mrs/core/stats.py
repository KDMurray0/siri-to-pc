"""What this server has actually done: per link, and for the house.

Every count lands twice — against the link that caused it and against the
house — so "what has this place played this month" and "what has this link
done all year" are both one lookup rather than a sum over passes that may
since have been revoked and forgotten.

Counts are kept in memory and written every half minute. A byte counter
updated on disk per range request would write thousands of times a song.
"""

from __future__ import annotations

import json
import threading
import time

from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("stats")

# requests: songs asked for. plays: tracks started. seconds: listened.
# bytes_out: served to devices. bytes_in/downloads: fetched from the web.
KEYS = ("requests", "plays", "seconds", "bytes_out", "bytes_in", "downloads")
HOUSE = "house"
MONTHS_KEPT = 24
FLUSH_EVERY = 30.0

_lock = threading.RLock()
_pending: dict[str, dict[str, float]] = {}
_names: dict[str, str] = {}
_due = 0.0


def _empty() -> dict:
    return {k: 0 for k in KEYS}


def month_of(when: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(when or time.time()))


def _path():
    return data_dir() / "stats.json"


def _blank() -> dict:
    return {"version": 1, "house": {"totals": _empty(), "months": {}},
            "links": {}}


def _read() -> dict:
    try:
        raw = json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(raw, dict):
        return _blank()
    out = _blank()
    house = raw.get("house")
    if isinstance(house, dict):
        out["house"] = _clean(house)
    links = raw.get("links")
    if isinstance(links, dict):
        for pid, row in links.items():
            if isinstance(pid, str) and isinstance(row, dict):
                out["links"][pid[:64]] = _clean(row)
    return out


def _clean(row: dict) -> dict:
    """One record, with anything odd in the file dropped rather than trusted."""
    got = {"totals": _empty(), "months": {},
           "first": int(row.get("first") or 0), "last": int(row.get("last") or 0)}
    if isinstance(row.get("name"), str):
        got["name"] = row["name"][:60]
    totals = row.get("totals")
    if isinstance(totals, dict):
        for k in KEYS:
            try:
                got["totals"][k] = max(0, int(totals.get(k, 0)))
            except (TypeError, ValueError):
                pass
    months = row.get("months")
    if isinstance(months, dict):
        for key, bucket in sorted(months.items())[-MONTHS_KEPT:]:
            if not isinstance(key, str) or not isinstance(bucket, dict):
                continue
            keep = _empty()
            for k in KEYS:
                try:
                    keep[k] = max(0, int(bucket.get(k, 0)))
                except (TypeError, ValueError):
                    pass
            got["months"][key[:7]] = keep
    return got


def note(who: str = HOUSE, *, name: str = "", **counts) -> None:
    """Add to a link's totals and the house's. Nothing is written yet."""
    global _due
    adds = {k: float(v) for k, v in counts.items() if k in KEYS and v}
    if not adds:
        return
    now = time.time()
    with _lock:
        for key in ({who, HOUSE} if who else {HOUSE}):
            acc = _pending.setdefault(key, {})
            for k, v in adds.items():
                acc[k] = acc.get(k, 0.0) + v
        if name and who and who != HOUSE:
            _names[who] = name[:60]
        if not _due:
            _due = now + FLUSH_EVERY
        due = _due <= now
    if due:
        flush()


def flush() -> None:
    """Fold what's pending into the file. Safe to call at any time."""
    global _due
    with _lock:
        pending = dict(_pending)
        names = dict(_names)
        _pending.clear()
        _names.clear()
        _due = 0.0
        if not pending:
            return
        data = _read()
        stamp = int(time.time())
        key = month_of(stamp)
        for who, adds in pending.items():
            row = data["house"] if who == HOUSE else \
                data["links"].setdefault(who, _clean({}))
            bucket = row["months"].setdefault(key, _empty())
            for k, v in adds.items():
                row["totals"][k] = int(row["totals"].get(k, 0) + v)
                bucket[k] = int(bucket.get(k, 0) + v)
            if not row.get("first"):
                row["first"] = stamp
            row["last"] = stamp
            if who in names:
                row["name"] = names[who]
            # Older than two years is history nobody asked for.
            for old in sorted(row["months"])[:-MONTHS_KEPT]:
                row["months"].pop(old, None)
        try:
            write_atomic(_path(), json.dumps(data))
        except OSError as exc:
            log.warning("couldn't write the stats: %s", exc)


def _merged(row: dict, who: str) -> dict:
    """A record with whatever hasn't been written yet folded in."""
    got = {"totals": dict(row["totals"]),
           "months": {k: dict(v) for k, v in row["months"].items()},
           "first": row.get("first", 0), "last": row.get("last", 0)}
    if row.get("name"):
        got["name"] = row["name"]
    adds = _pending.get(who)
    if adds:
        bucket = got["months"].setdefault(month_of(), _empty())
        for k, v in adds.items():
            got["totals"][k] = int(got["totals"].get(k, 0) + v)
            bucket[k] = int(bucket.get(k, 0) + v)
        got["last"] = int(time.time())
    return got


def house(months: int = 6) -> dict:
    """Everything this server has done, and the last few months of it."""
    with _lock:
        row = _merged(_read()["house"], HOUSE)
    keys = sorted(row["months"])[-max(1, months):]
    return {"totals": row["totals"], "first": row["first"], "last": row["last"],
            "this_month": row["months"].get(month_of(), _empty()),
            "months": [{"month": k, **row["months"][k]} for k in keys]}


def link(pass_id: str, months: int = 3) -> dict:
    """One link's own record, kept even after the link itself is gone."""
    with _lock:
        rows = _read()["links"]
        row = _merged(rows.get(pass_id) or _clean({}), pass_id)
    keys = sorted(row["months"])[-max(1, months):]
    return {"id": pass_id, "name": row.get("name", ""), "totals": row["totals"],
            "first": row["first"], "last": row["last"],
            "this_month": row["months"].get(month_of(), _empty()),
            "months": [{"month": k, **row["months"][k]} for k in keys]}


def links() -> list[dict]:
    """Every link that ever did anything, busiest first."""
    with _lock:
        rows = _read()["links"]
        out = [_merged(row, pid) | {"id": pid} for pid, row in rows.items()]
        for pid in _pending:
            if pid not in rows and pid != HOUSE:
                out.append(_merged(_clean({}), pid) | {"id": pid})
    for row in out:
        row["this_month"] = row["months"].get(month_of(), _empty())
        row["months"] = [{"month": k, **v} for k, v in sorted(row["months"].items())]
    out.sort(key=lambda r: (-r["totals"]["seconds"], -r["totals"]["requests"]))
    return out
