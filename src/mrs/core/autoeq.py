"""Headphone correction from AutoEq, picked by what the device calls itself.

AutoEq (github.com/jaakkopasanen/AutoEq) publishes a parametric EQ for about
nine thousand headphone measurements, each fitted to the Harman target. The
filters are RBJ biquads, which is exactly what ffmpeg's equalizer, lowshelf
and highshelf are — checked against an impulse to within 0.13 dB.

Windows names a Bluetooth output after the product ("Headphones (WH-1000XM4)"),
so that name is looked up in AutoEq's index and, when the match is certain,
applied without asking. A wired jack says only "High Definition Audio Device"
and matches nothing, which is correct: it could be anything plugged in. Then
the owner picks the model once and it is kept against that output's name.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import config
from ..logging_setup import get
from ..paths import data_dir, write_atomic

log = get("autoeq")

BASE = "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results/"
UA = {"User-Agent": "MusicRequestServer/2.0 (personal music player)"}
REFRESH = 7 * 86400
MAX_PROFILE_FILES = 128

# AutoEq's own advice on whose measurement to trust when there are several.
_SOURCE_RANK = {"oratory1990": 0, "crinacle": 1, "Innerfidelity": 2,
                "Rtings": 3, "Headphone.com Legacy": 4}

# Outputs whose names say nothing about what's plugged into them.
_GENERIC = re.compile(
    r"high definition audio|realtek|usb audio|codec|nvidia|\bamd\b|intel|"
    r"hdmi|s/pdif|spdif|digital (audio|output)|steam streaming|virtual|"
    r"voicemeeter|vb-audio|cable (input|output)|nahimic|display audio|"
    r"internal aux|\baux\b|line out|mirroring", re.I)
_ROLE = re.compile(r"^\s*(headphones?|headset|earphones?|speakers?|earbuds?)\s*"
                   r"\((.+)\)\s*$", re.I)
_NOISE = re.compile(r"\b(hands-?free|ag audio|hf audio|stereo|a2dp|le audio)\b", re.I)

_lock = threading.RLock()
_entries: list[dict] | None = None
_by_id: dict[str, dict] = {}


def _dir() -> Path:
    p = data_dir() / "autoeq"
    (p / "profiles").mkdir(parents=True, exist_ok=True)
    return p


def _fetch(url: str, timeout: float = 10.0) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=timeout) as r:
            return r.read()
    except Exception as exc:
        log.info("autoeq fetch failed: %s", exc)
        return None


# -- the index -----------------------------------------------------------

_LINE = re.compile(r"^- \[(?P<name>.+?)\]\(\./(?P<path>.+)\) by (?P<by>.+?)\s*$")


def parse_index(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        path = m["path"]
        if ".." in path or path.startswith("/") or "\\" in path:
            continue
        by = m["by"]
        source, _, rig = by.partition(" on ")
        parts = urllib.parse.unquote(path).split("/")
        form = parts[1] if len(parts) > 2 else ""
        form = ("in-ear" if "in-ear" in form else "earbud" if "earbud" in form
                else "over-ear" if "ear" in form else form)
        out.append({
            "id": hashlib.sha1(path.encode()).hexdigest()[:12],
            "name": m["name"], "path": path, "source": source.strip(),
            "rig": rig.strip(), "form": form,
        })
    return out


def _load(refresh: bool = False) -> list[dict]:
    global _entries, _by_id
    with _lock:
        if _entries is not None and not refresh:
            return _entries
        cached = _dir() / "index.md"
        text = ""
        stale = (not cached.is_file()
                 or time.time() - cached.stat().st_mtime > REFRESH)
        if stale or refresh:
            got = _fetch(BASE + "INDEX.md", timeout=20)
            if got and b"- [" in got:
                text = got.decode("utf-8", "replace")
                write_atomic(cached, text)
        if not text and cached.is_file():
            text = cached.read_text("utf-8", "replace")
        _entries = parse_index(text)
        _by_id = {e["id"]: e for e in _entries}
        return _entries


def entry(entry_id: str) -> dict | None:
    _load()
    return _by_id.get(entry_id or "")


def cached_entry(entry_id: str) -> dict | None:
    """An entry already resident in this process, without fetching the index."""
    return _by_id.get(entry_id or "")


def _rank(e: dict) -> tuple:
    return (_SOURCE_RANK.get(e["source"], 9), e["name"].count("("), len(e["name"]))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def search(query: str, limit: int = 25) -> list[dict]:
    """Every word typed, in the model's name; best-trusted measurement first."""
    words = _tokens(query)
    if not words:
        return []
    best: dict[str, dict] = {}
    for e in _load():
        have = _tokens(e["name"])
        compact = _compact(e["name"])
        ok = all(any(h.startswith(w) for h in have) or w in compact for w in words)
        if not ok:
            continue
        seen = best.get(e["name"].lower())
        if seen is None or _rank(e) < _rank(seen):
            best[e["name"].lower()] = e
    rows = sorted(best.values(),
                  key=lambda e: (0 if _compact(query) == _compact(e["name"]) else 1,
                                 len(e["name"]), _rank(e)))
    return [public(e) for e in rows[:limit]]


