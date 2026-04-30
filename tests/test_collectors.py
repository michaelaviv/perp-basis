"""Each collector must return exactly two PriceSnapshot rows: one gold + one wti.

We mock HTTP responses with respx (httpx) and yfinance via monkeypatch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from perp_basis.collectors import binance, hyperliquid, okx, yahoo_cme


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@respx.mock
async def test_binance_returns_gold_and_wti(ts):
    base = "https://fapi.binance.com"

    respx.get(f"{base}/fapi/v1/ticker/24hr").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "XAUUSDT", "lastPrice": "5000.10", "volume": "1234.5", "quoteVolume": "6172500.0"},
                {"symbol": "CLUSDT", "lastPrice": "75.42", "volume": "9876.5", "quoteVolume": "744897.0"},
            ],
        )
    )
    respx.get(f"{base}/fapi/v1/ticker/bookTicker").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "XAUUSDT", "bidPrice": "5000.05", "askPrice": "5000.15"},
                {"symbol": "CLUSDT", "bidPrice": "75.40", "askPrice": "75.44"},
            ],
        )
    )
    respx.get(f"{base}/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "XAUUSDT", "markPrice": "5000.12", "lastFundingRate": "0.0001"},
                {"symbol": "CLUSDT", "markPrice": "75.43", "lastFundingRate": "0.0002"},
                {"symbol": "BTCUSDT", "markPrice": "60000", "lastFundingRate": "0.0001"},
            ],
        )
    )
    respx.get(f"{base}/fapi/v1/openInterest").mock(
        return_value=httpx.Response(200, json={"openInterest": "12345"})
    )

    async with httpx.AsyncClient() as client:
        rows = await binance.collect(client, ts)

    products = sorted(r.product for r in rows)
    assert products == ["gold", "wti"], f"expected gold+wti, got {products}"
    by_p = {r.product: r for r in rows}
    assert by_p["gold"].symbol == "XAUUSDT"
    assert by_p["wti"].symbol == "CLUSDT"
    assert by_p["gold"].mark_price == pytest.approx(5000.12)
    assert by_p["gold"].bid == pytest.approx(5000.05)
    assert by_p["wti"].quote_volume_24h == pytest.approx(744897.0)
    assert by_p["gold"].funding_rate == pytest.approx(0.0001)
    assert by_p["wti"].open_interest == pytest.approx(12345)


# --------------------------------------------------------------------------- #
# OKX
# --------------------------------------------------------------------------- #

def _okx_ok(rows):
    return {"code": "0", "msg": "", "data": rows}


@pytest.mark.asyncio
@respx.mock
async def test_okx_returns_gold_and_wti(ts):
    base = "https://www.okx.com/api/v5"

    def ticker_resp(req):
        inst = req.url.params["instId"]
        last = "5000.5" if inst.startswith("XAU") else "75.5"
        return httpx.Response(
            200,
            json=_okx_ok([{"instId": inst, "last": last, "bidPx": "0.0", "askPx": "0.0",
                           "vol24h": "100", "volCcy24h": "1000"}]),
        )

    def mark_resp(req):
        inst = req.url.params["instId"]
        return httpx.Response(
            200,
            json=_okx_ok([{"instId": inst, "markPx": "5000.6" if inst.startswith("XAU") else "75.55"}]),
        )

    def fund_resp(req):
        inst = req.url.params["instId"]
        return httpx.Response(200, json=_okx_ok([{"instId": inst, "fundingRate": "0.00012"}]))

    def oi_resp(req):
        inst = req.url.params["instId"]
        return httpx.Response(200, json=_okx_ok([{"instId": inst, "oi": "5555"}]))

    respx.get(f"{base}/market/ticker").mock(side_effect=ticker_resp)
    respx.get(f"{base}/public/mark-price").mock(side_effect=mark_resp)
    respx.get(f"{base}/public/funding-rate").mock(side_effect=fund_resp)
    respx.get(f"{base}/public/open-interest").mock(side_effect=oi_resp)

    async with httpx.AsyncClient() as client:
        rows = await okx.collect(client, ts)

    products = sorted(r.product for r in rows)
    assert products == ["gold", "wti"]
    by_p = {r.product: r for r in rows}
    assert by_p["gold"].symbol == "XAU-USDT-SWAP"
    assert by_p["wti"].symbol == "CL-USDT-SWAP"
    assert by_p["gold"].mark_price == pytest.approx(5000.6)
    assert by_p["wti"].mark_price == pytest.approx(75.55)
    assert by_p["gold"].funding_rate == pytest.approx(0.00012)


# --------------------------------------------------------------------------- #
# Hyperliquid
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@respx.mock
async def test_hyperliquid_returns_gold_and_wti(ts):
    url = "https://api.hyperliquid.xyz/info"

    def info_resp(req):
        body = json.loads(req.content)
        t = body["type"]
        if t == "perpDexs":
            # Real HL response includes a leading null for the native exchange.
            return httpx.Response(200, json=[None, {"name": "xyz"}])
        if t == "metaAndAssetCtxs":
            dex = body.get("dex")
            if dex == "xyz":
                return httpx.Response(
                    200,
                    json=[
                        {"universe": [{"name": "xyz:GOLD"}, {"name": "xyz:CL"}]},
                        [
                            {"markPx": "5000.0", "midPx": "5000.1", "funding": "0.00005",
                             "openInterest": "111", "dayBaseVlm": "10", "dayNtlVlm": "50000"},
                            {"markPx": "75.5", "midPx": "75.55", "funding": "0.00010",
                             "openInterest": "222", "dayBaseVlm": "20", "dayNtlVlm": "1500"},
                        ],
                    ],
                )
            # native exchange — only crypto, none of our symbols
            return httpx.Response(
                200,
                json=[{"universe": [{"name": "BTC"}]}, [{"markPx": "60000"}]],
            )
        if t == "l2Book":
            coin = body["coin"]
            bid = "5000.0" if coin == "xyz:GOLD" else "75.5"
            ask = "5000.2" if coin == "xyz:GOLD" else "75.6"
            return httpx.Response(
                200,
                json={"levels": [[{"px": bid, "sz": "1"}], [{"px": ask, "sz": "1"}]]},
            )
        return httpx.Response(404)

    respx.post(url).mock(side_effect=info_resp)

    async with httpx.AsyncClient() as client:
        rows = await hyperliquid.collect(client, ts)

    products = sorted(r.product for r in rows)
    assert products == ["gold", "wti"], f"got {products}"
    by_p = {r.product: r for r in rows}
    assert by_p["gold"].symbol == "xyz:GOLD"
    assert by_p["wti"].symbol == "xyz:CL"
    assert by_p["gold"].mark_price == pytest.approx(5000.0)
    assert by_p["gold"].bid == pytest.approx(5000.0)
    assert by_p["gold"].ask == pytest.approx(5000.2)
    assert by_p["wti"].funding_rate == pytest.approx(0.00010)


# --------------------------------------------------------------------------- #
# Yahoo CME
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_yahoo_cme_returns_gold_and_wti(monkeypatch, ts):
    fake_ts_utc = datetime(2026, 4, 30, 11, 59, 0, tzinfo=timezone.utc)

    def fake_fetch_sync(symbols):
        out = {}
        for s in symbols:
            if s == "GC=F":
                out[s] = {"last": 4995.0, "volume": 12345, "bar_ts_utc": fake_ts_utc}
            elif s == "CL=F":
                out[s] = {"last": 75.30, "volume": 9999, "bar_ts_utc": fake_ts_utc}
        return out

    monkeypatch.setattr(yahoo_cme, "_fetch_sync", fake_fetch_sync)

    async with httpx.AsyncClient() as client:
        rows = await yahoo_cme.collect(client, ts)

    products = sorted(r.product for r in rows)
    assert products == ["gold", "wti"]
    by_p = {r.product: r for r in rows}
    assert by_p["gold"].symbol == "GC=F"
    assert by_p["wti"].symbol == "CL=F"
    assert by_p["gold"].mark_price == pytest.approx(4995.0)
    assert by_p["wti"].mark_price == pytest.approx(75.30)
    assert by_p["gold"].data_age_sec == 60
    assert by_p["gold"].bid is None  # CME via Yahoo: no book
