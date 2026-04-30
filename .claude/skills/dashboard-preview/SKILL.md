---
name: dashboard-preview
description: Serve dashboard/basis.html locally on http://localhost:8765/basis.html so the dashboard can be iterated against real production data without pushing to Yazam.io.
---

When invoked:

1. Confirm `dashboard/basis.html` has `GH_USER` set (not `"REPLACE_ME"`). If it's still the placeholder, ask the user for their GitHub username and edit the file accordingly.

2. Start a static server in the background from the dashboard directory:

   ```bash
   cd "/Users/michael/Elwood - work/perp-basis/dashboard" && \
     python3 -m http.server 8765
   ```

   Use `run_in_background: true` so the server keeps running while you continue working.

3. Tell the user to open `http://localhost:8765/basis.html` in their browser.

4. Remind them:
   - The dashboard fetches Parquet from `raw.githubusercontent.com` — it will not show data until the perp-basis repo is pushed to GitHub and at least one snapshot has been committed (or backfilled).
   - To stop the server, ask Claude to kill the background process.

5. If they report a JS error, ask them for the browser console output and inspect `dashboard/basis.html` to debug.
