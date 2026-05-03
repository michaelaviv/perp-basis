"""OVX (CBOE Crude Oil Volatility Index) collector — TradFi 30-day-IV anchor.

OVX is CBOE's published 30-day implied-volatility index for USO (oil ETF)
options, the freely-available equivalent to CME's CVOL for crude. It's a
single number, not a per-strike smile — used in the dashboard as a horizontal
reference line under the prediction-market-derived smiles.

Yahoo carries it as `^OVX` and yfinance returns end-of-day bars (delayed
~15 min during US RTH; stale on weekends).

We emit one VolSnapshot with a single StrikeQuote at strike == underlying
spot, mid_iv == OVX value / 100.0 (OVX is quoted in vol points, e.g. 75.4
means 75.4% annualized).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from perp_basis.schema_vol import StrikeQuote, VolSnapshot

log = logging.getLogger(__name__)

VENUE = "ovx"
PRODUCT = "wti"
# OVX is a once-per-day EOD index (published ~4pm ET on US trading days).
# 30h threshold = "show yesterday's print during trading hours; skip when no
# new print for 2+ days (weekends, holidays)". Keeps the dashboard showing the
# last known reading on weekday mornings but matches CBOE/USO's market-closed
# behavior on weekends.
STALE_THRESHOLD_SEC = 30 * 3600


def _fetch_sync() -> tuple[float | None, float | None, datetime | None]:
    """Returns (ovx_value, cl_f_close, ovx_bar_ts_utc). Each may be None on failure."""
    import yfinance as yf

    ovx_val: float | None = None
    bar_ts: datetime | None = None
    try:
        h = yf.Ticker("^OVX").history(period="5d", interval="1d", auto_adjust=False)
        if h is not None and not h.empty:
            ovx_val = float(h["Close"].iloc[-1])
            ts_index = h.index[-1]
            try:
                bar_ts = ts_index.tz_convert("UTC").to_pydatetime()
            except Exception:
                bar_ts = ts_index.to_pydatetime().replace(tzinfo=timezone.utc)
    except Exception as e:
        log.warning("ovx: ^OVX fetch failed: %s", e)

    cl_val: float | None = None
    try:
        h = yf.Ticker("CL=F").history(period="1d", interval="1m", auto_adjust=False)
        if h is None or h.empty:
            h = yf.Ticker("CL=F").history(period="5d", interval="5m", auto_adjust=False)
        if h is not None and not h.empty:
            cl_val = float(h["Close"].iloc[-1])
    except Exception as e:
        log.warning("ovx: CL=F fetch failed: %s", e)

    return ovx_val, cl_val, bar_ts


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    del client  # yfinance manages its own session, kept for orchestrator parity
    ovx_val, cl_val, bar_ts = await asyncio.to_thread(_fetch_sync)
    if ovx_val is None:
        return []

    iv = ovx_val / 100.0  # ^OVX is in vol points, convert to decimal
    age = 0
    if bar_ts is not None:
        age = max(0, int((ts - bar_ts).total_seconds()))
        if age > STALE_THRESHOLD_SEC:
            log.info("ovx: last print is %dh old (market closed) → skipping snapshot",
                     age // 3600)
            return []

    quote = StrikeQuote(
        strike=cl_val if cl_val is not None else 0.0,  # at-spot
        mid_iv=iv,
        last_px=ovx_val,
        market_id="^OVX",
        raw_prob=None,
        volume=None,
        open_interest=None,
        volume_24h_usd=None,
    )
    # Expiry is conceptual — OVX is a 30-day forward measure, no explicit expiry date.
    # Use ts + 30d so the dashboard's tenor-matching logic groups it with the right window.
    expiry_iso = (ts.date().toordinal() + 30)
    from datetime import date as _date
    expiry = _date.fromordinal(expiry_iso).isoformat()

    return [
        VolSnapshot(
            ts=ts,
            venue=VENUE,
            product=PRODUCT,
            expiry=expiry,
            underlying_px=cl_val,
            quotes=[quote],
            total_volume_24h_usd=None,
            data_age_sec=age,
        )
    ]
