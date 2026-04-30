"""OKX perpetual swap collector for XAU-USDT-SWAP (gold) and CL-USDT-SWAP (wti)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from perp_basis.config import symbols_for
from perp_basis.schema import PriceSnapshot

log = logging.getLogger(__name__)

VENUE = "okx"
BASE = "https://www.okx.com/api/v5"


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _data(payload: dict) -> list[dict]:
    if payload.get("code") != "0":
        log.warning("okx: non-zero code: %s msg=%s", payload.get("code"), payload.get("msg"))
        return []
    return payload.get("data") or []


async def _fetch(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    try:
        r = await client.get(f"{BASE}{path}", params=params, timeout=10.0)
        r.raise_for_status()
        return _data(r.json())
    except Exception as e:
        log.warning("okx: %s %s failed: %s", path, params, e)
        return []


async def _per_symbol(client: httpx.AsyncClient, sym: str) -> dict:
    """Hit the four per-symbol endpoints in parallel and merge into one dict."""
    ticker, mark, fund, oi = await asyncio.gather(
        _fetch(client, "/market/ticker", {"instId": sym}),
        _fetch(client, "/public/mark-price", {"instType": "SWAP", "instId": sym}),
        _fetch(client, "/public/funding-rate", {"instId": sym}),
        _fetch(client, "/public/open-interest", {"instType": "SWAP", "instId": sym}),
    )
    return {
        "ticker": ticker[0] if ticker else {},
        "mark": mark[0] if mark else {},
        "fund": fund[0] if fund else {},
        "oi": oi[0] if oi else {},
    }


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[PriceSnapshot]:
    syms = symbols_for(VENUE)  # {"gold": "XAU-USDT-SWAP", "wti": "CL-USDT-SWAP"}
    results = await asyncio.gather(*(_per_symbol(client, s) for s in syms.values()))

    rows: list[PriceSnapshot] = []
    for (product, sym), bundle in zip(syms.items(), results, strict=True):
        t = bundle["ticker"]
        m = bundle["mark"]
        f = bundle["fund"]
        oi = bundle["oi"]
        if not t and not m:
            log.warning("okx: no data for %s/%s", product, sym)
            continue
        rows.append(
            PriceSnapshot(
                ts=ts,
                venue=VENUE,
                product=product,
                symbol=sym,
                mark_price=_f(m.get("markPx")) or _f(t.get("last")),
                last_price=_f(t.get("last")),
                bid=_f(t.get("bidPx")),
                ask=_f(t.get("askPx")),
                volume_24h=_f(t.get("vol24h")),
                quote_volume_24h=_f(t.get("volCcy24h")),
                funding_rate=_f(f.get("fundingRate")),
                open_interest=_f(oi.get("oi")),
                data_age_sec=0,
            )
        )
    return rows
