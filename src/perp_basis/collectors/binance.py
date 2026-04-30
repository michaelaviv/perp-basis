"""Binance USDT-M perpetual futures collector for XAUUSDT (gold) and CLUSDT (wti)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx

from perp_basis.config import symbols_for
from perp_basis.schema import PriceSnapshot

log = logging.getLogger(__name__)

VENUE = "binance"
BASE = "https://fapi.binance.com"


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[PriceSnapshot]:
    syms = symbols_for(VENUE)  # {"gold": "XAUUSDT", "wti": "CLUSDT"}
    sym_list = list(syms.values())
    sym_param = json.dumps(sym_list, separators=(",", ":"))

    async def _get(path: str, params: dict | None = None):
        r = await client.get(f"{BASE}{path}", params=params, timeout=10.0)
        r.raise_for_status()
        return r.json()

    # Run in parallel: ticker24h (multi), bookTicker (multi), premiumIndex (all),
    # openInterest (per symbol).
    ticker_task = _get("/fapi/v1/ticker/24hr", {"symbols": sym_param})
    book_task = _get("/fapi/v1/ticker/bookTicker", {"symbols": sym_param})
    prem_task = _get("/fapi/v1/premiumIndex")
    oi_tasks = {
        product: _get("/fapi/v1/openInterest", {"symbol": s}) for product, s in syms.items()
    }

    try:
        ticker, book, prem, *oi_results = await asyncio.gather(
            ticker_task, book_task, prem_task, *oi_tasks.values()
        )
    except Exception as e:
        log.warning("binance: collect failed: %s", e)
        return []

    by_sym_ticker = {row["symbol"]: row for row in ticker}
    by_sym_book = {row["symbol"]: row for row in book}
    by_sym_prem = {row["symbol"]: row for row in prem if row["symbol"] in sym_list}
    by_product_oi = dict(zip(oi_tasks.keys(), oi_results, strict=True))

    rows: list[PriceSnapshot] = []
    for product, sym in syms.items():
        t = by_sym_ticker.get(sym, {})
        b = by_sym_book.get(sym, {})
        p = by_sym_prem.get(sym, {})
        oi = by_product_oi.get(product, {})
        if not t and not b and not p:
            log.warning("binance: no data for %s/%s", product, sym)
            continue
        rows.append(
            PriceSnapshot(
                ts=ts,
                venue=VENUE,
                product=product,
                symbol=sym,
                mark_price=_f(p.get("markPrice")),
                last_price=_f(t.get("lastPrice")),
                bid=_f(b.get("bidPrice")),
                ask=_f(b.get("askPrice")),
                volume_24h=_f(t.get("volume")),
                quote_volume_24h=_f(t.get("quoteVolume")),
                funding_rate=_f(p.get("lastFundingRate")),
                open_interest=_f(oi.get("openInterest")),
                data_age_sec=0,
            )
        )
    return rows
