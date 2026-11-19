#!/usr/bin/env python3
"""AliExpress affiliate products -> Pinterest-ready pins -> Make.com webhook.

Env vars (set as GitHub Secrets, never in code):
  AE_APP_KEY, AE_APP_SECRET, AE_TRACKING_ID, MAKE_WEBHOOK_URL
Optional: AE_SIGN_METHOD (sha256|md5) and AE_TS_MODE (ms|utc|gmt8) to skip auto-detection.

Usage:
  python pipeline.py --dry-run --limit 2     # builds pin images in ./out, sends nothing
  python pipeline.py --limit 4               # builds and sends to Make, records posted.json
"""
import argparse, base64, hashlib, hmac, io, json, math, os, random, re, sys, time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
    return (math.log(num(p.get("lastest_volume")) + 1) * (pct(p.get("evaluate_rate")) / 100)
            * (1 + pct(p.get("commission_rate")) / 20))


# --------------------------------------------------------------- text ----
STOP = {"new", "hot", "sale", "free", "shipping", "hotsale", "fashion", "original",
        "brand", "wholesale", "dropshipping", "drop", "ship", "pcs", "pc", "set"}


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
PALETTES = [("#FFF4E8", "#E85D3F", "#2B2B2B"), ("#EAF4F1", "#1F8A70", "#1E2A2A"),
            ("#FDEFF3", "#D6456B", "#2A1F24"), ("#F1F0FB", "#5B4BDB", "#23213A")]
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf"]


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


def make_pin(product_img, headline, tag, key):
    bg, accent, ink = pick(PALETTES, key)
    W, H = 1000, 1500
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # tag pill
    tf = font(34)
    tw = d.textlength(tag, font=tf)
    d.rounded_rectangle((W / 2 - tw / 2 - 34, 62, W / 2 + tw / 2 + 34, 128), radius=33, fill=accent)
    d.text((W / 2, 95), tag, font=tf, fill="white", anchor="mm")
    # headline (shrink until it fits in 3 lines)
    for size in (88, 78, 70, 62, 54):
        hf = font(size)
        lines = wrap(d, headline, hf, 860)
        if len(lines) <= 3:
            break
    lines = lines[:3]
    lh = int(size * 1.18)
    y = 175 + (3 - len(lines)) * lh // 2
    for ln in lines:
        d.text((W / 2, y), ln, font=hf, fill=ink, anchor="ma")
        y += lh
    # product card
    d.rounded_rectangle((50, 470, 950, 1370), radius=40, fill="white", outline=accent, width=6)
    prod = ImageOps.contain(product_img, (830, 830), Image.LANCZOS)
    img.paste(prod, (500 - prod.width // 2, 920 - prod.height // 2))
    # bottom bar
    d.rounded_rectangle((50, 1395, 950, 1470), radius=37, fill=accent)
    d.text((W / 2, 1433), "SAVE THIS FOR LATER", font=font(38), fill="white", anchor="mm")
    return img


def to_jpeg_b64(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue(), base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- main ----
def load_posted():
    try:
        return json.loads(POSTED_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
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
        name = niche["name"]
        done = posted.setdefault(name, {})
        limit = args.limit or cfg["settings"]["pins_per_run"]
        cands, seen = [], set()
        kws = niche["keywords"][:]
        random.shuffle(kws)
        for kw in kws:
            for page in range(1, cfg["settings"]["max_pages"] + 1):
                try:
                    prods = search(kw, page, cfg, tracking)
                except RuntimeError as e:
                    print(f"[{name}] {kw!r} page {page}: {e}")
                    break
                for p in prods:
                    pid = str(p.get("product_id"))
                    if pid not in seen and passes(p, niche["filters"], niche["exclude_words"], done):
                        seen.add(pid)
                        p["_kw"] = kw
                        cands.append(p)
                time.sleep(1)
                if not prods:
                    break
            if len(cands) >= limit * 4:
                break
        cands.sort(key=score, reverse=True)
        first, used_kw = [], set()          # prefer one product per keyword so pins vary
        for p in cands:
            if p["_kw"] not in used_kw:
                used_kw.add(p["_kw"])
                first.append(p)
        cands = first + [p for p in cands if p not in first]
        used_links = set()
        print(f"[{name}] {len(cands)} candidates passed filters; making up to {limit} pins")

        made = 0
        for p in cands:
            if made >= limit:
                break
            pid = str(p["product_id"])
            try:
                link = get_link(p, tracking)
                if not link or link in used_links:
                    print(f"[{name}] {pid}: no unique affiliate link, skipping")
                    continue
                used_links.add(link)
                title, desc, headline, core, tags = build_text(p, niche)
                pin = make_pin(download_image(p["product_main_image_url"]), headline, niche["tag"], pid)
                jpg, b64 = to_jpeg_b64(pin)
            except Exception as e:  # keep going on one bad product
                print(f"[{name}] {pid}: skipped ({e})")
                continue
            (OUT_DIR / f"{name}_{pid}.jpg").write_bytes(jpg)
            payload = {
                "niche": name, "board_name": niche["board_name"], "product_id": pid,
                # title / description_core are the NO-AI fallback; Make uses them if Gemini fails
                "title": title, "description": desc, "description_core": core,
                "hashtags": tags, "disclosure": DISCLOSURE,
                "product_title": p.get("product_title") or "",   # the original AliExpress name, for Gemini
                "link": link,                                     # your affiliate link -> the Pin's URL
                "alt_text": f"{title} - {name} product photo"[:480],
                "image_content_type": "image/jpeg", "image_base64": b64,
                "price": p.get("target_sale_price"), "good_rate": p.get("evaluate_rate"),
                "orders": p.get("lastest_volume"), "commission": p.get("commission_rate"),
                "video_url": norm_url(p.get("product_video_url")),  # for a later video-pin step
            }
            if args.dry_run:
                print(f"[{name}] DRY RUN pin {pid}: {title} | {link[:60]}")
                made += 1
                continue
            r = requests.post(hook, json=payload, timeout=90)
            if r.status_code == 200:
                done[pid] = time.strftime("%Y-%m-%d")
                POSTED_FILE.write_text(json.dumps(posted, indent=1))
                made += 1
                print(f"[{name}] sent pin {pid}: {title}")
            else:
                print(f"[{name}] webhook rejected {pid}: HTTP {r.status_code} {r.text[:150]}")
                break
            time.sleep(cfg["settings"]["delay_seconds"])
        print(f"[{name}] done: {made} pin(s)")


if __name__ == "__main__":
    main()
