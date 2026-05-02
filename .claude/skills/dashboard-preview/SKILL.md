---
name: dashboard-preview
description: Serve dashboard/basis.html locally on http://localhost:8765/dashboard/basis.html so the dashboard can be iterated against real production data without pushing to Yazam.io.
---

When invoked:

1. Confirm `dashboard/basis.html` has `GH_USER` set (not `"REPLACE_ME"`). If it's still the placeholder, ask the user for their GitHub username and edit the file accordingly.

2. Start a static server in the background from the **project root** (NOT `dashboard/`). The dashboard's `IS_LOCAL` mode fetches `../data/...` relative to `basis.html`, which only resolves correctly when the server is rooted at the project root:

   ```bash
   cd "/Users/michael/Elwood - work/perp-basis" && \
     python3 -m http.server 8765
   ```

   Use `run_in_background: true` so the server keeps running while you continue working.

   If port 8765 is already bound by a stale Python http.server from an earlier session, kill it (`lsof -nP -iTCP:8765 -sTCP:LISTEN | awk 'NR==2 {print $2}' | xargs kill`) and restart.

3. Tell the user to open `http://localhost:8765/dashboard/basis.html` in their browser.

4. Remind them:
   - The dashboard reads Parquet from the local `data/` directory in `IS_LOCAL` mode — it will not show data until at least one snapshot exists locally (run a `snapshot-now` or `backfill-cme` first if `data/latest.json` is missing).
   - To stop the server, ask Claude to kill the background process.

5. If they report a JS error, ask them for the browser console output and inspect `dashboard/basis.html` to debug.
