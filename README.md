# perp-basis

Append-only historical price store + live basis dashboard for **Gold and WTI Crude Oil**, comparing crypto perpetuals against CME futures.

## What this captures

Every 5 minutes, a GitHub Actions cron job snapshots **8 series in parallel**:

| Venue           | Gold              | WTI Crude Oil     |
|-----------------|-------------------|-------------------|
| Binance USDT-M  | `XAUUSDT`         | `CLUSDT`          |
| OKX Swap        | `XAU-USDT-SWAP`   | `CL-USDT-SWAP`    |
| Hyperliquid     | `xyz:GOLD` (HIP-3)| `xyz:CL` (HIP-3)  |
| CME via Yahoo   | `GC=F`            | `CL=F`            |

For each, we record: mark price, last trade, bid/ask, 24h volume (base + quote), funding rate (perps), open interest (perps), and how stale the source data was at capture time.

CME data comes from Yahoo Finance (~15-min delayed during RTH). All other sources are live.

## What's in the repo

```
.
├── src/perp_basis/        # Python collector & pipeline
│   ├── collectors/           # one module per venue
│   ├── snapshot.py           # capture entrypoint (called by cron)
│   ├── compact.py            # daily roll-up entrypoint
│   ├── backfill_cme.py       # CME-only history backfill from Yahoo
│   ├── manifest.py           # data/manifest.json + data/latest.json writers
│   ├── storage.py, schema.py, config.py
│   └── ...
├── tests/                    # pytest, mocked HTTP
├── data/
│   ├── snapshots/YYYY/MM/DD/HHMMSS.parquet   # one tiny file per cron run
│   ├── daily/YYYY/MM/DD.parquet              # compacted, ~2,300 rows/day
│   ├── manifest.json                         # list of available daily files
│   └── latest.json                           # most recent snapshot for the dashboard
├── dashboard/basis.html      # static dashboard (drop into yazam.io repo)
├── DASHBOARD_SETUP.md        # how to deploy basis.html on yazam.io
├── notebooks/                # ad-hoc analysis (DuckDB + polars)
├── .github/workflows/
│   ├── capture.yml           # cron */5 * * * *
│   └── compact.yml           # cron 5 0 * * *
├── .claude/skills/           # project slash-commands for local workflow
└── pyproject.toml
```

## Snapshot schema

Each Parquet row:

| field              | type           | notes                                                      |
|--------------------|----------------|------------------------------------------------------------|
| `ts`               | timestamp[ns, UTC] | shared across all 8 rows of one snapshot                |
| `venue`            | string         | `binance` \| `okx` \| `hyperliquid` \| `cme_yahoo`         |
| `product`          | string         | `gold` \| `wti`                                            |
| `symbol`           | string         | venue-native symbol                                        |
| `mark_price`       | float64        | mark/index for perps; last trade for CME                   |
| `last_price`       | float64        |                                                            |
| `bid`, `ask`       | float64        | top of book (null for CME via Yahoo)                       |
| `volume_24h`       | float64        | rolling 24h base-asset volume                              |
| `quote_volume_24h` | float64        | rolling 24h quote (USD) volume                             |
| `funding_rate`     | float64        | current funding rate (perps only)                          |
| `open_interest`    | float64        | nullable                                                   |
| `data_age_sec`     | int32          | source-data staleness at capture (matters for Yahoo CME)   |

## Local development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Sanity-check collectors with mocked HTTP
.venv/bin/pytest

# Take one real snapshot (writes a Parquet under data/snapshots/...)
.venv/bin/python -m perp_basis.snapshot

# Roll yesterday's snapshots into one daily file
.venv/bin/python -m perp_basis.compact --date yesterday

# Backfill CME-only history (years available via Yahoo)
.venv/bin/python -m perp_basis.backfill_cme --start 2024-01-01 --interval 5m
```

There are also four Claude Code skills under `.claude/skills/`:

- `/snapshot-now` — run a snapshot and print the rows
- `/basis [gold|wti]` — print the latest pairwise basis from `data/latest.json`
- `/backfill-cme` — wrapper for the Yahoo backfill script
- `/dashboard-preview` — serve `dashboard/basis.html` on localhost

## Querying historical data

```python
import duckdb
con = duckdb.connect()
con.sql("""
  SELECT * FROM read_parquet('data/daily/**/*.parquet')
  WHERE product = 'gold' AND ts > now() - INTERVAL 7 DAY
""").show()
```

Or via the SQL CLI:

```bash
duckdb -c "SELECT venue, product, COUNT(*) FROM 'data/daily/**/*.parquet' GROUP BY 1,2"
```

## Live dashboard

Deployed to **https://yazam.io/basis.html** as a single static HTML file with DuckDB-WASM + Plotly. It fetches Parquet from this repo via `raw.githubusercontent.com` and queries client-side. See [`DASHBOARD_SETUP.md`](DASHBOARD_SETUP.md) for the one-time deploy.

Tabs:
1. **Basis time series** — basis (bps) per perp venue vs CME, gold + WTI side by side.
2. **Top dislocation events** — 50 largest |basis| events with timestamp, funding, and volume context.
3. **Funding vs basis scatter** — checks whether funding pulls perps to the underlying.
4. **Volume regime overlay** — total perp volume under |basis|, to spot dislocations clustered in high-volume regimes.

## How the cron handles concurrency

GitHub Actions cron is best-effort (often 5–15 min late under load) and runs can overlap. The pipeline avoids races by writing **one tiny Parquet per snapshot** under `data/snapshots/`, then compacting once a day. Two concurrent runs simply produce two files with different timestamps. Push conflicts are handled by `git pull --rebase` with up to 3 retries.

## Operational notes

- **Repo must be public** for the dashboard's unauthenticated fetch from `raw.githubusercontent.com`.
- **Storage growth**: ~10 MB/year compressed in Parquet. Negligible.
- **Cron rate**: 5-min minimum on GitHub Actions (free tier). For true 1-min cadence move the snapshot job to an always-on runner (fly.io / Render / a tiny VM) — code is already async-ready.
- **Adding a new product**: add `(venue, product) → symbol` rows in `src/perp_basis/config.py`, extend the relevant collector(s) (each one already returns multiple rows in one call), and the rest of the pipeline + dashboard pick it up automatically.
