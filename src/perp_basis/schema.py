"""PriceSnapshot dataclass and matching pyarrow schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pyarrow as pa

VENUES = ("binance", "okx", "hyperliquid", "cme_yahoo")
PRODUCTS = ("gold", "wti")


@dataclass(slots=True)
class PriceSnapshot:
    ts: datetime
    venue: str
    product: str
    symbol: str
    mark_price: float | None = None
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume_24h: float | None = None
    quote_volume_24h: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    data_age_sec: int = 0


ARROW_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("ns", tz="UTC")),
        pa.field("venue", pa.string()),
        pa.field("product", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("mark_price", pa.float64()),
        pa.field("last_price", pa.float64()),
        pa.field("bid", pa.float64()),
        pa.field("ask", pa.float64()),
        pa.field("volume_24h", pa.float64()),
        pa.field("quote_volume_24h", pa.float64()),
        pa.field("funding_rate", pa.float64()),
        pa.field("open_interest", pa.float64()),
        pa.field("data_age_sec", pa.int32()),
    ]
)


def snapshots_to_table(rows: list[PriceSnapshot]) -> pa.Table:
    cols: dict[str, list] = {f.name: [] for f in ARROW_SCHEMA}
    for r in rows:
        cols["ts"].append(r.ts)
        cols["venue"].append(r.venue)
        cols["product"].append(r.product)
        cols["symbol"].append(r.symbol)
        cols["mark_price"].append(r.mark_price)
        cols["last_price"].append(r.last_price)
        cols["bid"].append(r.bid)
        cols["ask"].append(r.ask)
        cols["volume_24h"].append(r.volume_24h)
        cols["quote_volume_24h"].append(r.quote_volume_24h)
        cols["funding_rate"].append(r.funding_rate)
        cols["open_interest"].append(r.open_interest)
        cols["data_age_sec"].append(r.data_age_sec)
    return pa.Table.from_pydict(cols, schema=ARROW_SCHEMA)
