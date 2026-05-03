"""CBOE-listed USO option chain — best free per-strike IV smile for crude oil.

CME's CL=F futures don't expose an option chain on Yahoo, so we use USO
(the largest oil ETF, tracks WTI front-month) as a proxy. USO options are
listed on CBOE; their implied vols are comparable to but NOT identical to
CL option IVs because USO has roll/tracking-error baked in.

For each snapshot:
- Pick the expiry closest to ~30 days out.
- Pull every strike's IV (already inverted by Yahoo via Black-Scholes).
- Convert USO strike to a notional crude-price strike via current spot ratio
  (USO_strike / USO_spot * CL=F_spot) so the smile is plotted on the same
  x-axis as Kalshi/Polymarket markets.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

import httpx

from perp_basis.schema_vol import StrikeQuote, VolSnapshot

log = logging.getLogger(__name__)

VENUE = "cboe_uso"
PRODUCT = "wti"
ETF_TICKER = "USO"
TARGET_TENOR_DAYS = 30


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


def _fetch_chain_sync() -> dict:
    """Sync yfinance fetch. Returns dict with keys: uso_spot, cl_spot, expiry, calls, puts.
    Returns {} on failure. `calls`/`puts` are pandas DataFrames."""
    import yfinance as yf

    out: dict = {}
    try:
        uso_t = yf.Ticker(ETF_TICKER)
        h = uso_t.history(period="1d", interval="1m", auto_adjust=False)
        if h is None or h.empty:
            h = uso_t.history(period="5d", interval="5m", auto_adjust=False)
        if h is None or h.empty:
            log.warning("cboe_uso: no USO spot price")
            return {}
        out["uso_spot"] = float(h["Close"].iloc[-1])

        # CL=F spot for the strike-to-crude-price conversion
        cl_t = yf.Ticker("CL=F")
        h = cl_t.history(period="1d", interval="1m", auto_adjust=False)
        if h is None or h.empty:
            h = cl_t.history(period="5d", interval="5m", auto_adjust=False)
        out["cl_spot"] = float(h["Close"].iloc[-1]) if h is not None and not h.empty else None

        expiries = uso_t.options
        if not expiries:
            log.warning("cboe_uso: no expiries on %s", ETF_TICKER)
            return out
        # Pick closest to TARGET_TENOR_DAYS out
        now = datetime.now(timezone.utc).date().toordinal()
        target = now + TARGET_TENOR_DAYS
        def _dist(exp_str):
            d = datetime.fromisoformat(exp_str).date().toordinal()
            return abs(d - target)
        best = min(expiries, key=_dist)
        out["expiry"] = best
        chain = uso_t.option_chain(best)
        out["calls"] = chain.calls
        out["puts"] = chain.puts
    except Exception as e:
        log.warning("cboe_uso: chain fetch failed: %s", e)
    return out


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    del client
    data = await asyncio.to_thread(_fetch_chain_sync)
    uso_spot = data.get("uso_spot")
    cl_spot = data.get("cl_spot")
    expiry = data.get("expiry")
    calls = data.get("calls")
    puts = data.get("puts")
    if not (uso_spot and expiry and calls is not None):
        return []

    # Conversion factor from USO strike → notional crude price.
    # If we don't have CL=F spot, fall back to plotting on the USO strike axis
    # (1.0 ratio) — degraded but the IV values themselves are still correct.
    ratio = (cl_spot / uso_spot) if (cl_spot and uso_spot) else 1.0

    quotes: list[StrikeQuote] = []
    # We treat calls and puts at the same strike as one "ATM IV" data point per side;
    # for the smile it's traditional to use OTM on each side (puts below spot, calls above).
    for chain_df, side in ((calls, "C"), (puts, "P")):
        for _, row in chain_df.iterrows():
            iv = _f(row.get("impliedVolatility"))
            k_uso = _f(row.get("strike"))
            if iv is None or k_uso is None or iv <= 0:
                continue
            # OTM filter: keep calls above USO spot, puts below USO spot.
            if side == "C" and k_uso < uso_spot:
                continue
            if side == "P" and k_uso > uso_spot:
                continue
            k_cl = k_uso * ratio
            bid = _f(row.get("bid"))
            ask = _f(row.get("ask"))
            last = _f(row.get("lastPrice"))
            vol = _f(row.get("volume"))
            oi = _f(row.get("openInterest"))
            # Notional 24h volume in USD = contracts * lastPrice * 100 (US options multiplier).
            notional = None
            if vol is not None and last is not None:
                notional = vol * last * 100.0
            quotes.append(
                StrikeQuote(
                    strike=k_cl,
                    lo_strike=k_cl,
                    hi_strike=None,
                    mid_iv=iv,
                    bid_px=bid,
                    ask_px=ask,
                    last_px=last,
                    market_id=str(row.get("contractSymbol") or ""),
                    raw_prob=None,
                    volume=vol,
                    open_interest=oi,
                    volume_24h_usd=notional,
                )
            )

    if not quotes:
        return []

    total_vol_usd = sum(q.volume_24h_usd for q in quotes if q.volume_24h_usd is not None) or None

    return [
        VolSnapshot(
            ts=ts,
            venue=VENUE,
            product=PRODUCT,
            expiry=expiry,
            underlying_px=cl_spot,
            quotes=quotes,
            total_volume_24h_usd=total_vol_usd,
            data_age_sec=0,
        )
    ]
