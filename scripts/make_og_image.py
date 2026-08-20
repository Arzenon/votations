"""Generate the Open Graph share card, once.

Run by hand when the wording or the palette changes; the result is committed as
`app/static/og.png` and served like any other static asset. Pillow is *not* a
dependency of the application — it is a tool for making this file:

    python -m venv /tmp/img && /tmp/img/bin/pip install pillow
    /tmp/img/bin/python scripts/make_og_image.py

The card carries the site name and what the site is for. Everything else is the
tricolour ballot mark from the favicon, drawn large: a share card is seen at
thumbnail size in a feed, so it has to survive being small.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630
BLUE = (29, 58, 138)
INK = (22, 24, 31)
PAPER = (251, 250, 248)
MUTED = (91, 100, 114)
TRICOLORE = ((0, 85, 164), (255, 255, 255), (239, 65, 53))

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
TARGET = Path(__file__).resolve().parent.parent / "app" / "static" / "og.png"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / name
    if not path.exists():
        sys.exit(f"police introuvable : {path}")
    return ImageFont.truetype(str(path), size)


def main() -> int:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)

    # Left band: the mark on republican blue, so the card reads at thumbnail size.
    band = 360
    draw.rectangle([0, 0, band, HEIGHT], fill=BLUE)

    # The ballot check from the favicon, scaled up and drawn with round joins.
    check = [(118, 322), (168, 372), (258, 258)]
    draw.line(check, fill=(255, 255, 255), width=26, joint="curve")
    for point in (check[0], check[-1]):
        r = 13
        draw.ellipse([point[0] - r, point[1] - r, point[0] + r, point[1] + r], fill=(255, 255, 255))

    # Tricolour rule down the seam.
    for i, colour in enumerate(TRICOLORE):
        top = HEIGHT * i // 3
        draw.rectangle([band, top, band + 10, HEIGHT * (i + 1) // 3], fill=colour)

    text_left = band + 62
    draw.text((text_left, 214), "Votations", font=_font("DejaVuSerif-Bold.ttf", 76), fill=INK)
    draw.text((text_left, 300), "Françaises", font=_font("DejaVuSerif-Bold.ttf", 76), fill=BLUE)

    body = _font("DejaVuSans.ttf", 27)
    for i, line in enumerate(
        [
            "Donnez votre avis sur les lois",
            "adoptées par le Parlement.",
        ]
    ):
        draw.text((text_left, 410 + i * 38), line, font=body, fill=MUTED)

    draw.text(
        (text_left, 516),
        "Données ouvertes · aucune donnée personnelle collectée",
        font=_font("DejaVuSans.ttf", 21),
        fill=MUTED,
    )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    image.save(TARGET, "PNG", optimize=True)
    print(f"→ {TARGET} ({TARGET.stat().st_size} octets, {WIDTH}×{HEIGHT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
