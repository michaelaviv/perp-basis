"""CME front-month gold (GC=F) and WTI (CL=F) via Yahoo Finance through yfinance.

Delayed ~15 min during RTH. Outside CME RTH (esp. weekends) the most recent bar
may be stale by hours; `data_age_sec` exposes that staleness so the dashboard
can drop wildly stale CME points.

`ts` on each row is the bar's market time (so basis joins line up with the perp
print captured at the same wall-clock moment); `data_age_sec` is the lag
between that bar time and the snapshot's capture instant.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

import httpx

from perp_basis.config import symbols_for
from perp_basis.schema import PriceSnapshot

log = logging.getLogger(__name__)

VENUE = "cme_yahoo"


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        if math.isnan(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _fetch_sync(symbols: list[str]) -> dict[str, dict]:
    """Pull last 1m bar of OHLCV for each symbol from Yahoo. Synchronous (yfinance)."""
    import yfinance as yf  # imported lazily so unit tests can mock it

    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="1d", interval="1m", auto_adjust=False)
            if hist is None or hist.empty:
                hist = t.history(period="5d", interval="5m", auto_adjust=False)
            if hist is None or hist.empty:
                log.warning("yahoo: empty history for %s", sym)
                continue
            last = hist.iloc[-1]
            ts_index = hist.index[-1]
            # yfinance returns tz-aware index; normalize to UTC.
            try:
                ts_utc = ts_index.tz_convert("UTC").to_pydatetime()
            except Exception:
                ts_utc = ts_index.to_pydatetime().replace(tzinfo=timezone.utc)
            out[sym] = {
                "last": _f(last.get("Close")),
                "volume": _f(last.get("Volume")),
                "bar_ts_utc": ts_utc,
            }
        except Exception as e:
            log.warning("yahoo: %s failed: %s", sym, e)
    return out


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[PriceSnapshot]:
    """`client` is unused here (yfinance manages its own session) but kept for interface parity."""
    del client
    syms = symbols_for(VENUE)  # {"gold": "GC=F", "wti": "CL=F"}
    sym_list = list(syms.values())

    data = await asyncio.to_thread(_fetch_sync, sym_list)

    rows: list[PriceSnapshot] = []
    for product, sym in syms.items():
        bar = data.get(sym)
        if not bar:
            continue
        bar_ts = bar.get("bar_ts_utc")
        age = 0
        if bar_ts is not None:
            age = max(0, int((ts - bar_ts).total_seconds()))
        rows.append(
            PriceSnapshot(
                ts=bar_ts if bar_ts is not None else ts,
                venue=VENUE,
                product=product,
                symbol=sym,
                mark_price=bar["last"],
                last_price=bar["last"],
                bid=None,
                ask=None,
                volume_24h=bar.get("volume"),
                quote_volume_24h=None,
                funding_rate=None,
                open_interest=None,
                data_age_sec=age,
            )
        )
    return rows
