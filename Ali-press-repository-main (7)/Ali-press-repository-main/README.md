# Homey: Pinterest pin pipeline (AliExpress -> Make -> Pinterest)

Every run searches AliExpress for kitchen products, keeps the well-reviewed ones, builds a 1000x1500 pin
image with the **Homey** logo, and sends title / description / affiliate link / image (and the
video, when the product has one) to your Make webhook, which publishes the pin.

## Setup (once)
1. Put these files in a GitHub repo, keeping the `.github/workflows/pins.yml` path.
2. Settings > Actions > General > Workflow permissions > **Read and write**.
3. Settings > Secrets and variables > Actions, add: `AE_APP_KEY`, `AE_APP_SECRET`,
   `AE_TRACKING_ID`, `MAKE_WEBHOOK_URL`.

## Schedule: 10 pins a day
| Batch | Time (UTC) | Pins |
|-------|-----------|------|
| 1     | 07:00     | 3    |
| 2     | 12:30     | 3    |
| 3     | 19:00     | 4    |

Hard stop: `settings.daily_cap` (10) in `config.json` means that even manual runs can't push past 10 a day.
To change a time, edit the `cron` line **and** the matching text in the `LIMIT:` line of `pins.yml`
(they must be identical). GitHub may start scheduled runs a few minutes late.

## First runs
1. Actions > Make pins > Run workflow, leave **dry_run** ticked. Check the log and the `pin-previews` download.
2. In Make, import `make/AliExpress_Pins_blueprint_polling_v3.json` (see Video pins below), open the scenario,
   click **Run once**, then run the workflow with dry_run **unticked** and limit 1.
3. Turn the Make scenario on.

## Logo
Dusty-rose "Homey" wordmark (with "HOME & KITCHEN" underneath), transparent background (`assets/logo.png`), stamped top-left on
every pin with a soft white halo so it reads on any photo. It is drawn from the fonts in `fonts/`, so to
change the words or the pink, edit `config.json` > `logo` and the next run uses it. To regenerate the PNG:
`python logo.py`.

## Video pins
If a product has an AliExpress video (and it is under `video_max_mb`), the payload has `pin_type` = `video`
and a `video_url`; the pin image is sent as `image_base64` and used as the video cover. The Make blueprint
has a router: image pins go the original way, video pins go through: ask Pinterest for an upload slot ->
download the video -> upload it -> wait 5 minutes -> check status once -> create the pin only if Pinterest says the video succeeded. `max_videos_per_day` (default 1) caps video pins; once reached, products that have a video are skipped until the next UTC day and only no-video products are posted. Set `"video_pins": false`
in `config.json` to go back to image-only.
Make limits file size by plan (about 5 MB on Free). If your plan is Free, set `video_max_mb` to 5.

## Roundup pins ("13 best kitchen gadgets")
One cover pin (logo, big number, 3x3 collage) that links to a mobile page listing every product with its own
affiliate link. Setup: in GitHub go to Settings > Pages > Deploy from branch > `main` / `docs`, then set
`roundup.site_base_url` in `config.json` to `https://USER.github.io/REPO`. Run it from Actions > Make pins >
Run workflow with **roundup** ticked (leave dry_run ticked first; the cover and page land in the `pin-previews`
download). Wording is in `config.json` > `roundup`. Roundups don't count toward `daily_cap`.

## Bulk upload (CSV) workflow
`Actions > Bulk pins (CSV) > Run workflow` builds Pinterest "Bulk create Pins" files with every pin scheduled and tagged.
Modes: `test` (a few pins), `batches` (N batches of 200 pins, 15 a day), `single_day` (X pins for one day).
Needs the `META_IMAGE_API_KEY` secret (Meta Muse Image) and a public repo. Settings live in `config.json > bulk`
(posting window, pins per day, AI scene settings, disclosure wordings). Download the CSV from the run's
`pinterest-csv` artifact or from `bulk_out/latest`, then upload it in Pinterest: Settings > Bulk create Pins.
Pinterest schedules about 30 days ahead, so upload each file inside the dates shown on the run page.

## Price tiers (what it looks for)
Commission is a percentage, so the price decides what a sale is worth. The filters are set up for that:

| Tier | Price | Share | Filters |
|------|-------|-------|---------|
| Standard | $80 to $300 | about 8 of 10 pins | 500+ orders, 90%+ good rate, 5%+ commission |
| Premium | $300 to $800 | up to 2 of 10 pins a day | 100+ orders, 92%+ good rate, 5%+ commission |

Under $80 and over $800 are skipped. The keywords are sets, kits and bundles so the order total is bigger.
Ranking now also counts dollars earned per sale (price x commission), not just popularity.
Premium pins are capped by `settings.max_premium_per_day` (2) and `settings.premium_per_run` (1), tracked in
`posted.json` under `kitchen__premium`. Premium keywords are only searched when a premium slot is still free.
If runs say "0 candidates passed filters", lower `min_orders` or `min_commission` in `config.json`, or add keywords.
Each pin sent to Make now carries `tier` (standard or premium).

## Change what it posts
Edit `config.json`: keywords, filters (rating, orders, commission, price), board name, descriptions.

Never put the App Secret or webhook URL in any file. Keep them in GitHub Secrets.
