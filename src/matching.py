"""Matching and validation logic for Music Request Server.

Given a raw spoken or typed query, clean it up, detect transport commands,
parse the Alexa-style grammar, and build a search plan for ``search.py``.
"""

import re


# ── Phrase grammar table ──────────────────────────────────────────
# Each row: (regex, type, sets_shuffle, has_artist_group)
# Regex is compiled case-insensitively at load time.

GRAMMAR_ROWS = [
    # ── forces artist ────────────────────────────────────────
    (r'play\s+songs?\s+by\s+(.+)',           'artist', None, False),
    (r'play\s+music\s+by\s+(.+)',            'artist', None, False),
    (r'play\s+tracks?\s+by\s+(.+)',          'artist', None, False),
    (r'play\s+stuff\s+by\s+(.+)',            'artist', None, False),
    (r'play\s+everything\s+by\s+(.+)',       'artist', None, False),
    (r'play\s+all\s+of\s+(.+)',              'artist', None, False),
    (r'play\s+anything\s+by\s+(.+)',         'artist', None, False),
    (r'play\s+artist\s+(.+)',                'artist', None, False),
    (r'play\s+(?:the\s+)?band\s+(.+)',       'artist', None, False),
    # ── ambiguous: smart resolver decides artist vs genre/mood/song ──
    (r'play\s+some\s+(.+)',                  None, None, False),
    (r'play\s+(.+)\s+radio',                 None, True, False),
    (r'shuffle\s+(.+)',                      None, True, False),

    # ── forces album ─────────────────────────────────────────
    (r'play\s+the\s+album\s+(.+?)\s+by\s+(.+)', 'album', None, True),
    (r'play\s+the\s+album\s+(.+)',           'album', None, False),
    (r'play\s+album\s+(.+)',                 'album', None, False),
    (r'play\s+the\s+(.+?)\s+album',          'album', None, False),
    (r'play\s+the\s+record\s+(.+)',          'album', None, False),
    (r'play\s+the\s+lp\s+(.+)',             'album', None, False),
    (r'play\s+(.+?)\s+the\s+album',         'album', None, False),

    # ── forces song ──────────────────────────────────────────
    (r'play\s+the\s+song\s+(.+?)\s+by\s+(.+)', 'song', None, True),
    (r'play\s+the\s+song\s+(.+)',            'song', None, False),
    (r'play\s+song\s+(.+)',                  'song', None, False),
    (r'play\s+the\s+track\s+(.+)',           'song', None, False),
    (r'play\s+the\s+single\s+(.+)',          'song', None, False),

    # ── neutral: play X by Y ─────────────────────────────────
    (r'play\s+(.+?)\s+by\s+(.+)',            None, None, True),

    # ── neutral: play X ──────────────────────────────────────
    (r'play\s+(.+)',                          None, None, False),
]

# Compile regexes
GRAMMAR = []
for pattern, typ, shuffle_hint, has_artist in GRAMMAR_ROWS:
    compiled = re.compile(pattern, re.IGNORECASE)
    GRAMMAR.append({
        'regex': compiled,
        'type': typ,
        'shuffle': shuffle_hint,
        'has_artist': has_artist,
    })

# ── Transport command phrases ─────────────────────────────────────

TRANSPORT_PHRASES = [
    ("pause",       "stop"),
    ("pause",       "stop the music"),
    ("pause",       "stop playing"),
    ("pause",       "pause"),
    ("pause",       "pause the music"),
    ("pause",       "hold on"),
    ("resume",      "resume"),
    ("resume",      "unpause"),
    ("resume",      "carry on"),
    ("resume",      "continue"),
    ("resume",      "keep playing"),
    ("next",        "skip"),
    ("next",        "next"),
    ("next",        "next track"),
    ("next",        "skip this"),
    ("previous",    "back"),
    ("previous",    "previous"),
    ("previous",    "go back"),
    ("previous",    "previous track"),
]

# Trailing words trimmed from a captured name ("play jazz music" -> "jazz").
_THEME_TRAILERS = re.compile(
    r'\s+(music|songs?|tracks?|tunes?|beats|playlist|vibes?|mix|hits|classics)$',
    re.IGNORECASE,
)


