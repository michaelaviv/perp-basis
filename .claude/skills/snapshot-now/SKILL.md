---
name: snapshot-now
description: Run a perp-basis snapshot locally against the live exchange APIs and print all 8 captured rows (Gold + WTI × 4 venues). Use to sanity-check collectors outside the GitHub Actions cron.
---

When invoked:

1. Run the snapshot from the repo root using the project venv:

   ```bash
   cd "/Users/michael/Elwood - work/perp-basis" && \
     .venv/bin/python -m perp_basis.snapshot
   ```

2. The script prints a table grouped by (product, venue) and writes a Parquet file under `data/snapshots/YYYY/MM/DD/HHMMSS.parquet` plus refreshes `data/latest.json`.

3. Report to the user:
   - How many rows were captured (8 = full success).
   - Which venue/product pairs failed (if any).
   - The current basis vs CME for each perp venue, computed from the printed prices.

If the venv doesn't exist yet, set it up first with:

```bash
cd "/Users/michael/Elwood - work/perp-basis" && \
  python3.11 -m venv .venv && \
  .venv/bin/pip install -e ".[dev]"
```
