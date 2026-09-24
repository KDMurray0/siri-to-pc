# Music Request Server — Senior GUI/UX Review

Reviewed live, 2026-09-15. Window sizes: 1280×800, 1920×1080, 2560×1080, 768×1024. Idle state, playing state, lyrics state, queue, lists, sessions and passes, settings. The concept here is the strongest part of the app — and the surface around it is where it currently loses trust.

## What makes this one alive

- **The ambient wash.** The album art bleeding a blurred colour behind the card is the detail that makes the app feel like a room instead of a form. Keep it; it's the soul.
- **The lyric stage.** When a song plays, the lyrics take centre and the player demotes itself to a small bar in the corner — the song is the stage, the controls are the footlights. That's a real design decision and the right one for a music app. Nothing in this review asks for it to be removed; it asks for it to be executed cleanly (see below).
- **The control hierarchy.** One big play button, transport flanking it, and a second row of smaller actions under it. The eye lands on the one button that matters.
- **Empty states that speak.** "Nothing queued. Ask for a song, an artist, or a vibe." — the app tells you its own grammar instead of showing a grey void.
- **"Play anything — a song, artist, vibe, or Spotify link."** The search prompt is a use-case, not a placeholder.
- **A status line that narrates.** "Downloading · …" with a cancel × — the app says what it's doing and lets you stop it. That's honesty, and it's rare.
- **Named themes** — a small number of looks with personalities, not a colour picker.
- **Shared-list honesty** — "added by" appears on shared lists and disappears on your own, where it would say nothing. Detail, but a good one.

## Where it breaks trust

### 1. The most destructive action is the least protected

The two "never again" icons (this song / this artist) sit in the same row as like, download and add-to-list, in the same quiet grey, and fire on a single click. There is no confirm, no armed state, no undo. One mis-tap and an artist is out of rotation permanently. This is the app's most irreversible listener action, and it is wearing the same clothes as the "like this" button.

**How I'd build it:** make the first tap ask. The button arms itself into a short "sure?" state for a couple of seconds (or a small inline confirm appears), the destructive icons get a distinct hover treatment, and the toast that fires afterwards offers a brief undo. The server call stays one; the protection belongs in the page.

### 2. Destructive icons that look benign

Remove from the queue, Delete a list, "End this session", "Ban this link", "Stop sharing" — all the same quiet glyphs as Play and Add. "Remove this item" and "ban the whole link" carry identical visual weight, and none of them asks before acting.

**How I'd build it:** one shared language for destructive actions: a danger tint on hover (or a red glyph on press), and a confirmation step on the irreversible ones — End this session, Ban this link, Delete list. Keep the reversible queue-Remove confirm-free, but let it still be *recognisable* as destructive.

### 3. The right half of an ultra-wide screen is dead

At 2560×1080 the app keeps a fixed content column anchored to the left and leaves the other ~40% of the window as empty page. At 1280 the same content fills the frame. The layout has no idea wide screens exist.

**How I'd build it:** past ~1920px, either centre the composition with balanced margins and a slightly larger art/lyrics scale, or grow into a three-column layout — art left, lyrics centre, queue as a permanent right panel. One decision, one breakpoint, and a re-shoot at 2560×1080 and 3440×1440 to prove it.

### 4. A black scar in the idle state at 768×1024

In "Nothing playing" at 768×1024, a solid black rectangle sits in the lower-left of the content area — a layer that rendered without its content, in a darker shade than the surface around it. The state the user sees most often is the one that looks broken.

**How I'd build it:** find the fixed or absolutely-positioned layer that exists in the idle state at ≤800px width, and either give it content or hide it in that state; then re-shoot the idle state at that size until the scar is gone.

### 5. Ellipses that land mid-word

"Downloading · I Made For Lo…" and a queue row "I Don't Want to Miss a Thing (From "Arma…" — the app is naming the song and then stopping, mid-syllable. It reads as a bug even when it's only a width limit.

**How I'd build it:** ellipsize at word boundaries, let long queue titles wrap to a second line (the title-over-artist row already has the room), and carry the full title in the hover tooltip.

### 6. A button that starts empty

The "Choose output device" button between mute and the volume slider is empty in the markup until the page script paints an icon into it. If the script is late, slow, or fails, the most trusted row in the player carries a mystery blank button.

**How I'd build it:** put a static speaker glyph in the markup from the start and let the script swap in the device-specific icon once it knows one. A control should never be iconless.

## If I only had three

1. **Protect the block.** First tap asks, destructive icons are recognisable, the toast offers undo. This is the one that actually burns people.
2. **One danger language.** Every destructive action gets the same visual weight and the same moment of doubt.
3. **A wide mode.** 2560×1080 should be the app's best look, not its most wasted.

The concept — the wash, the lyric stage, the spoken empty states — is already better than most music apps ship. The surface just needs to protect the trust the concept earns.
