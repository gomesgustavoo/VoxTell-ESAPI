#!/usr/bin/env python3
"""Generate assets/og-card.v3.png — the link-preview card.

    landing/gen-og-card.py          # needs ../.venv-api/bin/python3 (PIL + fontTools)

WHY THIS EXISTS. index.html has declared `twitter:card = summary_large_image`
since v2 with no `og:image` behind it, so every share of this page rendered as a
bare text card.

WHY IT IS GENERATED RATHER THAN DESIGNED IN AN IMAGE EDITOR. Everything on the
card already ships in the page: the same hero slice, the same palette values from
design/tokens.css, and the real Geist faces read out of the served .woff2 (PIL
cannot open woff2; fontTools rewrites it as a bare TTF in memory, which needs
brotli). So the preview cannot drift typographically from the page it previews —
which a hand-made PNG would, the first time the headline changed.

The composition has one constraint worth keeping: the slice occupies the right
quarter with a 260px eased feather, because the feather has to be WIDER than the
gap to the headline. A narrower one let "Approve the contour." land on the bright
liver — still legible, but it reads as a collision.
"""

from __future__ import annotations

import io
import pathlib

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
A = HERE / "assets"

W, H = 1200, 630                       # the size every scraper crops to
GROUND, INK, MUTED, FAINT = "#0a0e13", "#f4f7fa", "#93a1b8", "#6b7a90"
BORDER = "#232d3d"
ACCENT, ACCENT2 = (34, 211, 238), (59, 130, 246)   # --vx-accent, --vx-accent-2
SLICE_W, FEATHER = 470, 260
MARGIN = 72


def face(woff2: str) -> io.BytesIO:
    """woff2 -> in-memory TTF, so the card uses the page's own faces."""
    font = TTFont(str(A / "fonts" / woff2))
    font.flavor = None                 # drop the woff2 wrapper
    buf = io.BytesIO()
    font.save(buf)
    buf.seek(0)
    return buf


def main() -> int:
    sans_b = ImageFont.truetype(face("Geist-latin.woff2"), 44)
    mono_h = ImageFont.truetype(face("GeistMono-latin.woff2"), 56)
    mono_s = ImageFont.truetype(face("GeistMono-latin.woff2"), 22)
    mono_t = ImageFont.truetype(face("GeistMono-latin.woff2"), 19)

    card = Image.new("RGB", (W, H), GROUND)

    src = Image.open(A / "hero-slice.v2.webp").convert("L")
    src = src.resize((SLICE_W, int(src.height * SLICE_W / src.width)), Image.LANCZOS)
    tinted = Image.blend(
        Image.new("RGB", src.size, GROUND), Image.merge("RGB", (src,) * 3), 0.34
    )
    mask = Image.new("L", src.size, 255)
    md = ImageDraw.Draw(mask)
    for x in range(FEATHER):
        md.line([(x, 0), (x, src.height)], fill=int(255 * (x / FEATHER) ** 1.6))
    card.paste(tinted, (W - SLICE_W, (H - src.height) // 2), mask)

    d = ImageDraw.Draw(card)

    # Wordmark. "Tell" carries the brand ramp, the same way .text-grad does on the
    # page: a gradient block masked by the glyphs, since PIL has no gradient fill.
    d.text((MARGIN, 62), "Vox", font=sans_b, fill=INK)
    vox_w = d.textlength("Vox", font=sans_b)
    tell_w = int(d.textlength("Tell", font=sans_b))
    ramp = Image.new("RGB", (tell_w, 60))
    rd = ImageDraw.Draw(ramp)
    for x in range(tell_w):
        t = x / max(1, tell_w - 1)
        rd.line([(x, 0), (x, 60)], fill=tuple(
            int(ACCENT[i] + (ACCENT2[i] - ACCENT[i]) * t) for i in range(3)))
    glyphs = Image.new("L", (tell_w, 60), 0)
    ImageDraw.Draw(glyphs).text((0, 0), "Tell", font=sans_b, fill=255)
    card.paste(ramp, (int(MARGIN + vox_w), 62), glyphs)

    d.text((MARGIN, 152), "SEGMENTATION FOR VARIAN ECLIPSE", font=mono_t, fill=FAINT)
    d.text((MARGIN, 244), "Type the structure.", font=mono_h, fill=INK)
    d.text((MARGIN, 316), "Approve the contour.", font=mono_h, fill=ACCENT)
    d.text((MARGIN, 432), "Contours land in the open plan's structure set.",
           font=mono_s, fill=MUTED)
    d.text((MARGIN, 466), "Nothing is written until you tick it.", font=mono_s, fill=MUTED)
    d.line([(MARGIN, 542), (540, 542)], fill=BORDER, width=1)
    d.text((MARGIN, 560), "voxtell.dicomsegvr.com  ·  no server in your network",
           font=mono_t, fill=FAINT)

    out = A / "og-card.v3.png"
    card.save(out, optimize=True)
    print(f"wrote {out.relative_to(HERE)} — {out.stat().st_size:,} bytes, {W}x{H}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
