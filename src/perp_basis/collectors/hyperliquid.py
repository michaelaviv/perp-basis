"""Hyperliquid HIP-3 commodity perpetual collector for GOLD and CL.

Hyperliquid hosts native perps and builder-deployed (HIP-3) perps on separate
"DEXs". GOLD and CL are HIP-3 markets, so we discover all DEXs via
`{"type":"perpDexs"}` and then query `metaAndAssetCtxs` on each one until
we find our two symbols. The native (non-HIP-3) exchange is also probed as a
fallback (no `dex` field in the request).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from perp_basis.config import symbols_for
from perp_basis.schema import PriceSnapshot

log = logging.getLogger(__name__)

VENUE = "hyperliquid"
INFO_URL = "https://api.hyperliquid.xyz/info"


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _post(client: httpx.AsyncClient, body: dict):
    r = await client.post(INFO_URL, json=body, timeout=10.0)
    r.raise_for_status()
    return r.json()


async def _meta_for_dex(client: httpx.AsyncClient, dex: str | None) -> dict[str, dict]:
    """Return {symbol: {mark, oracle, funding, openInterest, dayBaseVlm, dayNtlVlm}}."""
    body: dict = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    try:
        result = await _post(client, body)
    except Exception as e:
        log.warning("hyperliquid: metaAndAssetCtxs dex=%s failed: %s", dex, e)
        return {}

    if not (isinstance(result, list) and len(result) == 2):
        return {}
    meta, ctxs = result
    universe = (meta or {}).get("universe") or []
    by_sym: dict[str, dict] = {}
    for entry, ctx in zip(universe, ctxs or [], strict=False):
        name = entry.get("name") if isinstance(entry, dict) else None
        if name and isinstance(ctx, dict):
            by_sym[name] = ctx
    return by_sym


async def _l2_top(client: httpx.AsyncClient, coin: str) -> tuple[float | None, float | None]:
    try:
        result = await _post(client, {"type": "l2Book", "coin": coin})
    except Exception as e:
        log.warning("hyperliquid: l2Book %s failed: %s", coin, e)
        return None, None
    levels = (result or {}).get("levels") or []
    if len(levels) < 2:
        return None, None
    bid_levels, ask_levels = levels[0], levels[1]
    bid = _f(bid_levels[0]["px"]) if bid_levels else None
    ask = _f(ask_levels[0]["px"]) if ask_levels else None
    return bid, ask


async def _discover_dexes(client: httpx.AsyncClient) -> list[str]:
    try:
        result = await _post(client, {"type": "perpDexs"})
    except Exception as e:
        log.warning("hyperliquid: perpDexs failed: %s", e)
        return []
    out: list[str] = []
    if isinstance(result, list):
        for d in result:
            if isinstance(d, dict) and d.get("name"):
                out.append(d["name"])
            elif isinstance(d, str):
                out.append(d)
    return out


async def collect(client: httpx.AsyncClient, ts: datetime) -> list[PriceSnapshot]:
    syms = symbols_for(VENUE)  # {"gold": "GOLD", "wti": "CL"}
    needed = set(syms.values())

    # Probe the native exchange + every HIP-3 DEX in parallel.
    dexes = await _discover_dexes(client)
    meta_tasks = [_meta_for_dex(client, None)] + [_meta_for_dex(client, d) for d in dexes]
    meta_results = await asyncio.gather(*meta_tasks)

    # Merge: prefer the FIRST occurrence (native first, then HIP-3 dexes in order).
    merged: dict[str, dict] = {}
    for m in meta_results:
        for s, ctx in m.items():
            if s in needed and s not in merged:
                merged[s] = ctx

    # Pull bid/ask top-of-book for our two symbols in parallel.
    book_results = await asyncio.gather(*(_l2_top(client, s) for s in syms.values()))
    book_by_sym = dict(zip(syms.values(), book_results, strict=True))

    rows: list[PriceSnapshot] = []
    for product, sym in syms.items():
        ctx = merged.get(sym)
        if not ctx:
            log.warning("hyperliquid: %s/%s not found on any DEX", product, sym)
            continue
        bid, ask = book_by_sym.get(sym, (None, None))
        rows.append(
            PriceSnapshot(
                ts=ts,
                venue=VENUE,
                product=product,
                symbol=sym,
                mark_price=_f(ctx.get("markPx")) or _f(ctx.get("oraclePx")),
                last_price=_f(ctx.get("midPx")) or _f(ctx.get("markPx")),
                bid=bid,
                ask=ask,
                volume_24h=_f(ctx.get("dayBaseVlm")),
                quote_volume_24h=_f(ctx.get("dayNtlVlm")),
                funding_rate=_f(ctx.get("funding")),
                open_interest=_f(ctx.get("openInterest")),
                data_age_sec=0,
            )
        )
    return rows
