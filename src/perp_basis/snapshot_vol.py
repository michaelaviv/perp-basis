"""Vol-snapshot entrypoint: run every vol collector in parallel and persist.

Mirror of `snapshot.py` but for VolSnapshot rows. Fans out to:
  - cboe_uso     (USO option chain via yfinance)
  - kalshi_wti   (KXWTIW prediction markets)
  - polymarket_wti (oil settle events)
  - ovx          (single-number 30-day IV anchor)

Writes one Parquet under data/options_snapshots/YYYY/MM/DD/HHMMSS.parquet
and rewrites manifest.json so the dashboard can find it immediately.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

import httpx

from perp_basis.collectors_vol import cboe_uso, kalshi_wti, ovx, polymarket_wti
from perp_basis.manifest import write_manifest, write_vol_latest
from perp_basis.schema_vol import VolSnapshot
from perp_basis.storage import write_snapshot_vol

log = logging.getLogger(__name__)

VOL_COLLECTORS = (cboe_uso, kalshi_wti, ovx, polymarket_wti)


async def run_once() -> tuple[datetime, list[VolSnapshot]]:
    ts = datetime.now(timezone.utc)
    async with httpx.AsyncClient(http2=False) as client:
        results = await asyncio.gather(
            *(c.collect(client, ts) for c in VOL_COLLECTORS),
            return_exceptions=True,
        )

    rows: list[VolSnapshot] = []
    for c, res in zip(VOL_COLLECTORS, results, strict=True):
        if isinstance(res, BaseException):
            log.warning("%s: collector raised: %s", c.VENUE, res)
            continue
        rows.extend(res)
    return ts, rows


def _print_table(rows: list[VolSnapshot]) -> None:
    if not rows:
        print("(no vol rows captured)")
        return
    rows_sorted = sorted(rows, key=lambda r: r.venue)
    cols = ("venue", "product", "expiry", "underlying_px", "n_quotes",
            "atm_iv", "total_vol_usd", "data_age_sec")
    width = {c: len(c) for c in cols}
    flat = []
    for r in rows_sorted:
        atm = next(
            (q.mid_iv for q in r.quotes
             if q.mid_iv is not None and r.underlying_px is not None
             and abs(q.strike - r.underlying_px) < 5.0),
            None,
        )
        row = {
            "venue": r.venue,
            "product": r.product,
            "expiry": r.expiry,
            "underlying_px": f"{r.underlying_px:.2f}" if r.underlying_px else "—",
            "n_quotes": str(len(r.quotes)),
            "atm_iv": f"{atm*100:.1f}%" if atm is not None else "—",
            "total_vol_usd": f"${r.total_volume_24h_usd:,.0f}" if r.total_volume_24h_usd else "—",
            "data_age_sec": str(r.data_age_sec),
        }
        flat.append(row)
        for c in cols:
            width[c] = max(width[c], len(row[c]))
    line = " | ".join(c.ljust(width[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in flat:
        print(" | ".join(r[c].ljust(width[c]) for c in cols))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ts, rows = asyncio.run(run_once())
    if not rows:
        log.error("snapshot_vol: no rows captured from any venue; nothing to write")
        return 1
    path = write_snapshot_vol(rows, ts)
    write_vol_latest(rows, capture_ts=ts)
    write_manifest()  # rewrites with both basis and options sections
    log.info("snapshot_vol: wrote %s rows to %s", len(rows), path)
    _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
