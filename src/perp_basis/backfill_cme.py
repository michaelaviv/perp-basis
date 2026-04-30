"""Backfill CME-only history (GC=F + CL=F) from Yahoo into data/daily/.

Produces synthetic snapshot rows at the requested interval, with venue=cme_yahoo and
the perp fields left null. Useful so the dashboard's basis chart can show CME-only
context for periods before the perps existed (Binance gold = Jan 2026, WTI = Apr 2026).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from perp_basis.config import DATA_DIR, symbols_for
from perp_basis.schema import PriceSnapshot
from perp_basis.storage import write_daily, snapshots_to_table
from perp_basis.manifest import write_manifest

log = logging.getLogger(__name__)
VENUE = "cme_yahoo"


def _f(v) -> float | None:
    try:
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except (TypeError, ValueError):
        return None


def fetch_history(symbol: str, start: str, interval: str) -> list[PriceSnapshot]:
    import yfinance as yf

    products_by_sym = {s: p for p, s in symbols_for(VENUE).items()}
    product = products_by_sym[symbol]

    t = yf.Ticker(symbol)
    hist = t.history(start=start, interval=interval, auto_adjust=False)
    if hist is None or hist.empty:
        log.warning("backfill: empty history for %s", symbol)
        return []

    rows: list[PriceSnapshot] = []
    for idx, row in hist.iterrows():
        try:
            ts_utc = idx.tz_convert("UTC").to_pydatetime()
        except Exception:
            ts_utc = idx.to_pydatetime().replace(tzinfo=timezone.utc)
        last = _f(row.get("Close"))
        if last is None:
            continue
        rows.append(
            PriceSnapshot(
                ts=ts_utc,
                venue=VENUE,
                product=product,
                symbol=symbol,
                mark_price=last,
                last_price=last,
                volume_24h=_f(row.get("Volume")),
                data_age_sec=0,
            )
        )
    return rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--interval", default="5m", help="Yahoo interval: 1m,5m,15m,1h,1d,...")
    ap.add_argument("--products", default="gold,wti")
    args = ap.parse_args()

    products = [p.strip() for p in args.products.split(",") if p.strip()]
    syms = [symbols_for(VENUE)[p] for p in products]

    all_rows: list[PriceSnapshot] = []
    for sym in syms:
        rows = fetch_history(sym, args.start, args.interval)
        log.info("backfill: %s → %d bars", sym, len(rows))
        all_rows.extend(rows)

    by_day: dict[date, list[PriceSnapshot]] = defaultdict(list)
    for r in all_rows:
        by_day[r.ts.date()].append(r)

    for day, rows in sorted(by_day.items()):
        rows.sort(key=lambda r: (r.ts, r.venue, r.product))
        out = write_daily(snapshots_to_table(rows), day.isoformat())
        log.info("backfill: %s → %s (%d rows)", day, out, len(rows))

    write_manifest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
