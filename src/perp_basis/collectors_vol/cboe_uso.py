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

Market hours: CBOE-listed options on USO trade 9:30am-4:00pm ET on US
equity-market days. Outside those hours yfinance returns the most recent
session's quotes, frozen. We compute the freshness from the chain's max
`lastTradeDate` and SKIP the snapshot entirely (return []) when it's older
than `STALE_THRESHOLD_SEC` — otherwise we'd ingest 200+ duplicate rows
over a weekend and distort the §3/§4/§6 time-series sections.
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
# Skip the snapshot if the freshest strike trade is older than this. Tuned to
# catch "market closed for hours" without false-skipping illiquid strikes
# during RTH (USO option chain has trades on most strikes within ~30 min during
# RTH; 4 hours is the conservative gap that says the market is genuinely shut).
STALE_THRESHOLD_SEC = 4 * 3600


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
    """Sync yfinance fetch. Returns dict with keys: uso_spot, cl_spot, expiry,
    calls, puts, max_trade_ts. Returns {} on failure. `calls`/`puts` are pandas
    DataFrames."""
    import pandas as pd
    import yfinance as yf

    from perp_basis.collectors_vol._yf_shared import YF_LOCK

    out: dict = {}
    # Hold the shared yfinance lock for the whole fetch (USO history + CL=F
    # history + option chain + lastTradeDate scan). Released in `finally`
    # whether the body succeeds, returns early, or raises.
    YF_LOCK.acquire()
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

        # Freshness: the most recent trade across all strikes (calls + puts).
        # Used by collect() to skip the snapshot when the market is closed.
        max_ts = None
        for df in (chain.calls, chain.puts):
            if df is None or "lastTradeDate" not in df.columns:
                continue
            col = pd.to_datetime(df["lastTradeDate"], utc=True, errors="coerce")
            col_max = col.max()
            if pd.notna(col_max) and (max_ts is None or col_max > max_ts):
                max_ts = col_max
        if max_ts is not None:
            out["max_trade_ts"] = max_ts.to_pydatetime()
    except Exception as e:
        log.warning("cboe_uso: chain fetch failed: %s", e)
    finally:
        YF_LOCK.release()
    return out


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[VolSnapshot]:
    del client
    data = await asyncio.to_thread(_fetch_chain_sync)
    uso_spot = data.get("uso_spot")
    cl_spot = data.get("cl_spot")
    expiry = data.get("expiry")
    calls = data.get("calls")
    puts = data.get("puts")
    max_trade_ts = data.get("max_trade_ts")
    if not (uso_spot and expiry and calls is not None):
        return []

    # Skip the snapshot when the freshest strike trade is older than the
    # threshold — the market is closed and the chain is the previous session's
    # frozen state. Capturing it would just store duplicate rows for hours.
    age_sec = 0
    if max_trade_ts is not None:
        age_sec = max(0, int((ts - max_trade_ts).total_seconds()))
        if age_sec > STALE_THRESHOLD_SEC:
            log.info("cboe_uso: chain is %dh old (market closed) → skipping snapshot",
                     age_sec // 3600)
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
            # Sanity band on IV — Yahoo's impliedVolatility for very deep OTM
            # strikes is unreliable (option worth pennies, spread huge relative
            # to price → BS inversion explodes). [5%, 250%] is the same band we
            # use everywhere else.
            if iv < 0.05 or iv > 2.5:
                continue
            k_cl = k_uso * ratio
            bid = _f(row.get("bid"))
            ask = _f(row.get("ask"))
            last = _f(row.get("lastPrice"))
            vol = _f(row.get("volume"))
            oi = _f(row.get("openInterest"))
            # Liquidity gate: skip strikes with no real market (bid = 0 means
            # no resting buy orders → the IV Yahoo computed is from stale or
            # one-sided data; same goes for zero volume AND zero OI = dead
            # contract).
            if (bid is None or bid <= 0) and (vol is None or vol == 0) and (oi is None or oi == 0):
                continue
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
            data_age_sec=age_sec,
        )
    ]
