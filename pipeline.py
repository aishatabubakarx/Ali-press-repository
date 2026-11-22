#!/usr/bin/env python3
"""AliExpress affiliate products -> Pinterest-ready pins -> Make.com webhook.

Env vars (set as GitHub Secrets, never in code):
  AE_APP_KEY, AE_APP_SECRET, AE_TRACKING_ID, MAKE_WEBHOOK_URL
Optional: AE_SIGN_METHOD (sha256|md5) and AE_TS_MODE (ms|utc|gmt8) to skip auto-detection.

Usage:
  python pipeline.py --dry-run --limit 2     # builds pin images in ./out, sends nothing
  python pipeline.py --limit 4               # builds and sends to Make, records posted.json
  python pipeline.py --roundup --dry-run     # builds a "13 best ..." cover pin + roundup page

Never posts more than settings.daily_cap pins (default 10) in one UTC day, however often it runs.
"""
import argparse, string, base64, hashlib, hmac, html, io, json, math, os, random, re, sys, time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from logo import load_logo, with_glow

API_URL = "https://api-sg.aliexpress.com/sync"
ROOT = Path(__file__).parent
POSTED_FILE = ROOT / "posted.json"
OUT_DIR = ROOT / "out"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

SIGN_METHODS = ["sha256", "md5"]   # sha256 is what this account accepted
TS_MODES = ["ms", "utc", "gmt8"]
VIAS = ["form", "query"]          # how the parameters are sent
_mode = {"sign": None, "ts": None, "via": None}


def env(name):
    """Read a secret and strip stray spaces/newlines that break signatures."""
    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------- API ----
def make_ts(mode):
    if mode == "ms":
        return str(int(time.time() * 1000))
    t = time.gmtime() if mode == "utc" else time.gmtime(time.time() + 8 * 3600)
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


def sign(params, secret, method):
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    if method == "md5":
        return hashlib.md5((secret + base + secret).encode()).hexdigest().upper()
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def raw_call(method, biz, sign_method, ts_mode, via="form"):
    params = {
        "app_key": env("AE_APP_KEY"),
        "method": method,
        "sign_method": sign_method,
        "timestamp": make_ts(ts_mode),
        "v": "2.0",
        "format": "json",
    }
    params.update({k: str(v) for k, v in biz.items() if v not in (None, "")})
    params["sign"] = sign(params, env("AE_APP_SECRET"), sign_method)
    if via == "query":
        r = requests.post(API_URL, params=params, timeout=40)
    else:
        r = requests.post(API_URL, data=params, timeout=40)
    try:
        return r.json()
    except ValueError:
        return {"http_error": r.status_code, "body": r.text[:300]}


def is_error(data):
    # failures come back as {"error_response": {...}} (or a bare {"code":..,"message":..})
    return not any(k.endswith("_response") and k != "error_response" for k in data)


def err_code(data):
    e = data.get("error_response", data)
    return str(e.get("code") or e.get("http_error") or "unknown")


def detect_mode(probe):
    """Check the credentials, then find the signing/transport combination AliExpress accepts."""
    key, secret = env("AE_APP_KEY"), env("AE_APP_SECRET")
    print(f"[check] app_key={key!r} secret_length={len(secret)} "
          f"tracking_id={env('AE_TRACKING_ID')!r} webhook_set={bool(env('MAKE_WEBHOOK_URL'))}")
    if not key or not secret or not env("AE_TRACKING_ID"):
        sys.exit("[check] a GitHub secret is empty. Re-add AE_APP_KEY, AE_APP_SECRET and "
                 "AE_TRACKING_ID with the exact names (no typos).")
    s, t = env("AE_SIGN_METHOD"), env("AE_TS_MODE")
    if s and t:
        _mode.update(sign=s, ts=t, via=env("AE_VIA") or "form")
        return
    results = []
    for sm in SIGN_METHODS:
        for tm in TS_MODES:
            for via in VIAS:
                data = raw_call("aliexpress.affiliate.product.query", probe, sm, tm, via)
                if not is_error(data):
                    _mode.update(sign=sm, ts=tm, via=via)
                    print(f"[api] accepted: sign_method={sm} timestamp={tm} via={via}")
                    return
                results.append(f"{sm}/{tm}/{via}: {err_code(data)}")
                time.sleep(0.4)
    print("[api] every combination was rejected:")
    for line in results:
        print("   ", line)
    print("[api] last response:", json.dumps(data)[:400])
    sys.exit("[api] If every line says IncompleteSignature, the App Secret (or App Key) in "
             "GitHub is almost certainly not the current one. Re-copy it from the app's "
             "Overview page (View) and update the AE_APP_SECRET secret.")


