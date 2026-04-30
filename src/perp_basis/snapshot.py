"""Capture entrypoint: snapshot all (venue, product) pairs in parallel and persist."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

from perp_basis.collectors import binance, hyperliquid, okx, yahoo_cme
from perp_basis.manifest import write_latest
from perp_basis.schema import PriceSnapshot
from perp_basis.storage import write_snapshot

log = logging.getLogger(__name__)

ALL_COLLECTORS = (binance, okx, hyperliquid, yahoo_cme)

# Comma-separated list of venues to skip — used by GitHub Actions because
# fapi.binance.com returns HTTP 451 to US-located runners.
_disabled = {v.strip() for v in os.environ.get("PERP_BASIS_DISABLE_VENUES", "").split(",") if v.strip()}
COLLECTORS = tuple(c for c in ALL_COLLECTORS if c.VENUE not in _disabled)
if _disabled:
    log.info("snapshot: disabled venues from env: %s", sorted(_disabled))


async def run_once() -> tuple[datetime, list[PriceSnapshot]]:
    ts = datetime.now(timezone.utc)
    async with httpx.AsyncClient(http2=False) as client:
        results = await asyncio.gather(
            *(c.collect(client, ts) for c in COLLECTORS),
            return_exceptions=True,
        )

    rows: list[PriceSnapshot] = []
    for c, res in zip(COLLECTORS, results, strict=True):
        if isinstance(res, BaseException):
            log.warning("%s: collector raised: %s", c.VENUE, res)
            continue
        rows.extend(res)
    return ts, rows


def _print_table(rows: list[PriceSnapshot]) -> None:
    if not rows:
        print("(no rows captured)")
        return
    rows_sorted = sorted(rows, key=lambda r: (r.product, r.venue))
    cols = ("venue", "product", "symbol", "mark_price", "last_price", "bid", "ask",
            "volume_24h", "funding_rate", "data_age_sec")
    widths = {c: max(len(c), max(len(str(getattr(r, c))) for r in rows_sorted)) for c in cols}
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in rows_sorted:
        print(" | ".join(str(getattr(r, c)).ljust(widths[c]) for c in cols))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ts, rows = asyncio.run(run_once())
    if not rows:
        log.error("snapshot: no rows captured from any venue; nothing to write")
        return 1
    path = write_snapshot(rows, ts)
    write_latest(rows)
    log.info("snapshot: wrote %s rows to %s", len(rows), path)
    _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
