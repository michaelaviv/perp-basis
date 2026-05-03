# Plan: WTI implied-vol comparison page (CME options vs Kalshi vs Polymarket)

## Context

Today's `dashboard/basis.html` compares perp-futures price vs CME futures price (basis). The user wants a parallel comparison for **implied volatility** on WTI — between traditional options markets (CME) and prediction-market-derived IV (Kalshi, Polymarket). Same intuition: if there's a meaningful gap between IV derived from option chains and IV implied by prediction markets, that's a tradeable dislocation. Like the basis dashboard, sources have asynchronous staleness (CME/ICE are typically delayed), so the pipeline must capture every venue at the same wall-clock tick and let the dashboard surface "as-of" times.

**User-locked design choices (already confirmed):**
- Same repo, new `dashboard/vol.html` page (perp-basis is now slightly misnamed — accept it, no rename).
- Single ~30-day forward tenor (find the closest CME/Kalshi/Polymarket expiries, allow some tolerance).
- **Multiple strikes per venue → fit a vol smile.** Not just a single ATM number.
- Data source: research recommended — see below.

**Data-source verdict (from research):**
- **Kalshi**: `https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXWTI` works unauthenticated. Series `KXWTI` is daily settle-price markets ("WTI > $99.99 on May 5, 2026"); many strikes per expiry, ideal smile data. `series_ticker=KXWTIM` likely exists for monthly. Each market gives `yes_bid`, `yes_ask`, `last_price`, `floor_strike`, `cap_strike`, `close_time`.
- **Polymarket**: `https://gamma-api.polymarket.com/events?tag_slug=oil&active=true&closed=false` works unauthenticated. "Will Crude Oil (CL) settle at $X-$Y in [month]" events expose ~8 bucketed-range markets per expiry — invertible to IV via lognormal CDF on the range. ("Hit" markets are P(max S_t > K) — different math, exclude from v1.)
- **CME options**: two paths, use both:
  - **CME CVOL** (the official 30-day implied-vol benchmark for CL options) — single number, end-of-day, free from CME's CVOL data feed. Use as the **anchor** ATM-30d IV.
  - **Yahoo `CL=F` option chain** via `yfinance.Ticker("CL=F").option_chain(expiry)` — gives strike/IV per chain. Known flaky for futures (sometimes returns equity options for the `WTI` ticker by mistake), but worth trying for the smile. If unreliable in practice, fall back to **Barchart's free per-strike scrape** as a v2 task.
- **ICE Brent**: skip for v1 — no comparable free public API.

## Architecture

Parallel pipeline next to the existing one. Zero changes to working basis code.

```
src/perp_basis/
├── schema.py                     ← unchanged (PriceSnapshot)
├── schema_vol.py                 ← NEW: VolSnapshot + StrikeQuote
├── collectors/                   ← unchanged
├── collectors_vol/               ← NEW
│   ├── __init__.py
│   ├── cme_options.py            ← yfinance CL=F option chain → strike-level IV
│   ├── cme_cvol.py               ← CVOL anchor (single ATM 30d number)
│   ├── kalshi_wti.py             ← KXWTI markets → (strike, P) → invert to IV
│   └── polymarket_wti.py         ← oil events → (range, P) → invert to IV
├── snapshot.py                   ← unchanged
├── snapshot_vol.py               ← NEW: orchestrates collectors_vol
├── iv_math.py                    ← NEW: lognormal IV inversion helpers (BS, brentq)
├── storage.py                    ← extend: write_snapshot_vol()
└── manifest.py                   ← extend: add options_dates / options_intraday_files

.github/workflows/
├── capture.yml                   ← unchanged
├── compact.yml                   ← extend: also compact data/options_snapshots/
└── capture-vol.yml               ← NEW: cron */20, separate concurrency group

data/
├── snapshots/, daily/            ← unchanged
├── options_snapshots/YYYY/MM/DD/HHMMSS.parquet
└── options_daily/YYYY/MM/DD.parquet

dashboard/
├── basis.html                    ← unchanged (or extract topbar nav)
└── vol.html                      ← NEW: mirrors basis.html shell, vol-specific charts
```

## Critical files

