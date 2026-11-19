# Pinterest pin pipeline (AliExpress -> Make -> Pinterest)

Every run: searches AliExpress for products in each niche in `config.json`, keeps the well-reviewed
ones, builds a 1000x1500 pin image, and sends title / description / affiliate link / image to your
Make webhook, which publishes the pin.

## Setup (once)
1. Put these files in a GitHub repo, keeping the `.github/workflows/pins.yml` path.
2. Settings > Actions > General > Workflow permissions > **Read and write**.
3. Settings > Secrets and variables > Actions, add: `AE_APP_KEY`, `AE_APP_SECRET`,
   `AE_TRACKING_ID`, `MAKE_WEBHOOK_URL`.

## First runs
1. Actions > Make pins > Run workflow, leave **dry_run** ticked, limit 2. Check the log and the
   `pin-previews` download. Nothing is sent to Make.
2. In Make, open the scenario and click **Run once**, then run the workflow again with dry_run
   **unticked**, limit 1. Make learns the fields; finish mapping the modules.
3. Turn the Make scenario on. The workflow then runs 3x a day by itself (times are UTC, edit
   `pins.yml` to change them).

## Change what it posts
Edit `config.json`: keywords, filters (rating, orders, commission, price), board name, descriptions,
pins per run. Add another niche by copying the niche block.

Never put the App Secret or webhook URL in any file. Keep them in GitHub Secrets.
