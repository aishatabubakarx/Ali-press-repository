#!/usr/bin/env python3
"""Pinterest BULK builder: makes upload-ready CSV files (up to 200 pins each) for Pinterest's
"Bulk create Pins" tool, with every pin already scheduled and tagged.

Run it from GitHub: Actions > "Bulk pins (CSV)" > Run workflow. Modes:
  batches     N batches of 200 pins, N per_day pins on every day, one continuous timeline
  single_day  just X pins for ONE day you pick
  test        a handful of pins (tomorrow) to try the upload first

Per pin: product -> (optional) Meta Muse Image lifestyle scene -> headline text designed to fit the
picture -> saved under docs/bulk/<run>/ -> a row in the CSV with title, description, link, board,
publish date and keywords (Pinterest's tag area). Video pins get a designed thumbnail (never the
first frame).
"""
import argparse
import base64
import csv
import datetime as dt
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

import pipeline as P

ROOT = P.ROOT
BULK_DIR = ROOT / "docs" / "bulk"
CSV_DIR = ROOT / "bulk_out"
COLUMNS = ["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link",
           "Publish date", "Keywords"]

SCENE_PROMPT = """Use the attached image as the reference. This is the MAIN PRODUCT IMAGE.

Keep this exact product identical. Do not change its shape, color, pattern, logo, text, label, size, or proportions. Do not redesign it. Do not add or remove any part of it.

Make it photorealistic, like it was shot by a Canon EOS R5 camera with a 50mm lens at f/1.8, shallow depth of field, product in sharp focus, background softly blurred with natural bokeh, natural light, realistic textures and shadows.

Give it a MAKE SENSE SHOWCASE. Place the product in its natural, real-life setting where someone would actually use it. The showcase must be aspirational but REAL, not fantasy, not luxury hotel, not AI dream world.

The showcase must make sense for HOW this product is used:
- If the product sits on the ground, place it on the ground.
- If the product sits on a surface, place it on the correct surface, not directly on dirt or sand.
- If the product is carried, show it placed on its natural surface with its normal props around it.

Vertical 9:16 format, Pinterest style. Product is the hero, centered, upright, fully visible.
Keep the top and bottom of the frame calm and uncluttered. Do not add any text, captions or watermarks to the scene."""

_ai_dead = {"stop": False}


# ------------------------------------------------------------ Meta image ----
def meta_key():
    """The key from the GitHub secret. Fixes the common copy slip LLM_123_abc -> LLM|123|abc."""
    k = P.env("META_IMAGE_API_KEY")
    m = re.fullmatch(r"LLM_(\d+)_(.+)", k)
    return f"LLM|{m.group(1)}|{m.group(2)}" if m else k


def ai_scene(img, ai):
    """Product photo in -> photoreal lifestyle scene out (PIL image), or None if it fails."""
    key = meta_key()
    if not key or _ai_dead["stop"]:
        return None
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    for attempt in range(1, int(ai.get("retries", 3)) + 1):
        try:
            r = requests.post(
                ai["base_url"].rstrip("/") + "/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                files={"image": ("product.jpg", buf.getvalue(), "image/jpeg")},
                data={"model": ai["model"], "prompt": SCENE_PROMPT, "size": ai["size"], "n": "1"},
                timeout=240)
            if r.status_code == 200:
                d = r.json()["data"][0]
                raw = base64.b64decode(d["b64_json"]) if d.get("b64_json") else requests.get(d["url"], timeout=90).content
                return Image.open(io.BytesIO(raw)).convert("RGB")
            print(f"  [ai] HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code in (400, 401, 403, 404):    # wrong key / model / request: retrying will not help
                _ai_dead["stop"] = True
                print("  [ai] stopping AI scenes for this run (check META_IMAGE_API_KEY and ai settings); "
                      "the rest use the standard design.")
                return None
        except Exception as e:
            print(f"  [ai] attempt {attempt} failed: {e}")
        time.sleep(4 * attempt)
    return None


# ------------------------------------------------------------ pin design ----
MONT = ROOT / "fonts" / "Montserrat.ttf"


def mont(size, weight=800):
    try:
        f = ImageFont.truetype(str(MONT), size)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        return f
    except OSError:
        return P.font(size)


def cover(img, size):
    w, h = size
    s = max(w / img.width, h / img.height)
    im = img.resize((math.ceil(img.width * s), math.ceil(img.height * s)), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))


