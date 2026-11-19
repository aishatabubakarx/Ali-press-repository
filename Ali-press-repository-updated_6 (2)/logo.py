#!/usr/bin/env python3
"""Brand logo for the pins: a classic, cute dusty-rose wordmark on a TRANSPARENT background.

Rendered from the fonts in ./fonts, so changing the words in config.json > "logo" and re-running
(or just running the pipeline) gives a new logo. No image editing needed.

  python logo.py            # writes assets/logo.png (transparent)
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).parent
FONT_DIR = ROOT / "fonts"
SCRIPT_FONT = FONT_DIR / "GreatVibes-Regular.ttf"
CAPS_FONT = FONT_DIR / "Montserrat.ttf"

DEFAULTS = {
    "script": "Kitchen",          # big flowing word
    "caps": "GADGETS",            # small spaced capitals underneath
    "color": "#C9768C",           # dusty rose: girly and soft, not neon
    "accent": "#E8B4C0",          # lighter blush for the little details
}


def _hex(c, a=255):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


def _font(path, size, weight=None):
    f = ImageFont.truetype(str(path), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def _spaced(d, xy, text, fnt, fill, spacing):
    """Draw text with extra letter spacing (left edge at x). Returns the width used."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=fnt, fill=fill)
        x += d.textlength(ch, font=fnt) + spacing
    return x - xy[0] - spacing


def _heart(d, cx, cy, s, fill):
    """Tiny filled heart centred on (cx, cy), width ~ 2*s."""
    r = s / 2
    d.ellipse((cx - s, cy - s * 0.85, cx, cy + r * 0.15), fill=fill)
    d.ellipse((cx, cy - s * 0.85, cx + s, cy + r * 0.15), fill=fill)
    d.polygon([(cx - s * 0.97, cy - s * 0.18), (cx + s * 0.97, cy - s * 0.18), (cx, cy + s * 1.05)], fill=fill)


def make_logo(cfg=None, scale=3):
    """Return an RGBA image: rose wordmark, transparent background, trimmed tight."""
    c = {**DEFAULTS, **(cfg or {})}
    main, soft = _hex(c["color"]), _hex(c["accent"])
    big = 150 * scale
    sf = _font(SCRIPT_FONT, big)
    cf = _font(CAPS_FONT, 46 * scale, weight=600)
    sp = 14 * scale

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    sw = int(probe.textlength(c["script"], font=sf))
    cw = sum(probe.textlength(ch, font=cf) + sp for ch in c["caps"]) - sp
    pad = 40 * scale
    W = int(max(sw, cw + 150 * scale)) + pad * 2
    H = int(big * 1.45) + 90 * scale + pad
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # script word, centred
    d.text((W / 2, pad + big * 0.55), c["script"], font=sf, fill=main, anchor="mm")

    # caps line with thin rules and a little heart either side
    y = pad + big * 1.18
    x0 = (W - cw) / 2
    _spaced(d, (x0, y), c["caps"], cf, main, sp)
    mid = y + 26 * scale
    d.line((x0 - 130 * scale, mid, x0 - 38 * scale, mid), fill=soft, width=3 * scale)
    d.line((x0 + cw + 38 * scale, mid, x0 + cw + 130 * scale, mid), fill=soft, width=3 * scale)
    _heart(d, x0 - 20 * scale, mid, 9 * scale, soft)
    _heart(d, x0 + cw + 20 * scale, mid, 9 * scale, soft)

    img = img.crop(img.getbbox())
    return img.resize((img.width // scale, img.height // scale), Image.LANCZOS)


def load_logo(cfg=None):
    return make_logo((cfg or {}).get("logo") if cfg and "logo" in cfg else cfg)


def with_glow(logo, strength=235, radius=9):
    """Logo on a soft white halo, so the rose lettering reads on any photo (still no solid box)."""
    pad = radius * 4
    halo = Image.new("RGBA", (logo.width + pad * 2, logo.height + pad * 2), (0, 0, 0, 0))
    mask = Image.new("L", halo.size, 0)
    mask.paste(logo.split()[3], (pad, pad))
    mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(radius))
    mask = mask.point(lambda v: min(255, int(v * strength / 160)))
    halo.paste((255, 255, 255, 255), (0, 0), mask)
    halo.alpha_composite(logo, (pad, pad))
    return halo


if __name__ == "__main__":
    try:
        cfg = json.loads((ROOT / "config.json").read_text()).get("logo", {})
    except FileNotFoundError:
        cfg = {}
    out = ROOT / "assets" / "logo.png"
    out.parent.mkdir(exist_ok=True)
    make_logo(cfg).save(out)
    print("wrote", out)
