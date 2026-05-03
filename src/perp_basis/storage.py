"""Parquet writers for snapshot files and the compacted daily files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from perp_basis.config import DATA_DIR
from perp_basis.schema import ARROW_SCHEMA, PriceSnapshot, snapshots_to_table
from perp_basis.schema_vol import ARROW_SCHEMA_VOL, VolSnapshot, vol_snapshots_to_table


def snapshot_path(ts: datetime, root: Path = DATA_DIR) -> Path:
    return (
        root
        / "snapshots"
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / f"{ts:%d}"
        / f"{ts:%H%M%S}.parquet"
    )


def daily_path(date_str: str, root: Path = DATA_DIR) -> Path:
    """date_str is 'YYYY-MM-DD'."""
    y, m, d = date_str.split("-")
    return root / "daily" / y / m / f"{d}.parquet"


def vol_snapshot_path(ts: datetime, root: Path = DATA_DIR) -> Path:
    return (
        root
        / "options_snapshots"
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / f"{ts:%d}"
        / f"{ts:%H%M%S}.parquet"
    )


def vol_daily_path(date_str: str, root: Path = DATA_DIR) -> Path:
    y, m, d = date_str.split("-")
    return root / "options_daily" / y / m / f"{d}.parquet"


def write_snapshot(rows: list[PriceSnapshot], ts: datetime, root: Path = DATA_DIR) -> Path:
    if not rows:
        raise ValueError("Refusing to write empty snapshot")
    table = snapshots_to_table(rows)
    path = snapshot_path(ts, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return path


def write_daily(table: pa.Table, date_str: str, root: Path = DATA_DIR) -> Path:
    path = daily_path(date_str, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cast in case the concatenated schema has any type drift
    table = table.cast(ARROW_SCHEMA)
    pq.write_table(table, path, compression="zstd", row_group_size=10_000)
    return path


def write_snapshot_vol(rows: list[VolSnapshot], ts: datetime, root: Path = DATA_DIR) -> Path:
    if not rows:
        raise ValueError("Refusing to write empty vol snapshot")
    table = vol_snapshots_to_table(rows)
    path = vol_snapshot_path(ts, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return path


def write_daily_vol(table: pa.Table, date_str: str, root: Path = DATA_DIR) -> Path:
    path = vol_daily_path(date_str, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = table.cast(ARROW_SCHEMA_VOL)
    pq.write_table(table, path, compression="zstd", row_group_size=10_000)
    return path