def public(e: dict) -> dict:
    return {"id": e["id"], "name": e["name"], "source": e["source"],
            "rig": e["rig"], "form": e["form"]}


def model_in(device_name: str) -> str:
    """The part of an output's name that could be a product, or ""."""
    name = (device_name or "").strip()
    m = _ROLE.match(name)
    inner = m.group(2) if m else name
    if _GENERIC.search(inner):
        return ""
    inner = re.sub(r"^.*?['’]s\s+", "", inner)      # "Kyle's AirPods Pro"
    inner = _NOISE.sub(" ", inner)
    inner = re.sub(r"\s+", " ", inner).strip(" -")
    return inner if len(_compact(inner)) >= 4 else ""


def match(device_name: str) -> tuple[dict | None, bool]:
    """(entry, certain) for an output name. Certain means apply it unasked."""
    model = model_in(device_name)
    if not model:
        return None, False
    # Bose calls the QuietComfort a QC on the device and not in AutoEq.
    model = re.sub(r"\bQC\s*(?=\d)", "QuietComfort ", model, flags=re.I)
    want = _compact(model)
    has_digit = any(c.isdigit() for c in want)
    scored = []
    for e in _load():
        have = _compact(e["name"])
        if have == want:
            score = 100
        elif (have.endswith(want) or want.endswith(have)) and \
                min(len(have), len(want)) >= (5 if has_digit else 8):
            score = 90                      # brand left off, or added
        else:
            continue
        scored.append((-score, _rank(e), e))
    if not scored:
        return None, False
    scored.sort(key=lambda s: (s[0], s[1]))
    top = scored[0][2]
    # A later model whose name extends this one is a different product that
    # may well report the same name: AirPods Pro 2 calls itself "AirPods Pro".
    # A bracketed variant ("(ANC off)") is the same product measured another
    # way, and doesn't count.
    base = _compact(_bare(top["name"]))
    for e in _load():
        other = _compact(_bare(e["name"]))
        if other != base and other.startswith(base):
            return top, False
    rivals = {_compact(_bare(s[2]["name"])) for s in scored if s[0] == scored[0][0]}
    return top, len(rivals) == 1


def _bare(name: str) -> str:
    return re.sub(r"\([^)]*\)", "", name or "").strip()


# -- profiles ------------------------------------------------------------

_FILTER = re.compile(r"Filter\s+\d+:\s+ON\s+(PK|LSC|HSC)\s+Fc\s+([\d.]+)\s+Hz\s+"
                     r"Gain\s+(-?[\d.]+)\s+dB\s+Q\s+([\d.]+)", re.I)
_PREAMP = re.compile(r"Preamp:\s*(-?[\d.]+)\s*dB", re.I)


def parse_profile(text: str) -> dict:
    pre = _PREAMP.search(text or "")
    filters = []
    for kind, f, g, q in _FILTER.findall(text or "")[:20]:
        filters.append({
            "type": kind.upper(),
            "f": max(10.0, min(22000.0, float(f))),
            "gain": max(-30.0, min(30.0, float(g))),
            "q": max(0.05, min(20.0, float(q))),
        })
    preamp = max(-30.0, min(0.0, float(pre.group(1)))) if pre else 0.0
    return {"preamp": preamp, "filters": filters}