def strip_trailers(name):
    """Trim trailing 'music'/'songs'/'vibes'… so search terms stay clean."""
    if not name:
        return name
    n = name.strip()
    prev = None
    while prev != n:
        prev = n
        n = _THEME_TRAILERS.sub('', n).strip()
    return n or name.strip()


# ── Modifiers that can appear anywhere in the phrase ──────────────

SHUFFLE_ON_RE = re.compile(r'\b(shuffle|shuffled)\b', re.IGNORECASE)
SHUFFLE_OFF_RE = re.compile(r'\b(in\s+order|start\s+to\s+finish)\b', re.IGNORECASE)
MODE_NEXT_RE = re.compile(r'\b(next|after\s+this)\b', re.IGNORECASE)
MODE_QUEUE_RE = re.compile(r'\b(add|queue)\b', re.IGNORECASE)

# ── Dictation noise ──────────────────────────────────────────────

DICTATION_FILLER_RE = re.compile(
    r'\s+(please|for\s+me|now|on\s+the\s+speakers|in\s+the\s+kitchen|on\s+speakers)\s*$',
    re.IGNORECASE,
)

DICTATION_PUNCT = {
    'dot': '.',
    'hyphen': '-',
    'dash': '-',
    'apostrophe': "'",
    'exclamation mark': '!',
    'exclamation': '!',
}

SPELLING_TO_NUMBER = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
    'eighteen': '18', 'nineteen': '19', 'twenty': '20',
    'twenty-one': '21', 'twenty one': '21', 'thirty': '30', 'forty': '40',
    'fifty': '50', 'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90',
    'hundred': '100', 'thousand': '1000',
}


# ── Normalisation ────────────────────────────────────────────────

def normalise(text):
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s&]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# Leading wake-words / politeness stripped before parsing.
_WAKE_RE = re.compile(
    r'^\s*(?:hey\s+|ok\s+|okay\s+|yo\s+)?'
    r'(?:siri|google|alexa|computer|assistant)?[,\s]*'
    r'(?:can\s+you|could\s+you|would\s+you|please|will\s+you|i\s+want\s+you\s+to)?\s*',
    re.IGNORECASE,
)

# Phrases meaning "play" that should be normalised to the verb "play".
_PLAY_SYNONYMS = re.compile(
    r'^\s*(?:'
    r'put\s+on|throw\s+on|throw\s+me\s+on|pop\s+on|start\s+playing|start|'
    r'give\s+me|gimme|get\s+me|lemme\s+hear|let\s+me\s+hear|'
    r"let'?s\s+hear|let'?s\s+listen\s+to|listen\s+to|"
    r'i\s+wanna\s+hear|i\s+want\s+to\s+hear|i\s+wanna\s+listen\s+to|'
    r'i\s+want\s+to\s+listen\s+to|i\s+feel\s+like|how\s+about|'
    r'can\s+i\s+get|can\s+i\s+hear'
    r')\s+',
    re.IGNORECASE,
)


def normalise_query(text):
    """Normalise incoming dictated/typed query text.

    Runs dictation-specific cleanups before the grammar.
    """
    if not text:
        return ""

    # Strip leading wake-words / politeness, then map "play" synonyms to "play"
    # so the grammar (which keys on "play …") catches casual phrasings.
    text = _WAKE_RE.sub('', text, count=1)
    if not re.match(r'^\s*(play|shuffle)\b', text, re.IGNORECASE):
        text = _PLAY_SYNONYMS.sub('play ', text, count=1)

    # Strip trailing filler words
    text = DICTATION_FILLER_RE.sub('', text)

    # Convert spelled-out punctuation to actual characters
    for word, char in DICTATION_PUNCT.items():
        text = re.sub(r'\b' + re.escape(word) + r'\b', char, text, flags=re.IGNORECASE)

    # Convert spelled-out numbers to digits (word boundary aware)
    for spell, digit in sorted(SPELLING_TO_NUMBER.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + re.escape(spell) + r'\b', digit, text, flags=re.IGNORECASE)

    # Normalise &  <--> and (both ways for incoming text)
    text = text.replace('&', 'and')

    return text.strip()


