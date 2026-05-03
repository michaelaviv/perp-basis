"""VolSnapshot dataclass and matching pyarrow schema.

Parallel to schema.py (PriceSnapshot). Captures one snapshot of a venue's
WTI option chain for a single expiry, expressed as a list of strike-level
quotes that can be aggregated into a vol smile downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pyarrow as pa

VOL_VENUES = ("cme_cvol", "cme_options", "kalshi", "polymarket")
VOL_PRODUCTS = ("wti",)


@dataclass(slots=True)
class StrikeQuote:
    """One row of a venue's option/prediction-market chain at one strike.

    For options (CME): strike is a single price, lo_strike/hi_strike None.
    For binary "above K" markets (Kalshi): strike == lo_strike, hi_strike None.
    For binary "in [a, b]" markets (Polymarket): lo_strike, hi_strike set;
    `strike` carries the midpoint for plotting convenience.
    """
    strike: float
    lo_strike: float | None = None
    hi_strike: float | None = None
    mid_iv: float | None = None
    bid_px: float | None = None
    ask_px: float | None = None
    last_px: float | None = None
    market_id: str | None = None
    raw_prob: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    volume_24h_usd: float | None = None


@dataclass(slots=True)
class VolSnapshot:
    """One venue's contribution to the WTI vol smile at one timestamp."""
    ts: datetime
    venue: str
    product: str
    expiry: str  # ISO date like "2026-05-30"
    underlying_px: float | None
    quotes: list[StrikeQuote] = field(default_factory=list)
    total_volume_24h_usd: float | None = None
    data_age_sec: int = 0


# Nested struct for the per-strike quotes. DuckDB-WASM reads pa.list_(pa.struct(...))
# transparently — verified during planning research.
STRIKE_QUOTE_STRUCT = pa.struct(
    [
        pa.field("strike", pa.float64()),
        pa.field("lo_strike", pa.float64()),
        pa.field("hi_strike", pa.float64()),
        pa.field("mid_iv", pa.float64()),
        pa.field("bid_px", pa.float64()),
        pa.field("ask_px", pa.float64()),
        pa.field("last_px", pa.float64()),
        pa.field("market_id", pa.string()),
        pa.field("raw_prob", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("open_interest", pa.float64()),
        pa.field("volume_24h_usd", pa.float64()),
    ]
)

ARROW_SCHEMA_VOL = pa.schema(
    [
        pa.field("ts", pa.timestamp("ns", tz="UTC")),
        pa.field("venue", pa.string()),
        pa.field("product", pa.string()),
        pa.field("expiry", pa.string()),
        pa.field("underlying_px", pa.float64()),
        pa.field("quotes", pa.list_(STRIKE_QUOTE_STRUCT)),
        pa.field("total_volume_24h_usd", pa.float64()),
        pa.field("data_age_sec", pa.int32()),
    ]
)


def vol_snapshots_to_table(rows: list[VolSnapshot]) -> pa.Table:
    cols: dict[str, list] = {f.name: [] for f in ARROW_SCHEMA_VOL}
    for r in rows:
        cols["ts"].append(r.ts)
        cols["venue"].append(r.venue)
        cols["product"].append(r.product)
        cols["expiry"].append(r.expiry)
        cols["underlying_px"].append(r.underlying_px)
        cols["quotes"].append([
            {
                "strike": q.strike,
                "lo_strike": q.lo_strike,
                "hi_strike": q.hi_strike,
                "mid_iv": q.mid_iv,
                "bid_px": q.bid_px,
                "ask_px": q.ask_px,
                "last_px": q.last_px,
                "market_id": q.market_id,
                "raw_prob": q.raw_prob,
                "volume": q.volume,
                "open_interest": q.open_interest,
                "volume_24h_usd": q.volume_24h_usd,
            }
            for q in r.quotes
        ])
        cols["total_volume_24h_usd"].append(r.total_volume_24h_usd)
        cols["data_age_sec"].append(r.data_age_sec)
    return pa.Table.from_pydict(cols, schema=ARROW_SCHEMA_VOL)
