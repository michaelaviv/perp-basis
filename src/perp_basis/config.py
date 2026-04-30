"""Symbol registry: which native instrument to use for each (venue, product) pair."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

# {(venue, product): native_symbol}
SYMBOLS: dict[tuple[str, str], str] = {
    ("binance", "gold"): "XAUUSDT",
    ("binance", "wti"): "CLUSDT",
    ("okx", "gold"): "XAU-USDT-SWAP",
    ("okx", "wti"): "CL-USDT-SWAP",
    ("hyperliquid", "gold"): "xyz:GOLD",
    ("hyperliquid", "wti"): "xyz:CL",
    ("cme_yahoo", "gold"): "GC=F",
    ("cme_yahoo", "wti"): "CL=F",
}

VENUES = ("binance", "okx", "hyperliquid", "cme_yahoo")
PRODUCTS = ("gold", "wti")


def symbols_for(venue: str) -> dict[str, str]:
    """Return {product: native_symbol} for one venue. Used by collectors."""
    return {p: SYMBOLS[(venue, p)] for p in PRODUCTS if (venue, p) in SYMBOLS}
