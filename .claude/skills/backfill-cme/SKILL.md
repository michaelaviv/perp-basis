---
name: backfill-cme
description: Backfill CME-only history (GC=F gold + CL=F WTI) from Yahoo Finance into data/daily/. Use to seed historical context for periods before the perps existed.
---

Arguments (parsed from the user's message; ask if missing):
- `--start YYYY-MM-DD` (required, default 2024-01-01)
- `--interval` (default `5m` — one of `1m|5m|15m|1h|1d`)
- `--products` (default `gold,wti`)

When invoked:

1. Run:

   ```bash
   cd "/Users/michael/Elwood - work/perp-basis" && \
     .venv/bin/python -m perp_basis.backfill_cme \
       --start <START> --interval <INTERVAL> --products <PRODUCTS>
   ```

2. The script writes one Parquet file per day under `data/daily/YYYY/MM/DD.parquet` and rebuilds `data/manifest.json`.

3. Report to the user:
   - Number of bars fetched per symbol.
   - Date range covered in `data/daily/`.
   - That commits are NOT made automatically — they should `git add data/ && git commit -m "backfill cme ..."` and push.

Yahoo limitations to mention if relevant:
- `1m` interval: only last 7 days available.
- `5m`–`1h`: ~60 days.
- `1d`: years.
