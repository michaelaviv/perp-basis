"""Round-trip snapshot rows through Parquet and confirm the schema is preserved."""

from __future__ import annotations

from datetime import datetime, timezone

import pyarrow.parquet as pq

from perp_basis.schema import ARROW_SCHEMA, PriceSnapshot
from perp_basis.storage import write_snapshot


def test_snapshot_roundtrip(tmp_path):
    ts = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        PriceSnapshot(ts=ts, venue="binance", product="gold", symbol="XAUUSDT",
                      mark_price=5000.1, last_price=5000.2, bid=5000.0, ask=5000.3,
                      volume_24h=1.0, quote_volume_24h=5000.0, funding_rate=0.0001,
                      open_interest=10.0),
        PriceSnapshot(ts=ts, venue="binance", product="wti", symbol="CLUSDT",
                      mark_price=75.4, last_price=75.5, bid=75.3, ask=75.5,
                      volume_24h=2.0, quote_volume_24h=150.0, funding_rate=0.0002,
                      open_interest=20.0),
    ]
    path = write_snapshot(rows, ts, root=tmp_path)
    table = pq.read_table(path)

    assert table.schema.equals(ARROW_SCHEMA), "schema drifted"
    assert table.num_rows == 2
    products = sorted(table.column("product").to_pylist())
    assert products == ["gold", "wti"]