def _profile_file(entry_id: str) -> Path:
    return _dir() / "profiles" / f"{re.sub(r'[^a-f0-9]', '', entry_id)}.txt"


def _prune_profiles(keep: Path) -> None:
    """Keep an abusive or very old client from growing this cache forever."""
    try:
        rows = sorted((p for p in (_dir() / "profiles").glob("*.txt")
                       if p.is_file()), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        for stale in rows[MAX_PROFILE_FILES:]:
            if stale != keep:
                stale.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("couldn't prune AutoEq profiles: %s", exc)


def profile(entry_id: str, fetch: bool = True) -> dict | None:
    """The filters for an entry, from disk, or GitHub the first time."""
    path = _profile_file(entry_id)
    if path.is_file():
        got = parse_profile(path.read_text("utf-8", "replace"))
        return got if got["filters"] else None
    if not fetch:
        return None
    e = entry(entry_id)
    if not e:
        return None
    folder = urllib.parse.unquote(e["path"]).rstrip("/")
    leaf = folder.split("/")[-1]
    url = BASE + urllib.parse.quote(f"{folder}/{leaf} ParametricEQ.txt")
    raw = _fetch(url)
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    got = parse_profile(text)
    if not got["filters"]:
        return None
    write_atomic(path, text)
    _prune_profiles(path)
    return got


def chain(prof: dict | None) -> str:
    """As an ffmpeg/mpv filter string. Preamp first, so boosts can't clip."""
    if not prof or not prof.get("filters"):
        return ""
    name = {"PK": "equalizer", "LSC": "lowshelf", "HSC": "highshelf"}
    parts = [f"volume=volume={prof['preamp']:.2f}dB"] if prof.get("preamp") else []
    for flt in prof["filters"]:
        parts.append(f"{name[flt['type']]}=f={flt['f']:.1f}:width_type=q:"
                     f"width={flt['q']:.3f}:g={flt['gain']:.2f}")
    return ",".join(parts)


def cached_chain(entry_id: str) -> str:
    """Only what's on disk. For code paths that must not wait on the network."""
    return chain(profile(entry_id, fetch=False))


def cached_tunes() -> list[str]:
    """aeq-<id> for every profile on disk, so their phone transcodes are kept."""
    return [f"aeq-{p.stem}" for p in (_dir() / "profiles").glob("*.txt")]


# -- which output, and what it's set to ------------------------------------

def output_name() -> str:
    """What mpv is playing through, by name."""
    from . import endpoint
    dev = str(config.get("audio_device") or "auto")
    if dev == "auto":
        return endpoint.default_output()["name"]
    if dev.startswith("wasapi/"):
        return endpoint.output_named(dev[len("wasapi/"):]) or \
            str(config.get("audio_device_label") or "")
    if dev.startswith("cast:"):
        return ""                        # a phone is the speaker; not ours to tune
    return str(config.get("audio_device_label") or "")


def assigned(device_name: str) -> dict:
    rows = config.get("device_eq") or {}
    got = rows.get(device_name) if isinstance(rows, dict) else None
    return got if isinstance(got, dict) else {}


def assign(device_name: str, entry_id: str, auto: bool = False) -> dict:
    """Keep a choice against an output's name. "" means "this one: nothing"."""
    rows = dict(config.get("device_eq") or {})
    if entry_id:
        e = entry(entry_id)
        if not e:
            raise ValueError("unknown AutoEq entry")
        rows[device_name] = {"id": e["id"], "name": e["name"],
                             "source": e["source"], "auto": bool(auto)}
    else:
        rows[device_name] = {"id": "", "auto": False}
    config.set("device_eq", rows)
    return rows[device_name]


def pc_chain() -> str:
    """The correction for whatever the PC is playing through right now."""
    if not config.get("device_eq_enabled", True):
        return ""
    name = _Watch.last or ""
    row = assigned(name) if name else {}
    return cached_chain(row.get("id", "")) if row.get("id") else ""


class _Watch:
    """Notices the output changing and follows it."""
    last: str | None = None
    _thread: threading.Thread | None = None

    @classmethod
    def start(cls, on_change) -> None:
        if cls._thread and cls._thread.is_alive():
            return
        cls._thread = threading.Thread(target=cls._run, args=(on_change,),
                                       daemon=True, name="outputs")
        cls._thread.start()

    @classmethod
    def _run(cls, on_change) -> None:
        while True:
            try:
                name = output_name()
                if name != cls.last:
                    cls.last = name
                    log.info("playing through %r", name)
                    settle(name)
                    on_change()
            except Exception as exc:
                log.debug("output watch: %s", exc)
            time.sleep(3)


def settle(name: str) -> None:
    """First time an output is seen: match it, and fetch what it needs."""
    if not name:
        return
    row = assigned(name)
    if row.get("id"):
        profile(row["id"])               # make sure it's on disk
        return
    if row or not config.get("device_eq_auto", True):
        return                           # owner said none, or matching is off
    found, certain = match(name)
    if found and certain and profile(found["id"]):
        assign(name, found["id"], auto=True)
        log.info("matched %r to %s (%s)", name, found["name"], found["source"])


def watch(on_change) -> None:
    _Watch.start(on_change)


def refresh_now(on_change) -> None:
    """Re-read the output at once, e.g. after the owner picked a model."""
    _Watch.last = output_name()
    on_change()


def status() -> dict:
    name = _Watch.last if _Watch.last is not None else output_name()
    row = assigned(name) if name else {}
    suggestion = None
    if name and not row.get("id"):
        try:
            found, certain = match(name)
            suggestion = public(found) if found else None
        except Exception:
            suggestion = None
    return {"device": name, "model": model_in(name) if name else "",
            "enabled": bool(config.get("device_eq_enabled", True)),
            "auto": bool(config.get("device_eq_auto", True)),
            "profile": row if row.get("id") else None,
            "chosen_none": bool(row) and not row.get("id"),
            "suggestion": suggestion,
            "curve": curve(profile(row["id"], fetch=False)) if row.get("id") else []}


def curve(prof: dict | None, points: int = 64) -> list[list[float]]:
    """[freq, dB] of a profile's response, for drawing. RBJ, same as ffmpeg."""
    if not prof or not prof.get("filters"):
        return []
    import cmath
    import math
    fs = 48000.0
    out = []
    for i in range(points):
        f = 20.0 * (1000.0 ** (i / (points - 1)))          # 20Hz..20kHz
        z = cmath.exp(-1j * 2 * math.pi * f / fs)
        h = 1 + 0j
        for flt in prof["filters"]:
            b, a = _rbj(flt["type"], flt["f"], flt["gain"], flt["q"], fs)
            h *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
        # Shape only. The preamp moves the whole line down and says nothing
        # about what the correction does.
        out.append([round(f, 1), round(20 * math.log10(abs(h)), 2)])
    return out


def _rbj(kind: str, f: float, gain: float, q: float, fs: float):
    import math
    A = 10 ** (gain / 40)
    w = 2 * math.pi * f / fs
    cw, sw = math.cos(w), math.sin(w)
    al = sw / (2 * q)
    if kind == "PK":
        b = [1 + al * A, -2 * cw, 1 - al * A]
        a = [1 + al / A, -2 * cw, 1 - al / A]
    else:
        s = 2 * math.sqrt(A) * al
        if kind == "LSC":
            b = [A * ((A + 1) - (A - 1) * cw + s), 2 * A * ((A - 1) - (A + 1) * cw),
                 A * ((A + 1) - (A - 1) * cw - s)]
            a = [(A + 1) + (A - 1) * cw + s, -2 * ((A - 1) + (A + 1) * cw),
                 (A + 1) + (A - 1) * cw - s]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + s), -2 * A * ((A - 1) + (A + 1) * cw),
                 A * ((A + 1) + (A - 1) * cw - s)]
            a = [(A + 1) - (A - 1) * cw + s, 2 * ((A - 1) - (A + 1) * cw),
                 (A + 1) - (A - 1) * cw - s]
    return [x / a[0] for x in b], [x / a[0] for x in a]