def design(scene, headline, logo_cfg):
    """Put the headline where the picture is calmest, in a colour that contrasts with it, over a
    soft tinted band, with an outline + shadow and a small accent bar. Brand tab in the corner."""
    logo_cfg = logo_cfg or {}
    Wd, Hd = scene.size
    gray = scene.convert("L").filter(ImageFilter.GaussianBlur(3))
    zones = {"top": (0.12, 0.34), "bottom": (0.66, 0.90)}
    best = None
    for name, (a, b) in zones.items():
        st = ImageStat.Stat(gray.crop((0, int(Hd * a), Wd, int(Hd * b))))
        score = st.stddev[0] + (0 if name == "bottom" else 3)      # calmest zone wins; slight lean to bottom
        if best is None or score < best[0]:
            best = (score, name, st.mean[0])
    _, zone, mean = best
    light_bg = mean >= 150
    ink = (62, 26, 42) if light_bg else (255, 255, 255)
    edge = (255, 255, 255) if light_bg else (42, 16, 25)

    probe = ImageDraw.Draw(scene)
    maxw = int(Wd * 0.84)
    size = int(min(Wd * 0.105, Hd * 0.11))
    while True:
        f = mont(size, 800)
        lines = P.wrap(probe, headline, f, maxw)
        if size <= 34 or (len(lines) <= 3 and all(probe.textlength(l, font=f) <= maxw for l in lines)):
            break
        size -= 4
    line_h = int(size * 1.12)
    block_h = line_h * len(lines)
    a, b = zones[zone]
    mid = int(Hd * (a + b) / 2)
    y0 = mid - block_h // 2 - 12

    canvas = scene.convert("RGBA")
    band = block_h + int(Hd * 0.13)
    top = mid - band // 2
    base = (255, 247, 249) if light_bg else (30, 12, 20)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i in range(band):
        al = int(135 * math.sin(math.pi * i / max(band - 1, 1)) ** 1.5)
        ld.line((0, top + i, Wd, top + i), fill=base + (al,))
    canvas.alpha_composite(layer)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    y = y0
    for ln in lines:
        sd.text((Wd / 2, y + 5), ln, font=f, fill=(0, 0, 0, 150), anchor="ma", stroke_width=6, stroke_fill=(0, 0, 0, 150))
        y += line_h
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
    td = ImageDraw.Draw(canvas)
    sw = max(3, size // 22)
    y = y0
    for ln in lines:
        td.text((Wd / 2, y), ln, font=f, fill=ink + (255,), anchor="ma", stroke_width=sw, stroke_fill=edge + (255,))
        y += line_h
    bw = int(Wd * 0.16)
    accent = P._hex_rgb(logo_cfg.get("color", "#C9768C"))
    td.rounded_rectangle((Wd / 2 - bw / 2, y + 12, Wd / 2 + bw / 2, y + 22), radius=5, fill=accent + (255,),
                         outline=edge + (255,), width=2)

    m = int(logo_cfg.get("margin", 0))
    canvas.alpha_composite(P.logo_stamp(logo_cfg), (m, m))
    return canvas.convert("RGB")


def save_jpg(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=85, optimize=True, progressive=True)


def video_size(url):
    """(width, height) of the video, so the thumbnail can match its aspect ratio (Pinterest requires it)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:stream_tags=rotate", "-of", "json", url],
            capture_output=True, text=True, timeout=60)
        s = json.loads(out.stdout)["streams"][0]
        w, h = int(s["width"]), int(s["height"])
        if str(s.get("tags", {}).get("rotate", "0")) in ("90", "270", "-90", "-270"):
            w, h = h, w
        return (w, h) if w > 0 and h > 0 else None
    except Exception as e:
        print(f"  [video] could not read size ({e})")
        return None


# ------------------------------------------------------------- schedule ----
def day_slots(day, n, bcfg, seed="s"):
    """n publish times (UTC) spread across the posting window of `day`, slightly uneven so it looks natural."""
    sh, sm = (int(x) for x in str(bcfg.get("window_start_utc", "12:00")).split(":"))
    start = dt.datetime.combine(day, dt.time(sh, sm))
    step = float(bcfg.get("window_hours", 16)) * 60 / max(n, 1)
    out = []
    for i in range(n):
        j = random.Random(f"{seed}-{day}-{i}").uniform(0 if i == 0 else -0.3, 0.3) * step
        out.append(start + dt.timedelta(minutes=i * step + j))
    return out


def public_base(bcfg):
    b = bcfg.get("public_base_url") or P.env("PUBLIC_BASE_URL")
    if b:
        return b.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://raw.githubusercontent.com/{repo or 'USER/REPO'}/{branch}"


GENERIC = {"set", "sets", "kit", "bundle"}


def keywords(raw_title, niche, n=10):
    words = P.product_words(raw_title)
    nouns = set(niche.get("product_nouns", []))
    out = []
    for i, w in enumerate(words):
        if w in nouns:
            prev = words[i - 1] if i else ""
            if w in GENERIC:                      # "set" alone is useless as a tag, "cookware set" is not
                if prev:
                    out.append(f"{prev} {w}")
                continue
            if prev and prev not in nouns:
                out.append(f"{prev} {w}")
            out.append(w)
    out += niche.get("bulk_keywords", [])
    seen, res = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            res.append(k)
    return ", ".join(res[:n])


def prune(keep_days):
    today = dt.date.today()
    for m in BULK_DIR.glob("*/manifest.json"):
        try:
            last = dt.date.fromisoformat(json.loads(m.read_text())["last_publish"][:10])
        except Exception:
            continue
        if last + dt.timedelta(days=keep_days) < today:
            shutil.rmtree(m.parent, ignore_errors=True)
            print(f"[prune] removed old images {m.parent.name}")


# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["batches", "single_day", "test"], default="batches")
    ap.add_argument("--batches", type=int, default=1)
    ap.add_argument("--per-day", type=int)
    ap.add_argument("--start-date")
    ap.add_argument("--day-date")
    ap.add_argument("--day-pins", type=int, default=15)
    ap.add_argument("--test-pins", type=int, default=5)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="do not write posted.json")
    ap.add_argument("--niche")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    b = cfg.get("bulk", {})
    batch_size = int(b.get("batch_size", 200))
    per_day = args.per_day or int(b.get("pins_per_day", 15))
    ai = {**{"enabled": True, "base_url": "https://api.meta.ai/v1", "model": "muse-image-1.0",
             "size": "1080x1920", "workers": 4, "retries": 3}, **b.get("ai", {})}
    use_ai = ai["enabled"] and not args.no_ai and bool(meta_key())
    if ai["enabled"] and not args.no_ai and not meta_key():
        print("[ai] META_IMAGE_API_KEY is not set, so pins use the standard design (no AI scene).")
    tracking = P.env("AE_TRACKING_ID")
    P.detect_mode({"keywords": "kitchen", "page_no": 1, "page_size": 1, "tracking_id": tracking})
    niche = next(n for n in cfg["niches"] if not args.niche or n["name"] == args.niche)
    name = niche["name"]

    today = dt.datetime.now(dt.timezone.utc).date()
    if args.mode == "batches":
        total = max(1, args.batches) * batch_size
        first_day = dt.date.fromisoformat(args.start_date) if args.start_date else today + dt.timedelta(days=1)
        times = []
        d = first_day
        while len(times) < total:
            slots = day_slots(d, per_day, b)
            times += slots[:total - len(times)]
            d += dt.timedelta(days=1)
    elif args.mode == "single_day":
        if not args.day_date:
            sys.exit("single_day needs a date (YYYY-MM-DD)")
        total = min(max(1, args.day_pins), batch_size)
        times = day_slots(dt.date.fromisoformat(args.day_date), total, b)
    else:  # test
        total = max(1, args.test_pins)
        first_day = dt.date.fromisoformat(args.start_date) if args.start_date else today + dt.timedelta(days=1)
        times = day_slots(first_day, total, b)

    posted = P.load_posted()
    done = posted.setdefault(name, {})
    vdone = posted.setdefault(f"{name}__videos", {})
    pdone = posted.setdefault(f"{name}__premium", {})
    print(f"[bulk] mode={args.mode}: want {total} pins for {name}; AI scenes: {'on' if use_ai else 'off'}")

    # 1) find products (standard tier + the premium slice), interleaved so premium pins are spread out
    n_prem = round(total * float(b.get("premium_share", 0.2))) if niche.get("premium_filters") else 0
    cands = P.gather(niche, cfg, tracking, done, total, n_prem)
    std = [p for p in cands if p.get("_tier") != "premium"]
    prem = [p for p in cands if p.get("_tier") == "premium"]
    order = []
    while std or prem:
        if prem and len(order) % 5 == 2:
            order.append(prem.pop(0))
        elif std:
            order.append(std.pop(0))
        else:
            order.append(prem.pop(0))
    print(f"[bulk] {len(order)} products passed the filters")

    # 2) affiliate link + product photo + text for each
    chosen, used_links = [], set()
    for p in order:
        if len(chosen) >= total:
            break
        pid = str(p["product_id"])
        try:
            link = P.get_link(p, tracking)
            if not link or link in used_links:
                continue
            img = P.download_image(p["product_main_image_url"])
            title, desc, headline, core, _tags = P.build_text(p, niche)
        except Exception as e:
            print(f"[bulk] {pid}: skipped ({e})")
            continue
        used_links.add(link)
        chosen.append({"p": p, "pid": pid, "link": link, "img": img, "title": title, "core": core,
                       "headline": headline})
    if len(chosen) < total:
        print(f"[bulk] WARNING: only {len(chosen)} of {total} pins could be made, because not enough products "
              f"passed your filters. Lower min_orders / min_commission in config.json, or add keywords.")
    if not chosen:
        sys.exit("[bulk] no products to make pins from")

    # 3) AI lifestyle scenes (several at once)
    scenes = [None] * len(chosen)
    if use_ai:
        print(f"[ai] making {len(chosen)} scenes with {ai['model']} ({ai['workers']} at a time)...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=int(ai["workers"])) as ex:
            futs = [ex.submit(ai_scene, c["img"], ai) for c in chosen]
            for i, fu in enumerate(futs):
                scenes[i] = fu.result()
                if (i + 1) % 10 == 0:
                    print(f"[ai] {i + 1}/{len(chosen)} done ({int(time.time() - t0)}s)")
        print(f"[ai] {sum(s is not None for s in scenes)} of {len(chosen)} scenes made; the rest use the standard design")

    # 4) design every pin, write files, build rows
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_url = f"{public_base(b)}/docs/bulk/{run_id}"
    out_dir = BULK_DIR / run_id
    logo = cfg.get("logo")
    vcap = int(cfg["settings"].get("max_videos_per_day", 1))
    vids_by_day = {}
    out_size = tuple(b.get("out_size", [900, 1600]))
    rows = []
    for i, c in enumerate(chosen):
        p, pid = c["p"], c["pid"]
        when = times[i]
        day = when.date().isoformat()
        scene = scenes[i]
        vurl = P.norm_url(p.get("product_video_url"))
        is_video = False
        thumb_url = ""
        if (cfg["settings"].get("video_pins", True) and vurl and vids_by_day.get(day, 0) < vcap
                and P.video_ok(vurl, cfg["settings"].get("video_max_mb", 25))):
            vs = video_size(vurl)
            if vs:
                tw = 900 if vs[1] >= vs[0] else 1280
                tsize = (tw, round(tw * vs[1] / vs[0]))
                src = scene if scene is not None else c["img"]
                save_jpg(design(cover(src, tsize), c["headline"], logo), out_dir / f"{name}_{pid}_thumb.jpg")
                thumb_url = f"{base_url}/{name}_{pid}_thumb.jpg"
                is_video = True
                vids_by_day[day] = vids_by_day.get(day, 0) + 1
        if is_video:
            media = vurl
        else:
            pin = design(cover(scene, out_size), c["headline"], logo) if scene is not None \
                else P.make_pin(c["img"], c["headline"], logo)
            save_jpg(pin, out_dir / f"{name}_{pid}.jpg")
            media = f"{base_url}/{name}_{pid}.jpg"
        opts = b.get("disclosures") or [b.get("disclosure", P.DISCLOSURE)]
        disc = P.pick(opts, f"d{pid}").strip()                  # a different soft wording per pin
        room = 500 - (len(disc) + 1 if disc else 0)           # the disclosure always survives the 500-character cap
        core = c["core"] if len(c["core"]) <= room else c["core"][:room].rsplit(" ", 1)[0]
        desc = f"{core} {disc}".strip()
        rows.append({"Title": c["title"][:100], "Media URL": media, "Pinterest board": niche["board_name"],
                     "Thumbnail": thumb_url, "Description": desc, "Link": c["link"],
                     "Publish date": when.strftime("%Y-%m-%dT%H:%M:%S"),
                     "Keywords": keywords(p.get("product_title"), niche),
                     "_pid": pid, "_video": is_video, "_premium": p.get("_tier") == "premium"})
        if (i + 1) % 25 == 0:
            print(f"[bulk] designed {i + 1}/{len(chosen)}")

    # 5) CSV files (200 pins each), manifest, memory of what was used
    latest, archive = CSV_DIR / "latest", CSV_DIR / f"run_{run_id}"
    shutil.rmtree(latest, ignore_errors=True)
    latest.mkdir(parents=True)
    archive.mkdir(parents=True)
    chunks = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    summary = []
    for k, chunk in enumerate(chunks, 1):
        d1, d2 = chunk[0]["Publish date"][:10], chunk[-1]["Publish date"][:10]
        tag = {"batches": f"batch_{k}_of_{len(chunks)}", "single_day": "single_day", "test": "test"}[args.mode]
        fname = f"{tag}_{d1}_to_{d2}.csv" if d1 != d2 else f"{tag}_{d1}.csv"
        with open(latest / fname, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            w.writerows(chunk)
        shutil.copy(latest / fname, archive / fname)
        earliest = dt.date.fromisoformat(d2) - dt.timedelta(days=int(b.get("max_days_ahead", 30)))
        summary.append({"file": fname, "pins": len(chunk), "first": d1, "last": d2,
                        "upload_from": max(earliest, today).isoformat(),
                        "upload_by": d1, "videos": sum(r["_video"] for r in chunk)})
        print(f"[bulk] wrote bulk_out/latest/{fname}: {len(chunk)} pins, {d1} to {d2}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(
        {"run": run_id, "mode": args.mode, "last_publish": rows[-1]["Publish date"]}, indent=1))
    if not args.dry_run:
        for r in rows:
            day = r["Publish date"][:10]
            done[r["_pid"]] = day
            if r["_video"]:
                vdone[r["_pid"]] = day
            if r["_premium"]:
                pdone[r["_pid"]] = day
        P.POSTED_FILE.write_text(json.dumps(posted, indent=1))
    prune(int(b.get("keep_days", 45)))

    # 6) the plain-English summary that appears on the run page
    lines = ["## Pinterest bulk files are ready", "",
             f"Made **{len(rows)} pins** ({sum(r['_video'] for r in rows)} video, "
             f"{sum(1 for s in scenes if s is not None)} with an AI scene). Download them from the "
             "**pinterest-csv** box at the bottom of this page, or from the `bulk_out/latest` folder in your repo.", "",
             "| File | Pins | First pin | Last pin | Upload it between |", "|---|---|---|---|---|"]
    for s in summary:
        lines.append(f"| {s['file']} | {s['pins']} | {s['first']} | {s['last']} | {s['upload_from']} and {s['upload_by']} |")
    lines += ["", "Pinterest only lets you schedule about 30 days ahead, so a file whose last pin is further out "
              "than that should wait until the date in the last column.", ""]
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