# ── Transport detection ──────────────────────────────────────────

# Pre-compile regex — order matters: longer phrases first
_sorted_transport = sorted(TRANSPORT_PHRASES, key=lambda x: -len(x[1]))
_TRANSPORT_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(p) for _, p in _sorted_transport) + r')$',
    re.IGNORECASE,
)

# Transport map is built AFTER normalise is defined (import-order fix).
_TRANSPORT_MAP = {}
for action, phrase in TRANSPORT_PHRASES:
    _TRANSPORT_MAP[normalise(phrase)] = action


# ── Natural-language commands (assistant QOL) ─────────────────────

_VOL_SET_RE = re.compile(r'(?:set\s+)?(?:the\s+)?volume\s+(?:to|at|is)?\s*(\d{1,3})\b', re.IGNORECASE)
_VOL_PCT_RE = re.compile(r'\b(\d{1,3})\s*(?:percent|%)\s*volume\b', re.IGNORECASE)
_VOL_UP_RE = re.compile(r'\b(turn\s+(?:it|the\s+(?:volume|music))?\s*up|louder|volume\s+up|crank\s+it(?:\s+up)?|pump\s+it\s+up)\b', re.IGNORECASE)
_VOL_DOWN_RE = re.compile(r'\b(turn\s+(?:it|the\s+(?:volume|music))?\s*down|quieter|softer|volume\s+down|lower\s+(?:it|the\s+volume))\b', re.IGNORECASE)
_MUTE_RE = re.compile(r'^\s*(mute|silence|shush)\s*$', re.IGNORECASE)
_MAXVOL_RE = re.compile(r'\b(max(?:imum)?\s+volume|full\s+volume|full\s+blast|blast\s+it)\b', re.IGNORECASE)
_MORE_LIKE_RE = re.compile(r'\b(more\s+like\s+this|similar\s+songs?|more\s+of\s+this|keep\s+this\s+vibe)\b', re.IGNORECASE)
_SURPRISE_RE = re.compile(r'\b(surprise\s+me|play\s+(?:me\s+)?something(?:\s+random)?|random\s+songs?|i\s+don.?t\s+care\s+what)\b', re.IGNORECASE)
_LIKE_RE = re.compile(r'\b(i\s+(?:like|love|dig)\s+this|like\s+this(?:\s+song)?|love\s+this(?:\s+song)?|favou?rite\s+this|add\s+to\s+(?:my\s+)?(?:likes|favou?rites))\b', re.IGNORECASE)


def match_command(query):
    """Detect natural-language commands beyond transport (volume, like, etc.).

    Returns a dict describing the command, or None. Handled by app.py before
    the query is treated as a music search.
    """
    if not query:
        return None
    q = normalise_query(query)
    m = _VOL_SET_RE.search(q) or _VOL_PCT_RE.search(q)
    if m:
        return {"command": "volume", "value": max(0, min(100, int(m.group(1))))}
    if _MAXVOL_RE.search(q):
        return {"command": "volume", "value": 100}
    if _MUTE_RE.match(q):
        return {"command": "volume", "value": 0}
    if _VOL_UP_RE.search(q):
        return {"command": "volume_delta", "delta": 15}
    if _VOL_DOWN_RE.search(q):
        return {"command": "volume_delta", "delta": -15}
    if _MORE_LIKE_RE.search(q):
        return {"command": "more_like_this"}
    if _LIKE_RE.search(q):
        return {"command": "like"}
    if _SURPRISE_RE.search(q):
        return {"command": "surprise"}
    return None


def match_transport(query):
    """Check if the query matches a transport command.

    Returns None if not a transport phrase, or a dict with key ``action``.
    Called from app.py BEFORE any library matching so "stop" never becomes a song.
    """
    if not query:
        return None

    cleaned = normalise_query(query)
    norm = normalise(cleaned)

    # Exact match first
    action = _TRANSPORT_MAP.get(norm)
    if action:
        return {"action": action}

    # Regex fallback
    m = _TRANSPORT_RE.match(cleaned)
    if m:
        matched_text = normalise(m.group(0))
        action = _TRANSPORT_MAP.get(matched_text)
        if action:
            return {"action": action}

    return None