def call(method, biz):
    data = raw_call(method, biz, _mode["sign"], _mode["ts"], _mode["via"])
    if is_error(data):
        raise RuntimeError(f"API error: {json.dumps(data)[:400]}")
    return data


def norm_url(u):
    if not u:
        return ""
    return "https:" + u if u.startswith("//") else u


def search(keyword, page, cfg, tracking_id):
    s = cfg["settings"]
    data = call("aliexpress.affiliate.product.query", {
        "keywords": keyword, "page_no": page, "page_size": s["page_size"],
        "tracking_id": tracking_id, "target_currency": s["currency"],
        "target_language": s["language"], "ship_to_country": s.get("ship_to_country"),
        "sort": "LAST_VOLUME_DESC",
    })
    resp = data.get("aliexpress_affiliate_product_query_response", {}).get("resp_result", {})
    if resp.get("resp_code") != 200:
        raise RuntimeError(f"search failed: code={resp.get('resp_code')} msg={resp.get('resp_msg')}")
    prods = resp.get("result", {}).get("products", {}).get("product", []) or []
    for p in prods:
        small = p.get("product_small_image_urls")
        if isinstance(small, dict):
            p["product_small_image_urls"] = small.get("string", [])
    return prods


def get_link(p, tracking_id):
    """One affiliate link PER product, generated from that product's own URL."""
    url = f"https://www.aliexpress.com/item/{p.get('product_id')}.html"
    try:
        data = call("aliexpress.affiliate.link.generate", {
            "promotion_link_type": "0", "source_values": url, "tracking_id": tracking_id})
        res = data.get("aliexpress_affiliate_link_generate_response", {}).get("resp_result", {}).get("result", {})
        links = res.get("promotion_links", {}).get("promotion_link", [])
        if links and links[0].get("promotion_link"):
            return links[0]["promotion_link"]
    except RuntimeError as e:
        print(f"  link.generate failed for {p.get('product_id')}: {e}")
    return p.get("promotion_link", "")


# ------------------------------------------------------------ filtering ----
def pct(x):
    try:
        return float(str(x).replace("%", "").strip())
    except ValueError:
        return 0.0


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return 0.0


def passes(p, f, exclude, posted_ids):
    if str(p.get("product_id")) in posted_ids or not p.get("product_main_image_url"):
        return False
    title = (p.get("product_title") or "").lower()
    if any(w.lower() in title for w in exclude):
        return False
    price = num(p.get("target_sale_price"))
    return (pct(p.get("evaluate_rate")) >= f["min_good_rate"]
            and num(p.get("lastest_volume")) >= f["min_orders"]
            and pct(p.get("commission_rate")) >= f["min_commission"]
            and f["min_price"] <= price <= f["max_price"])


def score(p):
    """Popularity x rating x commission rate x how many dollars a sale actually pays."""
    price, rate = num(p.get("target_sale_price")), pct(p.get("commission_rate"))
    return (math.log(num(p.get("lastest_volume")) + 1) * (pct(p.get("evaluate_rate")) / 100)
            * (1 + rate / 20) * math.log1p(price * rate / 100))


# --------------------------------------------------------------- text ----
STOP = {"new", "hot", "sale", "free", "shipping", "hotsale", "fashion", "original",
        "brand", "wholesale", "dropshipping", "drop", "ship", "pcs", "pc"}


def clean_title(t, max_words=7):
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", t or "")
    t = re.sub(r"[^A-Za-z0-9&\- ]", " ", t)
    seen, out = set(), []
    words = t.split()
    while words and any(c.isdigit() for c in words[0]):   # "500 900ML Manual..." -> "Manual..."
        words.pop(0)
    for w in words:
        lw = w.lower()
        if lw in STOP or re.fullmatch(r"\d{4}", w) or lw in seen:
            continue
        seen.add(lw)
        out.append(w if any(c.isdigit() for c in w) else w.capitalize())
    return " ".join(out[:max_words]) or "Kitchen Find"


