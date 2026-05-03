"""Kalshi WTI prediction-market collector.

Pulls all open markets in the user-chosen WTI series (default: KXWTIW, weekly
settle-price markets), groups by expiry, picks the expiry closest to ~30 days
out, and inverts each market's mid-probability into an implied volatility via
the lognormal CDF helpers in `iv_math`.

Series strike types we handle:
- "greater" : binary "WTI > floor_strike at close" → iv_from_above_prob
- "less"    : binary "WTI < cap_strike at close"   → iv_from_below_prob
              (computed as 1 - prob_above using the same math)
- "between" : binary "floor < WTI < cap at close"  → iv_from_range_prob

Underlying (F) is taken from CL=F via yfinance, matching what `yahoo_cme.py`
already uses for the basis pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from perp_basis import iv_math
from perp_basis.schema_vol import StrikeQuote, VolSnapshot

log = logging.getLogger(__name__)

VENUE = "kalshi"
PRODUCT = "wti"
SERIES_TICKER = "KXWTIW"  # weekly: closest tenor to 30d we can get from Kalshi
TARGET_TENOR_DAYS = 30
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _fetch_underlying_sync() -> float | None:
    """Current CL=F front-month price for use as F in IV inversion."""
    import yfinance as yf

    try:
        hist = yf.Ticker("CL=F").history(period="1d", interval="1m", auto_adjust=False)
        if hist is None or hist.empty:
            hist = yf.Ticker("CL=F").history(period="5d", interval="5m", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning("kalshi: yfinance CL=F fetch failed: %s", e)
        return None


async def _fetch_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    """Page through /markets?series_ticker=... collecting all open markets."""
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(20):  # safety cap on pagination depth
        params = {"series_ticker": series, "status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"{BASE}/markets", params=params, timeout=15.0)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or None
        if not cursor:
            break
    return out


def _mid_prob(market: dict) -> float | None:
    """Convert Kalshi cents bid/ask into a probability midpoint in [0, 1]."""
    bid, ask = market.get("yes_bid"), market.get("yes_ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0 / 100.0
    last = market.get("last_price")
    if last is not None:
        return float(last) / 100.0
    return None


def _quote_for(market: dict, F: float, T_years: float) -> StrikeQuote | None:
    prob = _mid_prob(market)
    if prob is None or not (0.0 < prob < 1.0):
        return None
    strike_type = market.get("strike_type")
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")

    iv: float | None = None
    strike: float | None = None
    lo: float | None = None
    hi: float | None = None
    if strike_type == "greater" and floor is not None:
        strike, lo = float(floor), float(floor)
        iv = iv_math.iv_from_above_prob(prob, F, strike, T_years)
    elif strike_type == "less" and cap is not None:
        # P(S < cap) = 1 - P(S > cap)  → invert the complement.
        strike, hi = float(cap), float(cap)
        iv = iv_math.iv_from_above_prob(1.0 - prob, F, strike, T_years)
    elif strike_type == "between" and floor is not None and cap is not None:
        lo, hi = float(floor), float(cap)
        strike = (lo + hi) / 2.0
        iv = iv_math.iv_from_range_prob(prob, F, lo, hi, T_years)
    else:
        return None

    if iv is None or iv != iv:  # NaN check
        return None

    bid_px = market.get("yes_bid")
    ask_px = market.get("yes_ask")
    last_px = market.get("last_price")
    vol = market.get("volume")
    vol24 = market.get("volume_24h")
    # Kalshi prices are 0-100 cents per contract; notional in USD = contracts * (price/100).
    # Use mid-price for the conversion when computing 24h notional.
    notional_usd = None
    if vol24 is not None and prob is not None:
        notional_usd = float(vol24) * prob

    return StrikeQuote(
        strike=strike,
        lo_strike=lo,
        hi_strike=hi,
        mid_iv=iv,
        bid_px=float(bid_px) / 100.0 if bid_px is not None else None,
        ask_px=float(ask_px) / 100.0 if ask_px is not None else None,
        last_px=float(last_px) / 100.0 if last_px is not None else None,
        market_id=market.get("ticker"),
        raw_prob=prob,
        volume=float(vol) if vol is not None else None,
        open_interest=float(market.get("open_interest")) if market.get("open_interest") is not None else None,
        volume_24h_usd=notional_usd,
    )


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    try:
        markets = await _fetch_markets(client, SERIES_TICKER)
    except Exception as e:
        log.warning("kalshi: fetch failed: %s", e)
        return []
    if not markets:
        log.info("kalshi: no markets returned for %s", SERIES_TICKER)
        return []

    F = await asyncio.to_thread(_fetch_underlying_sync)
    if F is None:
        log.warning("kalshi: no underlying CL=F price → cannot derive IV")
        return []

    # Group by expiry (close_time).
    by_expiry: dict[str, list[dict]] = {}
    for m in markets:
        ct = m.get("close_time")
        if not ct:
            continue
        by_expiry.setdefault(ct, []).append(m)
    if not by_expiry:
        return []

    # Pick the expiry whose distance from `ts + TARGET_TENOR_DAYS` is smallest.
    target = ts.timestamp() + TARGET_TENOR_DAYS * 86400
    def _dist(close_iso: str) -> float:
        return abs(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() - target)
    best_expiry = min(by_expiry, key=_dist)
    best_close = datetime.fromisoformat(best_expiry.replace("Z", "+00:00"))
    T_years = max((best_close.timestamp() - ts.timestamp()) / (365.25 * 86400), 1 / (365.25 * 24 * 60))

    quotes: list[StrikeQuote] = []
    for m in by_expiry[best_expiry]:
        q = _quote_for(m, F, T_years)
        if q is not None:
            quotes.append(q)

    total_vol_usd = sum(q.volume_24h_usd for q in quotes if q.volume_24h_usd is not None) or None

    return [
        VolSnapshot(
            ts=ts,
            venue=VENUE,
            product=PRODUCT,
            expiry=best_close.date().isoformat(),
            underlying_px=F,
            quotes=quotes,
            total_volume_24h_usd=total_vol_usd,
            data_age_sec=0,
        )
    ]
