# Dashboard setup — deploying `basis.html` to Yazam.io

The dashboard is one self-contained HTML file: `dashboard/basis.html`. It lives in
this repo for review, but its **deployment target is the Yazam.io GitHub Pages
repo**. It loads its data over the public internet from this repo via
`raw.githubusercontent.com`.

## One-time setup

### 1. Make this repo (`perp-basis`) public

The dashboard fetches Parquet without authentication. The repo must therefore be
public:

- GitHub → `perp-basis` → **Settings** → **General** → scroll to *Danger Zone*
  → **Change visibility** → **Make public**.

(The data is just market prices — there is nothing private here.)

### 2. Push at least one snapshot to `main`

The dashboard reads `data/manifest.json` and `data/latest.json`. These don't
exist until the capture workflow has run at least once. Either:

- Wait for the cron `*/5` to fire, **or**
- Trigger it manually: GitHub → `perp-basis` → **Actions** → *capture* →
  **Run workflow**.

### 3. Drop `basis.html` into the Yazam.io repo

In the Yazam.io repo (assumed: plain static HTML on GitHub Pages):

1. Copy `dashboard/basis.html` to the **repo root** of Yazam.io (or any path
   you prefer, e.g. `/basis.html` or `/dashboards/basis.html`).
2. **Edit the two config lines at the top of the script tag** in `basis.html`:

   ```js
   const GH_USER = "REPLACE_ME";          // ← your GitHub username/org
   const GH_REPO = "perp-basis";
   ```

3. Commit and push to Yazam.io's `main`. GitHub Pages republishes within ~1 min.
4. Visit `https://yazam.io/basis.html`.

### 4. (Optional) Confirm GitHub Pages is enabled on Yazam.io

If `https://yazam.io/basis.html` returns 404, check Pages is enabled:

- Yazam.io repo → **Settings** → **Pages** → **Source** = "Deploy from a branch"
  → branch `main`, folder `/ (root)` → **Save**.
- The Pages settings page will show "Your site is live at https://yazam.io".

## How the data flow works

```
perp-basis/main/data/                              yazam.io GitHub Pages
├── manifest.json    ─┐                               │
├── latest.json      ─┤── raw.githubusercontent.com ──┤── basis.html
└── daily/YYYY/MM/   ─┘                               │   (DuckDB-WASM in browser)
    └── DD.parquet                                    │
```

Every reload of `https://yazam.io/basis.html`:
1. Fetches `latest.json` → renders the "Now" panel.
2. Fetches `manifest.json` → learns which days have data.
3. For the selected date range, fetches each day's `daily/YYYY/MM/DD.parquet`.
4. Registers the buffers with DuckDB-WASM and runs SQL queries client-side.

`raw.githubusercontent.com` serves with `Access-Control-Allow-Origin: *`, so no
proxy is needed.

## Local preview before deploying

Use the project skill `/dashboard-preview` (or run manually):

```bash
cd dashboard && python3 -m http.server 8765
# then open http://localhost:8765/basis.html
```

You can iterate on `basis.html` locally and only push to Yazam.io when ready.

## Updating the dashboard later

The dashboard ships once. Subsequent data updates require **no Yazam.io
deploys** — the cron job pushes new Parquet to the perp-basis repo and the
dashboard picks them up on the next page reload.

You only need to redeploy `basis.html` if you change the dashboard code itself.