def pick(items, key):
    return items[int(hashlib.md5(str(key).encode()).hexdigest(), 16) % len(items)]


FILLER = STOP | {"for", "and", "with", "the", "your", "you", "multi", "function", "multifunctional",
                 "portable", "simple", "convenient", "household", "home", "high", "quality", "large",
                 "small", "mini", "creative", "universal", "style", "kitchen", "accessories",
                 "accessory", "tool", "tools", "item", "items", "gadget", "gadgets", "use", "type"}
DISCLOSURE = "As an AliExpress affiliate I may earn a commission from purchases."


def product_words(raw_title):
    """Meaningful words from the ORIGINAL AliExpress title (no AI, just filtering)."""
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", raw_title or "")
    t = re.sub(r"[^A-Za-z ]", " ", t)
    out = []
    for w in t.lower().split():
        if len(w) >= 3 and w not in FILLER and w not in out:
            out.append(w)
    return out[:8]


ADJ = {"plastic", "stainless", "steel", "manual", "hanging", "resistant", "heat", "non", "stick",
       "wooden", "foldable", "adjustable", "electric", "automatic", "reusable", "double", "layer",
       "tier", "silicone", "metal", "glass", "ceramic", "bamboo", "wall", "mounted", "free"}


def make_hashtags(raw_title, niche, n=5):
    """Hashtags from product-type words in the original title (+ the niche's own tags)."""
    words = product_words(raw_title)
    nouns = set(niche.get("product_nouns", []))
    tags = []
    for i, w in enumerate(words):
        if w in nouns:
            prev = words[i - 1] if i > 0 else ""
            tags.append("#" + (prev + w if prev and prev not in ADJ and prev not in nouns else w))
    out = []
    for t in tags[:3] + niche["hashtags"]:
        if t not in out and len(t) <= 28:
            out.append(t)
    return out[:n]


def build_text(p, niche):
    raw, pid = p.get("product_title"), p["product_id"]
    title = clean_title(raw, 9)                          # pin title: a bit longer, keyword-rich
    headline = clean_title(raw, 7)                       # text printed on the image: shorter
    suffix = niche.get("title_suffix", "")
    full_title = title
    if niche.get("title_keyword", "kitchen").lower() not in title.lower() and len(title + suffix) <= 100:
        full_title = title + suffix                      # keep the niche keyword in the pin title
    benefit = pick(niche["benefit_lines"], f"b{pid}")
    cta = pick(niche["cta_lines"], f"c{pid}")
    tags = " ".join(make_hashtags(raw, niche))
    core = f"{title}. {benefit} {cta}"                   # plain text; Make adds tags + disclosure itself
    desc = f"{core} {tags} {DISCLOSURE}"
    if len(desc) > 490:
        core = f"{title}. {benefit}"
        desc = f"{core} {tags} {DISCLOSURE}"
    return full_title[:100], desc[:490], headline, core, tags


# -------------------------------------------------------------- image ----
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf"]
W, H = 1000, 1500


def font(size):
    for fp in FONT_PATHS:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default(size)


def wrap(draw, text, fnt, width):
    lines, cur = [], ""
    for w in text.split():
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=fnt) <= width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    return lines + [cur] if cur else lines


