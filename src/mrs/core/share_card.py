"""A real image for chat crawlers: cover, album tint and single-song controls."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..paths import write_atomic_bytes


def _font(size: int, bold: bool = False):
    for name in (("C:/Windows/Fonts/segoeuib.ttf" if bold else
                  "C:/Windows/Fonts/segoeui.ttf"),
                 "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit(draw, text, font, width):
    text = str(text or "").replace("\n", " ")
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def render(row: dict, cover: Path | None, out: Path, width: int, height: int):
    art = None
    if cover:
        try:
            with Image.open(cover) as source:
                art = ImageOps.fit(source.convert("RGB"), (216, 216))
        except (OSError, ValueError):
            pass
    colour = (92, 98, 114)
    if art:
        palette = art.resize((48, 48)).quantize(colors=8).convert("RGB")
        colours = palette.getcolors(2304) or []
        if colours:
            # Prefer populated, chromatic midtones over black margins/logo white.
            colour = max(colours, key=lambda item: item[0] *
                         (.18 + (max(item[1]) - min(item[1])) / 255) *
                         (.3 if max(item[1]) < 32 else 1))[1]
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for x in range(width):
        amount = .74 * (1 - x / width) ** 1.15 + .1
        rgb = tuple(round(18 + c * amount * .58) for c in colour)
        draw.line((x, 0, x, height), fill=rgb)
    if art:
        mask = Image.new("L", (216, 216))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 215, 215), radius=22, fill=255)
        image.paste(art, (32, 32), mask)
    else:
        draw.rounded_rectangle((32, 32, 248, 248), radius=22, fill=(45, 47, 54))
        draw.text((98, 87), "♪", font=_font(70), fill=(195, 198, 210))
    white, muted = (250, 249, 252), (203, 203, 212)
    draw.text((282, 30), "SHARED SONG", font=_font(17, True), fill=muted)
    draw.text((280, 69), _fit(draw, row.get("title") or "Shared song", _font(36, True), 726),
              font=_font(36, True), fill=white)
    draw.text((282, 119), _fit(draw, row.get("artist"), _font(25), 722),
              font=_font(25), fill=muted)
    draw.rounded_rectangle((282, 187, 990, 191), radius=2, fill=(142, 140, 153))
    draw.text((282, 204), "0:00", font=_font(18), fill=muted)
    seconds = max(0, int(row.get("duration") or 0))
    length = f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""
    draw.text((990, 204), length, font=_font(18), fill=muted, anchor="ra")
    draw.rounded_rectangle((1040, 98, 1132, 190), radius=27, fill=white)
    draw.polygon(((1076, 121), (1076, 167), (1110, 144)), fill=(29, 30, 36))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92, optimize=True)
    write_atomic_bytes(out, buffer.getvalue())