# ── Phrase grammar parser ────────────────────────────────────────

def parse_phrase(query):
    """Run the raw query through the grammar table.

    Returns dict with keys: name, artist, type, shuffle_hint, mode_hint
    or None if no grammar row matched.
    """
    shuffle_on = bool(SHUFFLE_ON_RE.search(query))
    shuffle_off = bool(SHUFFLE_OFF_RE.search(query))
    mode_next = bool(MODE_NEXT_RE.search(query))
    mode_queue = bool(MODE_QUEUE_RE.search(query))

    for rule in GRAMMAR:
        m = rule['regex'].search(query)
        if m:
            name = m.group(1).strip()
            artist = None
            if rule['has_artist']:
                artist = m.group(2).strip() if m.lastindex >= 2 else None

            result = {
                'name': name,
                'artist': artist,
                'type': rule['type'],
                'shuffle_hint': rule['shuffle'] or shuffle_on,
                'shuffle_off': shuffle_off,
                'mode_next': mode_next,
                'mode_queue': mode_queue,
            }

            if mode_next:
                result['mode'] = 'next'
            elif mode_queue:
                result['mode'] = 'queue'
            else:
                result['mode'] = 'play'

            if rule['shuffle']:
                result['shuffle'] = True
            elif shuffle_off:
                result['shuffle'] = False
            elif shuffle_on:
                result['shuffle'] = True
            else:
                result['shuffle'] = None

            return result

    return None


# ── Search plan builder ──────────────────────────────────────────

def build_search_plan(query, artist=None, type_hint="auto", shuffle=None, mode="play"):
    """Build a search-plan dict for the YouTube Music layer.

    Runs dictation cleanup, transport check, phrase grammar, then assembles
    a plan with ``kind``, ``shuffle``, ``mode``, ``spoken`` and (when the
    artist is known) an explicit artist filter.

    Returns None if the query is a transport command (handled elsewhere).
    """
    cleaned = normalise_query(query)
    query_norm = normalise(cleaned)

    # Explicit artist override (from query param, not grammar)
    explicit_artist_norm = normalise(artist) if artist else None

    grammar_result = parse_phrase(cleaned)
    if grammar_result:
        query_norm = normalise(grammar_result['name'])
        if grammar_result['artist']:
            explicit_artist_norm = normalise(grammar_result['artist'])
        if grammar_result['type']:
            type_hint = grammar_result['type']
        shuffle = grammar_result.get('shuffle') if shuffle is None else shuffle
        mode = grammar_result.get('mode', mode)

    # Keep search terms clean ("play jazz music" -> query "jazz"); the smart
    # resolver in search.py decides artist vs genre/mood vs song from the
    # YouTube Music catalog rather than any hardcoded list.
    if type_hint in (None, "auto") and not explicit_artist_norm and query_norm:
        query_norm = normalise(strip_trailers(query_norm))

    # If query is empty but artist is set, treat as artist request
    if not query_norm and explicit_artist_norm:
        type_hint = 'artist'
        query_norm = explicit_artist_norm

    # Determine the "spoken" reply (what Siri will read back)
    if explicit_artist_norm and type_hint != 'artist':
        spoken = f"{query_norm} by {explicit_artist_norm}"
    else:
        spoken = query_norm or explicit_artist_norm

    # Default shuffle: artist & genre queues shuffle, albums never. For an
    # ambiguous ("auto") query leave it None so the resolver decides once it
    # knows whether it found an artist, a genre, or a single song.
    if shuffle is None:
        if type_hint in ('artist', 'genre'):
            shuffle = True
        elif type_hint == 'album':
            shuffle = False
        elif type_hint in (None, 'auto'):
            shuffle = None  # resolver decides from resolved kind

    return {
        "query": query_norm,
        "artist": explicit_artist_norm,
        "kind": type_hint if type_hint != "auto" else None,
        "shuffle": shuffle,
        "mode": mode,
        "spoken": spoken,
    }