def download_image(url):
    r = requests.get(norm_url(url), headers=UA, timeout=40)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def edge_color(img):
    """Average colour of the photo's border, and whether that border is a plain backdrop."""
    s = img.resize((40, 40), Image.BILINEAR)
    px = []
    for i in range(40):
        px += [s.getpixel((i, 0)), s.getpixel((i, 39)), s.getpixel((0, i)), s.getpixel((39, i))]
    avg = tuple(sum(p[c] for p in px) // len(px) for c in range(3))
    spread = max(abs(p[c] - avg[c]) for p in px for c in range(3))
    return avg, spread < 18


def feather(size, edge):
    """Mask that fades the photo's edges so it melts into the background."""
    m = Image.new("L", size, 255)
    ImageDraw.Draw(m).rectangle((0, 0, size[0] - 1, size[1] - 1), outline=0, width=max(2, edge // 2))
    return m.filter(ImageFilter.GaussianBlur(edge / 2))


def scrim(top, strength=225):
    """Dark gradient over the bottom of the pin so white text stays readable."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(top, H):
        a = int(strength * ((y - top) / (H - top)) ** 1.4)
        d.line((0, y, W, y), fill=(18, 18, 22, a))
    return layer


_logo_cache = {}


def logo_stamp(cfg_logo, width=210):
    """Small brand tab for the pin's top-left corner: the wordmark on a clean white tab that sits
    flush against the edges (square on the outside, rounded on the inside), so it reads on any photo."""
    key = (json.dumps(cfg_logo, sort_keys=True), width)
    if key not in _logo_cache:
        deep = dict(cfg_logo or {})                       # a deeper rose than the brand colour, for contrast
        c = deep.get("color", "#C9768C").lstrip("#")
        deep["color"] = "#%02X%02X%02X" % tuple(int(int(c[i:i + 2], 16) * 0.78) for i in (0, 2, 4))
        lg = load_logo(deep)
        pad = 12
        lg = lg.resize((width, int(lg.height * width / lg.width)), Image.LANCZOS)
        tw, th = lg.width + pad * 2, lg.height + pad * 2
        scale = 4                                          # draw big, shrink for smooth corners
        tab = Image.new("RGBA", (tw * scale, th * scale), (0, 0, 0, 0))
        r = 16 * scale
        ImageDraw.Draw(tab).rounded_rectangle((-r, -r, tw * scale - 1, th * scale - 1), radius=r,
                                              fill=(255, 255, 255, 240))
        tab = tab.resize((tw, th), Image.LANCZOS)
        tab.alpha_composite(lg, (pad, pad))
        _logo_cache[key] = tab
    return _logo_cache[key]


def make_pin(product_img, headline, logo_cfg=None):
    """Image-first pin: the product photo fills the full width and is the hero.
    Text sits on a dark gradient at the bottom, with the small brand tab in the top-left corner."""
    col, plain = edge_color(product_img)
    if plain:   # white / flat studio backdrop: extend it seamlessly
        canvas = Image.new("RGB", (W, H), col)
    else:       # busy photo: extend with a soft blurred copy of itself
        canvas = ImageOps.fit(product_img, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(45))
        canvas = Image.blend(canvas, Image.new("RGB", (W, H), (255, 255, 255)), 0.2)
    if plain:
        hero = ImageOps.contain(product_img, (W, 1220), Image.LANCZOS)   # full width, as tall as fits
    else:       # busy lifestyle photo: fill the whole frame width and height, trim the sides a little
        hero = ImageOps.fit(product_img, (W, 1220), Image.LANCZOS)
    y = 70 + (1220 - hero.height) // 2
    canvas.paste(hero, (0, y), feather(hero.size, 40))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim(1010)).convert("RGB")
    stamp = logo_stamp(logo_cfg)
    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(stamp, (0, 0))
    canvas = canvas.convert("RGB")
    d = ImageDraw.Draw(canvas)
    for size in (84, 74, 66, 58, 50):                  # headline: shrink until it fits in 2 lines
        hf = font(size)
        lines = wrap(d, headline, hf, 900)
        if len(lines) <= 2:
            break
    lines = lines[:2]
    lh = int(size * 1.15)
    y = 1385 - lh * len(lines)
    for ln in lines:
        d.text((W / 2, y), ln, font=hf, fill="white", anchor="ma")
        y += lh
    d.text((W / 2, 1440), "SAVE THIS FOR LATER", font=font(28), fill=(255, 255, 255, 190), anchor="mm")
    return canvas


def make_roundup_cover(images, count, headline, subline, logo_cfg=None):
    """'13 best ...' cover: brand logo, big number + headline over a 3x3 collage of the products."""
    accent = (logo_cfg or {}).get("color", "#C9768C")
    img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    lg = load_logo(logo_cfg)
    lg = lg.resize((230, int(lg.height * 230 / lg.width)), Image.LANCZOS)
    d.rounded_rectangle((30, 26, 30 + lg.width + 40, 26 + lg.height + 30), radius=22, fill="white")
    img.paste(lg, (50, 41), lg)
    d.text((40, 160), str(count), font=font(230), fill="white", anchor="la")
    nx = 70 + int(d.textlength(str(count), font=font(230)))
    for size in (66, 58, 50, 44):
        hf = font(size)
        lines = wrap(d, headline.upper(), hf, W - nx - 40)
        if len(lines) <= 3:
            break
    y = 175
    for ln in lines[:3]:
        d.text((nx, y), ln, font=hf, fill="white", anchor="la")
        y += int(size * 1.15)
    d.text((nx, y + 6), subline, font=font(32), fill="#FFE9EE", anchor="la")
    tile, gap, x0, y0 = 300, 20, 30, 440
    shown = images[:9] if count <= 9 else images[:8]     # 9th tile becomes '+N more' only for bigger lists
    for i in range(9):
        cx, cy = x0 + (i % 3) * (tile + gap), y0 + (i // 3) * (tile + gap)
        if i < len(shown):
            t = ImageOps.fit(shown[i], (tile, tile), Image.LANCZOS)
            img.paste(t, (cx, cy))
            d.ellipse((cx + 10, cy + 10, cx + 66, cy + 66), fill=accent)
            d.text((cx + 38, cy + 38), str(i + 1), font=font(32), fill="white", anchor="mm")
        elif count > len(shown):
            d.rectangle((cx, cy, cx + tile, cy + tile), fill="#2B2B2B")
            more = count - len(shown)
            d.text((cx + tile / 2, cy + tile / 2 - 22), f"+{more}", font=font(84), fill="white", anchor="mm")
            d.text((cx + tile / 2, cy + tile / 2 + 52), "MORE PICKS", font=font(30), fill="white", anchor="mm")
    d.rounded_rectangle((30, 1400, 970, 1470), radius=35, fill="white")
    d.text((W / 2, 1435), f"SEE ALL {count} PICKS", font=font(38), fill=accent, anchor="mm")
    return img


def to_jpeg_b64(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue(), base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------- roundup page ----
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<meta name="description" content="{title} - hand-picked, well-reviewed finds.">
<style>
body{{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;background:#faf7f4;color:#222}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 60px}}
h1{{font-size:1.9rem;line-height:1.2;margin:.2em 0}}
.note{{font-size:.85rem;color:#666;margin:0 0 24px}}
.card{{background:#fff;border-radius:16px;box-shadow:0 2px 10px #0001;margin:0 0 22px;overflow:hidden}}
.card img{{display:block;width:100%;aspect-ratio:1/1;object-fit:contain;background:#fff}}
.body{{padding:14px 18px 20px}}
.n{{display:inline-block;background:{accent};color:#fff;border-radius:50%;width:34px;height:34px;line-height:34px;text-align:center;font-weight:700;margin-right:8px}}
h2{{font-size:1.15rem;margin:0 0 6px;display:flex;align-items:center}}
.price{{color:#555;margin:0 0 12px}}
a.btn{{display:inline-block;background:{accent};color:#fff;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:999px}}
</style></head><body><main>
<h1>{title}</h1>
<p class="note">{disclosure} Prices and availability can change.</p>
{cards}
</main></body></html>"""

CARD = """<div class="card"><img src="{img}" alt="{alt}" loading="lazy"><div class="body">
<h2><span class="n">{n}</span>{name}</h2><p class="price">{price}</p>
<a class="btn" href="{link}" rel="sponsored nofollow noopener" target="_blank">View on AliExpress</a></div></div>"""


def build_roundup_page(title, items, accent):
    cards = "\n".join(CARD.format(
        n=i, img=html.escape(norm_url(it["image"]), quote=True), alt=html.escape(it["name"], quote=True),
        name=html.escape(it["name"]), price=html.escape(it["price"]),
        link=html.escape(it["link"], quote=True)) for i, it in enumerate(items, 1))
    return PAGE.format(title=html.escape(title), accent=accent, cards=cards, disclosure=DISCLOSURE)


# ---------------------------------------------------------------- main ----
def load_posted():
    try:
        return json.loads(POSTED_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def gather(niche, cfg, tracking, done, want, need_premium=0):
    """Search AliExpress and return the best candidates that pass the niche's filters.

    Standard products ($80-$300 by default) come from `keywords` + `filters`. When need_premium > 0
    and the niche has `premium_keywords` + `premium_filters` (the $300-$800 slice), that many premium
    products are slotted in right after the first standard one, tagged p["_tier"] = "premium".
    """
    name = niche["name"]
    s = cfg["settings"]
    seen = set()

    def sweep(kws, filters, goal, tier):
        found = []
        kws = kws[:]
        random.shuffle(kws)
        for kw in kws:
            for page in range(1, s["max_pages"] + 1):
                try:
                    prods = search(kw, page, cfg, tracking)
                except RuntimeError as e:
                    print(f"[{name}] {kw!r} page {page}: {e}")
                    break
                for p in prods:
                    pid = str(p.get("product_id"))
                    if pid not in seen and passes(p, filters, niche["exclude_words"], done):
                        seen.add(pid)
                        p["_kw"], p["_tier"] = kw, tier
                        found.append(p)
                time.sleep(1)
                if not prods:
                    break
            if len(found) >= goal:
                break
        return found

    def ranked(cands):
        cands.sort(key=score, reverse=True)
        first, used_kw = [], set()      # prefer one product per keyword so pins vary
        for p in cands:
            if p["_kw"] not in used_kw:
                used_kw.add(p["_kw"])
                first.append(p)
        return first + [p for p in cands if p not in first]

    out = ranked(sweep(niche["keywords"], niche["filters"], want * 4, "standard"))
    pf, pk = niche.get("premium_filters"), niche.get("premium_keywords")
    if need_premium > 0 and pf and pk:
        extra = ranked(sweep(pk, pf, need_premium * 4, "premium"))[:need_premium]
        for i, p in enumerate(extra):
            out.insert(min(1 + i, len(out)), p)
    return out


def video_ok(url, max_mb=25):
    """Only offer a video pin if the file is really there, is a video, and is not too big.
    (Some CDNs refuse HEAD requests, so fall back to a streamed GET that reads only the headers.)"""
    if not url:
        return False
    for method in (requests.head, requests.get):
        try:
            r = method(url, headers=UA, timeout=20, allow_redirects=True, **({"stream": True} if method is requests.get else {}))
            ctype = r.headers.get("Content-Type", "").lower()
            size = int(r.headers.get("Content-Length") or 0)
            r.close()
            if r.status_code == 200:
                is_video = ctype.startswith("video/") or url.lower().split("?")[0].endswith((".mp4", ".mov", ".m4v"))
                return is_video and 0 < size <= max_mb * 1024 * 1024
        except (requests.RequestException, ValueError):
            continue
    return False


def send(hook, payload):
    r = requests.post(hook, json=payload, timeout=90)
    return r.status_code == 200, f"HTTP {r.status_code} {r.text[:150]}"


def run_singles(niche, cfg, tracking, hook, posted, args):
    name = niche["name"]
    done = posted.setdefault(name, {})
    limit = args.limit or cfg["settings"]["pins_per_run"]
    cap = int(cfg["settings"].get("daily_cap", 10))
    today = time.strftime("%Y-%m-%d")
    posted_today = sum(1 for d_ in done.values() if d_ == today)
    room = cap - posted_today
    if room <= 0 and not args.dry_run:
        print(f"[{name}] daily cap of {cap} pins already reached today ({posted_today}). Nothing to do.")
        return
    if not args.dry_run:
        limit = min(limit, room)
    vdone = posted.setdefault(f"{name}__videos", {})
    vcap = int(cfg["settings"].get("max_videos_per_day", 1))
    videos_today = sum(1 for d_ in vdone.values() if d_ == today)
    pdone = posted.setdefault(f"{name}__premium", {})
    pcap = int(cfg["settings"].get("max_premium_per_day", 2))
    premium_today = sum(1 for d_ in pdone.values() if d_ == today)
    need_premium = max(0, min(int(cfg["settings"].get("premium_per_run", 1)), pcap - premium_today, limit))
    cands = gather(niche, cfg, tracking, done, limit, need_premium)
    used_links = set()
    print(f"[{name}] {len(cands)} candidates passed filters; making up to {limit} pins "
          f"(video pins today: {videos_today}/{vcap}, premium today: {premium_today}/{pcap})")
    made = 0
    for p in cands:
        if made >= limit:
            break
        pid = str(p["product_id"])
        is_premium = p.get("_tier") == "premium"
        if is_premium and premium_today >= pcap:
            continue
        if (cfg["settings"].get("video_pins", True) and videos_today >= vcap
                and p.get("product_video_url")):
            print(f"[{name}] {pid}: has a video but today's video cap ({vcap}) is used up, skipped (stays available for another day)")
            continue
        try:
            link = get_link(p, tracking)
            if not link or link in used_links:
                print(f"[{name}] {pid}: no unique affiliate link, skipping")
                continue
            used_links.add(link)
            title, desc, headline, core, tags = build_text(p, niche)
            pin = make_pin(download_image(p["product_main_image_url"]), headline, cfg.get("logo"))
            jpg, b64 = to_jpeg_b64(pin)
        except Exception as e:  # keep going on one bad product
            print(f"[{name}] {pid}: skipped ({e})")
            continue
        (OUT_DIR / f"{name}_{pid}.jpg").write_bytes(jpg)
        vurl = norm_url(p.get("product_video_url"))
        is_video = bool(cfg["settings"].get("video_pins", True)) and video_ok(vurl, cfg["settings"].get("video_max_mb", 25))
        payload = {
            "pin_type": "video" if is_video else "image",
            "niche": name, "board_name": niche["board_name"], "product_id": pid,
            "tier": "premium" if is_premium else "standard",
            # title / description_core are the NO-AI fallback; Make uses them if Gemini fails
            "title": title, "description": desc, "description_core": core,
            "hashtags": tags, "disclosure": DISCLOSURE,
            "product_title": p.get("product_title") or "",   # the original AliExpress name, for Gemini
            "link": link,                                     # your affiliate link -> the Pin's URL
            "alt_text": f"{title} - {name} product photo"[:480],
            "image_content_type": "image/jpeg", "image_base64": b64,   # the pin image / video cover
            "price": p.get("target_sale_price"), "good_rate": p.get("evaluate_rate"),
            "orders": p.get("lastest_volume"), "commission": p.get("commission_rate"),
            "video_url": vurl if is_video else "",
            "image_url": norm_url(p.get("product_main_image_url")),   # public link, usable as the video pin cover
        }
        kind = "VIDEO" if is_video else "image"
        if args.dry_run:
            print(f"[{name}] DRY RUN {kind} pin {pid} ({'premium' if is_premium else 'standard'}, ${p.get('target_sale_price')}, {p.get('commission_rate')}): {title} | {link[:60]}")
            made += 1
            continue
        ok, info = send(hook, payload)
        if ok:
            done[pid] = time.strftime("%Y-%m-%d")
            if is_video:
                vdone[pid] = done[pid]
                videos_today += 1
            if is_premium:
                pdone[pid] = done[pid]
                premium_today += 1
            POSTED_FILE.write_text(json.dumps(posted, indent=1))
            made += 1
            print(f"[{name}] sent {kind} pin {pid}: {title}")
        else:
            print(f"[{name}] webhook rejected {pid}: {info}")
            break
        time.sleep(cfg["settings"]["delay_seconds"])
    print(f"[{name}] done: {made} pin(s)")


def run_roundup(niche, cfg, tracking, hook, posted, args):
    """One '13 best ...' pin: a collage cover that links to a roundup page with every affiliate link."""
    name = niche["name"]
    rc = {**{"count": 13, "headline": f"Best {name} finds", "subline": "we think you'll love",
             "site_base_url": ""}, **cfg.get("roundup", {}), **niche.get("roundup", {})}
    count = args.roundup_count or rc["count"]
    site = rc["site_base_url"].rstrip("/")
    if not site and not args.dry_run:
        print("[roundup] skipped: set roundup.site_base_url in config.json to your GitHub Pages "
              "address (e.g. https://USER.github.io/REPO). The pin links to the page built from it.")
        return
    rdone = posted.setdefault(f"{name}__roundup", {})
    cands = gather(niche, cfg, tracking, rdone, count)
    items, imgs, used_links = [], [], set()
    for p in cands:
        if len(items) >= count:
            break
        pid = str(p["product_id"])
        try:
            link = get_link(p, tracking)
            if not link or link in used_links:
                continue
            im = download_image(p["product_main_image_url"])
        except Exception as e:
            print(f"[{name}] roundup {pid}: skipped ({e})")
            continue
        used_links.add(link)
        imgs.append(im)
        price = p.get("target_sale_price")
        items.append({"id": pid, "name": clean_title(p.get("product_title"), 9), "link": link,
                      "image": p["product_main_image_url"],
                      "price": f"About ${price}" if price else ""})
    if len(items) < 6:
        print(f"[{name}] roundup: only {len(items)} usable products, not enough for a list (need 6+)")
        return
    n = len(items)
    title = f"{n} {string.capwords(rc['headline'])} {string.capwords(rc['subline'])}"
    key = f"roundup{n}{time.strftime('%Y%m%d')}"
    cover = make_roundup_cover(imgs, n, rc["headline"], rc["subline"], cfg.get("logo"))
    jpg, b64 = to_jpeg_b64(cover)
    slug = f"best-{name}-{time.strftime('%Y-%m-%d')}-{hashlib.md5(key.encode()).hexdigest()[:4]}"
    accent = cfg.get("logo", {}).get("color", "#C9768C")
    page = build_roundup_page(title, items, accent)
    page_dir = OUT_DIR if args.dry_run else ROOT / "docs"
    page_dir.mkdir(exist_ok=True)
    (page_dir / f"{slug}.html").write_text(page, encoding="utf-8")
    (page_dir / ".nojekyll").touch()
    (OUT_DIR / f"{name}_{slug}.jpg").write_bytes(jpg)
    url = f"{site}/{slug}.html" if site else f"https://example.github.io/{slug}.html"
    tags = " ".join(niche["hashtags"][:5])
    core = (f"{title}. Hand-picked, well-reviewed finds in one list. "
            f"Tap through to see all {n} with photos and prices. Save it for later.")
    payload = {
        "pin_type": "roundup", "niche": name, "board_name": niche["board_name"], "product_id": slug,
        "title": title[:100], "description": f"{core} {tags} {DISCLOSURE}"[:490],
        "description_core": core, "hashtags": tags, "disclosure": DISCLOSURE,
        "product_title": f"{title} (a list of {n} products, shown as a collage)",
        "link": url, "alt_text": f"Collage of {n} {name} finds"[:480],
        "image_content_type": "image/jpeg", "image_base64": b64,
        "price": "", "good_rate": "", "orders": "", "commission": "", "video_url": "", "image_url": "",
    }
    if args.dry_run:
        print(f"[{name}] DRY RUN roundup of {n}: {title} -> {url}")
        return
    ok, info = send(hook, payload)
    if ok:
        for it in items:
            rdone[it["id"]] = time.strftime("%Y-%m-%d")
        POSTED_FILE.write_text(json.dumps(posted, indent=1))
        print(f"[{name}] sent roundup pin: {title} -> {url}")
    else:
        print(f"[{name}] webhook rejected roundup: {info}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--roundup", action="store_true", help="make one '13 best ...' list pin instead of single-product pins")
    ap.add_argument("--roundup-count", type=int)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    tracking = env("AE_TRACKING_ID")
    hook = env("MAKE_WEBHOOK_URL")
    if not args.dry_run and not hook:
        sys.exit("MAKE_WEBHOOK_URL is not set (use --dry-run to test without it)")

    detect_mode({"keywords": "kitchen", "page_no": 1, "page_size": 1, "tracking_id": tracking})
    posted = load_posted()
    OUT_DIR.mkdir(exist_ok=True)
    for niche in cfg["niches"]:
        if args.niche and niche["name"] != args.niche:
            continue
        (run_roundup if args.roundup else run_singles)(niche, cfg, tracking, hook, posted, args)


if __name__ == "__main__":
    main()