- **NEW: `src/perp_basis/schema_vol.py`** — `VolSnapshot(ts, venue, product, expiry, underlying_px, quotes: list[StrikeQuote], total_volume_24h_usd: float | None)` where `StrikeQuote = (strike, lo_strike|None, hi_strike|None, mid_iv|None, bid_px|None, ask_px|None, last_px|None, market_id, raw_prob|None, volume|None, open_interest|None, volume_24h_usd|None)`. Pydantic dataclass with slots, parallel to `PriceSnapshot`. Companion `ARROW_SCHEMA_VOL` with `pa.list_(pa.struct(...))` for the strike array (manifest gotcha doc says nested types work fine in DuckDB-WASM read). **Volume fields are critical** — a smile fitted on illiquid strikes is meaningless, and the dashboard needs them to (a) size scatter dots in the smile overlay so the eye discounts thin strikes, (b) compute per-venue 24h notional for the depth section, (c) gate any future trade-quality verdict.
- **NEW: `src/perp_basis/iv_math.py`** — pure functions: `iv_from_above_prob(prob, F, K, T) -> float` (Kalshi-style, single-strike binary), `iv_from_range_prob(prob, F, lo, hi, T) -> float` (Polymarket-style range). Both use `scipy.optimize.brentq` to solve `BS_lognormal_cdf(σ) == prob`. Risk-free rate set to 0 for v1 (small effect at 30d, simplifies).
- **NEW: `src/perp_basis/collectors_vol/cme_cvol.py`** — single-row collector returning the CVOL benchmark (no smile, just an anchor ATM-30d IV).
- **NEW: `src/perp_basis/collectors_vol/cme_options.py`** — yfinance option chain on `CL=F`. Pick the expiry closest to today+30d. Return one `VolSnapshot` with the strike list and IVs from yfinance (known unreliable; collector logs and continues on bad data, doesn't crash the pipeline).
- **NEW: `src/perp_basis/collectors_vol/kalshi_wti.py`** — fetch all `series_ticker=KXWTI` markets (paginated). Group by `close_time`, pick the expiry closest to 30d. For each market: prob = midpoint of (yes_bid, yes_ask), invert to IV using `iv_math.iv_from_above_prob(prob, F=current_CL=F, K=floor_strike, T=days_to_close/365)`. Populate `volume` from the market's `volume` field (in contracts) and `volume_24h_usd` from `volume_24h * mid_price` (Kalshi prices are 0–1 USD per contract, so notional is `contracts × price_in_dollars`).
- **NEW: `src/perp_basis/collectors_vol/polymarket_wti.py`** — fetch `tag_slug=oil` events with `active=true`. Filter to `settle at $X-$Y` events (exclude `hit` events for v1). Same expiry-matching logic. For each market: `prob = midpoint`, invert via `iv_math.iv_from_range_prob(prob, F, lo, hi, T)`. Populate `volume_24h_usd` from the market's `volumeNum` (already USDC ≈ USD) and `liquidity` from the `liquidityNum` field.
- **NEW: `src/perp_basis/snapshot_vol.py`** — mirror of `snapshot.py`. Same shared `httpx.AsyncClient`, same parallel-gather pattern, calls `write_snapshot_vol()` and updates the same `manifest.json`.
- **EDIT: `src/perp_basis/storage.py`** — add `write_snapshot_vol(rows: list[VolSnapshot], ts) -> Path` with the same partitioned-by-day layout, just under `data/options_snapshots/` and using `ARROW_SCHEMA_VOL`.
- **EDIT: `src/perp_basis/manifest.py`** — add `options_dates` and `options_intraday_files` fields. Single manifest stays the source of truth.
- **EDIT: `.github/workflows/compact.yml`** — also compact `data/options_snapshots/` into `data/options_daily/` at 00:05 UTC.
- **NEW: `.github/workflows/capture-vol.yml`** — cron `*/20 * * * *`. Separate concurrency group `capture-vol` so it can run alongside `capture` without conflict. Same git push retry pattern with `merge=ours`. Same Binance proxy env (not used by vol collectors but harmless).
- **NEW: `dashboard/vol.html`** — copy `basis.html` head + CSS + topbar shell + DuckDB-WASM bootstrapping. Add a small back-link `← Basis` in the topbar (and add `→ Vol` to basis.html's topbar in the same edit). Reuse the basis.html color palette so the two pages feel like one product:
  - CME chain → same green as Hyperliquid (`#29c481`)
  - Kalshi → new orange/amber (`#f59e0b`)
  - Polymarket → new purple (`#a78bfa`)
  - CME CVOL → dashed grey reference line (`#6b7280` dashed), since it's an anchor not a per-strike series
  - Reuse the existing `withGaps` helper for all line traces so cron pauses don't draw fake interpolation (the bug we just fixed for §7 of basis.html).
  
  Sections (6-section layout — §6 added for volume, mirrors basis-§7):
  - **§1 Now panel**: ATM-30d IV per venue (CME CVOL, CME chain ATM, Kalshi-derived ATM, Polymarket-derived ATM) with as-of time per source AND 24h notional volume per venue (so a thin venue is visible at a glance). Plus a single headline number: "GAP: pred-mkt avg vs CME = ±X vol pts".
  - **§2 Smile overlay**: scatter of (strike, IV) per venue at the latest snapshot, with a smoothing spline through each. **Dot size proportional to `log(1 + volume_24h_usd)` per strike** so the eye discounts illiquid points; the spline fit can also be weighted by volume. CME chain is the reference; Kalshi and Polymarket overlay. CME CVOL drawn as a horizontal dashed line at its single value. Tooltip shows strike, IV, and notional volume.
  - **§3 ATM-30d time series**: line chart per venue over the loaded range, using `withGaps`.
  - **§4 Dispersion**: `max(IV) − min(IV)` across venues at each timestamp.
  - **§5 Top dislocation events**: table of largest pred-mkt-vs-CME IV gaps in the range. Filter dropdowns for venue + strike bucket (ATM / OTM call wing / OTM put wing). Each row also shows the strike's 24h volume — a 10-vol-pt gap on a $100 market isn't a real opportunity. Add a "min volume" filter that defaults to $10k.
  - **§6 Vol-market depth · 24h notional volume per venue**: mirror of basis-§7. Per-venue total notional (sum across strikes for that snapshot's expiry) over time, with the same Live / Daily-avg toggle. Tells you whether prediction markets are deep enough to act on, and surfaces the weekday/weekend pattern just like basis-§7 does for perps.

## Implementation order (build incrementally, test at each step)

1. **`iv_math.py` + unit tests** — pure math, no I/O. Confirm round-trip: `iv_from_above_prob(BS_above_prob(σ=0.30, F, K, T), F, K, T) ≈ 0.30`. Ship-ready before any I/O is wired.
2. **`schema_vol.py`** + Arrow schema. Validate one fake row writes and reads back correctly.
3. **One collector at a time** — Kalshi first (simplest, most reliable API). Run via a `python -m perp_basis.snapshot_vol` once, inspect output.
4. **Polymarket collector** — same pattern, exclude `hit` markets.
5. **CME CVOL** — anchor number, hardcoded URL.
6. **CME options chain via yfinance** — last because flakiest. Build with `try/except` so a broken CL=F response logs a warning and continues.
7. **Storage + manifest extension**, write parquet, verify `manifest.json` includes both basis and options sections.
8. **Workflow** — `capture-vol.yml` triggered manually first via `gh workflow run`, then enable cron.
9. **Dashboard `vol.html`** — copy basis.html, gut the basis-specific sections, add vol sections one at a time. Verify each renders against real captured data.

## Verification

End-to-end smoke test before merging:

1. **Math**: `pytest tests/test_iv_math.py -v` — round-trip BS lognormal CDF for both `above` and `range` cases at σ ∈ {0.10, 0.30, 0.60}, T ∈ {7, 30, 90} days. Should round-trip to within 1e-6.
2. **Each collector standalone**: `python -m perp_basis.snapshot_vol` from the Mac (Binance proxy not needed for these). Should print one `VolSnapshot` per venue with non-empty `quotes`. Sanity-check that derived IVs are in [0.05, 2.0] — anything outside is a data bug.
3. **Storage**: confirm new parquet lands in `data/options_snapshots/...` and `manifest.json` lists it.
4. **Local dashboard**: serve via the dashboard-preview skill, open `http://localhost:8765/dashboard/vol.html`. All 5 sections render with real captured data. CME smile and prediction-market smile should be on the same axis ranges (sanity that we didn't mix up units of bps vs decimal vol).
5. **Workflow dry-run**: `gh workflow run capture-vol.yml`, watch with `gh run watch <id> --exit-status`, confirm parquet is committed and pushed.
6. **Manual sanity check on the smile shape**: prediction-market IV should typically be HIGHER than CME IV in the wings (prediction markets price tail risk richer due to thin books). If they're identical, suspect a unit/conversion bug.

## Out of scope (explicit non-goals for v1)

- ICE Brent (no free public API at parity with CME).
- Other commodities (gold, gas) — same code shape but separate venue mapping; defer until WTI is solid.
- Vol surface (multiple tenors at once) — single tenor per snapshot for v1.
- Trading-direction / setup-tag verdicts like §1 of basis.html — once the data is flowing, follow up with a vol-specific verdict (e.g., "predictions richer than chain by N vol pts").
- Backfill of historical IV — start collecting forward only.
