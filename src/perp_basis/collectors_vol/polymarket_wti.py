"""Polymarket WTI / CL prediction-market collector.

Pulls "What will Crude Oil (CL) settle at in [Month]?" events (multiple
range/above buckets per expiry), picks the event whose resolution date is
closest to ~30d out, and inverts each market's mid-probability into IV.

Skipped explicitly: "hit by end of [Month]" events — those are max-of-path
binaries (lookback options), not European binaries, and need a different
inversion than what `iv_math` provides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from perp_basis import iv_math
from perp_basis.schema_vol import StrikeQuote, VolSnapshot

log = logging.getLogger(__name__)

VENUE = "polymarket"
PRODUCT = "wti"
TARGET_TENOR_DAYS = 30
BASE = "https://gamma-api.polymarket.com"

# Title shape: "What will Crude Oil (CL) settle at in May 2026?" / "in May?"
SETTLE_EVENT_TITLE_RE = re.compile(r"settle at in ", re.IGNORECASE)
# Question-level strike parsers
RANGE_RE = re.compile(r"\$\s*([\d.]+)\s*-\s*\$\s*([\d.]+)")
ABOVE_RE = re.compile(r">\s*\$\s*([\d.]+)")
BELOW_RE = re.compile(r"<\s*\$\s*([\d.]+)")


def _fetch_underlying_sync() -> float | None:
    import yfinance as yf

    try:
        hist = yf.Ticker("CL=F").history(period="1d", interval="1m", auto_adjust=False)
        if hist is None or hist.empty:
            hist = yf.Ticker("CL=F").history(period="5d", interval="5m", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning("polymarket: yfinance CL=F fetch failed: %s", e)
        return None


def _parse_outcome_yes_index(outcomes_raw) -> int | None:
    """Polymarket sends `outcomes` as a JSON string like '[\"Yes\", \"No\"]'."""
    try:
        outs = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        for i, o in enumerate(outs):
            if str(o).strip().lower() == "yes":
                return i
    except Exception:
        return None
    return None


def _yes_mid_prob(market: dict) -> float | None:
    """Extract the YES-side midpoint probability in [0, 1]."""
    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    if bid is not None and ask is not None:
        try:
            return (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            pass
    last = market.get("lastTradePrice")
    if last is not None:
        try:
            return float(last)
        except (TypeError, ValueError):
            pass
    # Fallback: outcomePrices as a final resort.
    yes_i = _parse_outcome_yes_index(market.get("outcomes"))
    if yes_i is None:
        return None
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        return float(prices[yes_i])
    except Exception:
        return None


def _quote_for(market: dict, F: float, T_years: float) -> StrikeQuote | None:
    prob = _yes_mid_prob(market)
    if prob is None or not (0.0 < prob < 1.0):
        return None
    q_text = market.get("question") or ""

    iv: float | None = None
    strike: float | None = None
    lo: float | None = None
    hi: float | None = None

    m = RANGE_RE.search(q_text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        strike = (lo + hi) / 2.0
        iv = iv_math.iv_from_range_prob(prob, F, lo, hi, T_years)
    else:
        m = ABOVE_RE.search(q_text)
        if m:
            strike, lo = float(m.group(1)), float(m.group(1))
            iv = iv_math.iv_from_above_prob(prob, F, strike, T_years)
        else:
            m = BELOW_RE.search(q_text)
            if m:
                strike, hi = float(m.group(1)), float(m.group(1))
                iv = iv_math.iv_from_above_prob(1.0 - prob, F, strike, T_years)
            else:
                return None

    if iv is None or iv != iv:  # NaN
        return None
    # Sanity band — drop inversion artifacts outside any plausible vol regime.
    if iv < 0.05 or iv > 2.5:
        return None
    # Harmonized with kalshi_wti.py at 33% (geometric mean of the earlier
    # 25%/50% asymmetry). The IV sanity band catches what gets through;
    # matching gates avoids biasing the Kalshi-vs-Polymarket comparison.
    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    try:
        if bid is not None and ask is not None and prob > 0:
            spread = float(ask) - float(bid)
            if spread / prob > 0.33:
                return None
    except (TypeError, ValueError):
        pass

    # Polymarket reports `volume24hr` as raw USDC traded (both YES and NO sides
    # of the binary). To make this comparable to Kalshi's `contracts × mid_price`
    # notional, multiply by the YES-side mid-prob — the equivalent "amount of
    # YES exposure transferred". Without this conversion, low-prob markets
    # appear ~10× their true notional and §6 vol depth misleads.
    vol24 = market.get("volume24hr")
    notional_usd = float(vol24) * prob if (vol24 is not None and prob is not None) else None

    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    last = market.get("lastTradePrice")
    return StrikeQuote(
        strike=strike,
        lo_strike=lo,
        hi_strike=hi,
        mid_iv=iv,
        bid_px=float(bid) if bid is not None else None,
        ask_px=float(ask) if ask is not None else None,
        last_px=float(last) if last is not None else None,
        market_id=str(market.get("conditionId") or market.get("id") or ""),
        raw_prob=prob,
        volume=float(market["volumeNum"]) if market.get("volumeNum") is not None else None,
        open_interest=None,
        volume_24h_usd=notional_usd,
    )


async def _fetch_oil_events(client: httpx.AsyncClient) -> list[dict]:
    params = {"tag_slug": "oil", "active": "true", "closed": "false", "limit": 100}
    r = await client.get(f"{BASE}/events", params=params, timeout=15.0)
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else body.get("data", [])


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    try:
        events = await _fetch_oil_events(client)
    except Exception as e:
        log.warning("polymarket: fetch failed: %s", e)
        return []
    if not events:
        return []

    # Keep only "settle at in <month>" events; skip "hit by end of <month>" etc.
    settle_events = [e for e in events if SETTLE_EVENT_TITLE_RE.search(e.get("title") or "")]
    if not settle_events:
        log.info("polymarket: no 'settle at' oil events in current set")
        return []

    F = await asyncio.to_thread(_fetch_underlying_sync)
    if F is None:
        log.warning("polymarket: no underlying CL=F price → cannot derive IV")
        return []

    # Pick event whose endDate is closest to ts + TARGET_TENOR_DAYS.
    target = ts.timestamp() + TARGET_TENOR_DAYS * 86400
    def _dist(e: dict) -> float:
        end = e.get("endDate")
        if not end:
            return float("inf")
        try:
            return abs(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - target)
        except Exception:
            return float("inf")
    best = min(settle_events, key=_dist)
    end_iso = best.get("endDate")
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else None
    if end_dt is None:
        return []
    T_years = max((end_dt.timestamp() - ts.timestamp()) / (365.25 * 86400), 1 / (365.25 * 24 * 60))

    quotes: list[StrikeQuote] = []
    for m in best.get("markets", []):
        q = _quote_for(m, F, T_years)
        if q is not None:
            quotes.append(q)

    total_vol_usd = sum(q.volume_24h_usd for q in quotes if q.volume_24h_usd is not None) or None

    return [
        VolSnapshot(
            ts=ts,
            venue=VENUE,
            product=PRODUCT,
            expiry=end_dt.date().isoformat(),
            underlying_px=F,
            quotes=quotes,
            total_volume_24h_usd=total_vol_usd,
            data_age_sec=0,
        )
    ]
