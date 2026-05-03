"""Kalshi WTI prediction-market collector.

Pulls all open markets across the configured WTI series (KXWTI daily +
KXWTIW weekly), groups by close_time, and picks the expiry with the highest
24h notional inside a tradeability window. Inverts each chosen market's
mid-probability into an implied volatility via the lognormal CDF helpers in
`iv_math`.

Why max-volume across multiple series: KXWTIW alone runs ~$3k 24h notional;
KXWTI dailies run an order of magnitude higher when active. Picking the
fattest expiry per snapshot keeps the IV signal tradeable instead of
chasing a fixed 30d tenor that often has near-zero size.

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
# Pull both daily (KXWTI) and weekly (KXWTIW) series; pick the fattest
# expiry per snapshot. KXWTI is typically ~10× more liquid than KXWTIW
# but only goes a few days out, so we need both for full tenor coverage.
SERIES_TICKERS = ("KXWTI", "KXWTIW")
# Tradeability window — drop expiries inside last-day chop (intraday quotes
# get jumpy when there is <24h to settle and the IV inversion explodes for
# tiny prob shifts) or too far out (Kalshi WTI series rarely list past ~60d
# and any liquidity is vestigial when they do). KXWTI dailies close 1-2d
# from listing so MIN_DTE must be small to capture them.
MIN_DTE_DAYS = 1.0
MAX_DTE_DAYS = 60
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _fetch_underlying_sync() -> float | None:
    """Current CL=F front-month price for use as F in IV inversion."""
    import yfinance as yf

    from perp_basis.collectors_vol._yf_shared import YF_LOCK

    with YF_LOCK:
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


def _to_float(v) -> float | None:
    """Kalshi's *_fp / *_dollars fields are stringified fixed-point numbers."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _mid_prob(market: dict) -> float | None:
    """Convert Kalshi cents bid/ask into a probability midpoint in [0, 1]."""
    bid, ask = market.get("yes_bid"), market.get("yes_ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0 / 100.0
    last = market.get("last_price")
    if last is not None:
        return float(last) / 100.0
    return None


async def _yes_bid_ask_from_orderbook(client: httpx.AsyncClient, ticker: str
                                      ) -> tuple[float | None, float | None]:
    """The /markets summary often has stale or null bid/ask even when there are
    real resting orders. Hit /orderbook directly and derive the YES side from
    both yes_dollars (YES bids) and no_dollars (NO bids → 1 − price = YES ask).
    Returns (yes_bid, yes_ask) in dollars (i.e., probability units in [0, 1])."""
    try:
        r = await client.get(f"{BASE}/markets/{ticker}/orderbook", timeout=10.0)
        r.raise_for_status()
        ob = r.json().get("orderbook_fp", {}) or {}
    except Exception:
        return None, None

    yes_bids = ob.get("yes_dollars") or []
    no_bids = ob.get("no_dollars") or []
    # Each row is ["0.34", "100.00"] — price as string, qty as string. Take max price.
    def _max_price(rows):
        if not rows:
            return None
        try:
            return max(float(r[0]) for r in rows if r and r[0] is not None)
        except (ValueError, TypeError, IndexError):
            return None
    yes_bid = _max_price(yes_bids)
    no_bid_max = _max_price(no_bids)
    yes_ask = (1.0 - no_bid_max) if no_bid_max is not None else None
    return yes_bid, yes_ask


def _quote_for(market: dict, F: float, T_years: float) -> StrikeQuote | None:
    prob = _mid_prob(market)
    if prob is None or not (0.0 < prob < 1.0):
        return None
    # Liquidity sanity: drop quotes where the bid-ask spread is so wide relative
    # to the midpoint that the "price" is effectively unknown. On Kalshi, this
    # catches body-of-distribution markets where both sides hit the $0.01
    # minimum-tick floor — they all look like prob ≈ 0.03 regardless of the
    # underlying, and the inversion produces a misleading V-shape on the smile.
    bid_c, ask_c = market.get("yes_bid"), market.get("yes_ask")
    if bid_c is not None and ask_c is not None and prob > 0:
        spread = (ask_c - bid_c) / 100.0  # convert cents → dollars
        # Harmonized with polymarket_wti.py at 33% (geometric mean of the
        # earlier 25%/50% asymmetry). The IV sanity band catches what gets
        # through; matching gates avoids biasing the Kalshi-vs-Polymarket
        # comparison toward Polymarket because Kalshi was filtered tighter.
        if spread / prob > 0.33:
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
    # Reject IVs far outside any plausible WTI vol regime — typical 10-150%,
    # so [5%, 250%] is a generous sanity band. Anything outside is an
    # inversion artifact (e.g. a tiny-prob body market that math thinks
    # implies σ < 5%).
    if iv < 0.05 or iv > 2.5:
        return None

    # Bid/ask come from the /markets summary as cents (or are backfilled from
    # /orderbook in cents by `_yes_bid_ask_from_orderbook` upstream).
    bid_px = market.get("yes_bid")
    ask_px = market.get("yes_ask")
    # As of the 2026 Kalshi API rev, summary fields use *_fp suffixes (string
    # fixed-point) for quantities and *_dollars for prices. The pre-rev names
    # (last_price, volume, volume_24h, open_interest) are gone, so reading the
    # old keys silently returned None and we lost all volume + last-trade data.
    last_px = _to_float(market.get("last_price_dollars"))  # already in dollars
    vol = _to_float(market.get("volume_fp"))               # contracts
    vol24 = _to_float(market.get("volume_24h_fp"))         # contracts
    oi = _to_float(market.get("open_interest_fp"))         # contracts
    # Notional USD ≈ contracts * mid-prob (each contract pays $1 on YES).
    notional_usd = vol24 * prob if vol24 is not None and prob is not None else None

    return StrikeQuote(
        strike=strike,
        lo_strike=lo,
        hi_strike=hi,
        mid_iv=iv,
        bid_px=float(bid_px) / 100.0 if bid_px is not None else None,
        ask_px=float(ask_px) / 100.0 if ask_px is not None else None,
        last_px=last_px,
        market_id=market.get("ticker"),
        raw_prob=prob,
        volume=vol,
        open_interest=oi,
        volume_24h_usd=notional_usd,
    )


def _expiry_notional_usd(markets: list[dict]) -> float:
    """Sum contracts * mid-price across markets in one expiry as a quick
    pre-filter ranking. Bid/ask come from the /markets summary in dollars
    (the orderbook backfill happens later, after expiry selection)."""
    total = 0.0
    for m in markets:
        v = _to_float(m.get("volume_24h_fp"))
        if v is None:
            continue
        bid = _to_float(m.get("yes_bid_dollars"))
        ask = _to_float(m.get("yes_ask_dollars"))
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        else:
            mid = _to_float(m.get("last_price_dollars"))
        if mid is None:
            continue
        total += v * mid
    return total


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    fetches = await asyncio.gather(
        *(_fetch_markets(client, s) for s in SERIES_TICKERS),
        return_exceptions=True,
    )
    markets: list[dict] = []
    for series, result in zip(SERIES_TICKERS, fetches, strict=True):
        if isinstance(result, BaseException):
            log.warning("kalshi: fetch %s failed: %s", series, result)
            continue
        markets.extend(result)
    if not markets:
        log.info("kalshi: no markets returned across %s", SERIES_TICKERS)
        return []

    F = await asyncio.to_thread(_fetch_underlying_sync)
    if F is None:
        log.warning("kalshi: no underlying CL=F price → cannot derive IV")
        return []

    # Group by expiry (close_time), filter to the tradeability window.
    ts_sec = ts.timestamp()
    def _dte_days(close_iso: str) -> float:
        return (datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() - ts_sec) / 86400

    by_expiry: dict[str, list[dict]] = {}
    for m in markets:
        ct = m.get("close_time")
        if not ct:
            continue
        dte = _dte_days(ct)
        if dte < MIN_DTE_DAYS or dte > MAX_DTE_DAYS:
            continue
        by_expiry.setdefault(ct, []).append(m)
    if not by_expiry:
        log.info("kalshi: no expiries in window [%dd, %dd]", MIN_DTE_DAYS, MAX_DTE_DAYS)
        return []

    # Pick the expiry with the highest 24h notional inside the window.
    best_expiry = max(by_expiry, key=lambda e: _expiry_notional_usd(by_expiry[e]))
    best_close = datetime.fromisoformat(best_expiry.replace("Z", "+00:00"))
    log.info("kalshi: chose expiry %s (~%.0fd, ~$%.0f 24h notional) from %d candidates",
             best_close.date(), _dte_days(best_expiry),
             _expiry_notional_usd(by_expiry[best_expiry]), len(by_expiry))
    T_years = max((best_close.timestamp() - ts.timestamp()) / (365.25 * 86400), 1 / (365.25 * 24 * 60))

    chosen = by_expiry[best_expiry]

    # The /markets summary often returns yes_bid/yes_ask=null even when there
    # are real resting orders. Backfill from the per-market /orderbook endpoint
    # in parallel, then patch the summary dicts before inversion.
    ob_results = await asyncio.gather(
        *(_yes_bid_ask_from_orderbook(client, m["ticker"]) for m in chosen),
        return_exceptions=True,
    )
    for m, ob in zip(chosen, ob_results, strict=True):
        if isinstance(ob, BaseException):
            continue
        ob_bid, ob_ask = ob
        # Express as cents (the /markets summary's units) so _mid_prob still works.
        if m.get("yes_bid") is None and ob_bid is not None:
            m["yes_bid"] = ob_bid * 100.0
        if m.get("yes_ask") is None and ob_ask is not None:
            m["yes_ask"] = ob_ask * 100.0

    quotes: list[StrikeQuote] = []
    for m in chosen:
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
