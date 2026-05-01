"""One-shot backfill: rewrite cme_yahoo rows so `ts` is the bar's market time.

Before this fix, every row in a snapshot was tagged with the same capture
wall-clock `ts`, even though CME prices via Yahoo are ~15 min delayed. The
collector recorded the lag in `data_age_sec`, so we can recover the true bar
time as `ts - data_age_sec`.

This script walks every parquet file under data/snapshots/ and data/daily/,
shifts each `cme_yahoo` row's `ts` back by its `data_age_sec`, and writes
the file back atomically. Perp rows are untouched.

Idempotency: writes a marker at data/.cme_ts_backfilled on success and refuses
to run if it already exists. Delete the marker to re-run.

Usage:
  python -m tools.backfill_cme_ts          # dry run, prints what it would do
  python -m tools.backfill_cme_ts --apply  # actually rewrite files
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from perp_basis.config import DATA_DIR
from perp_basis.schema import ARROW_SCHEMA

log = logging.getLogger(__name__)

MARKER = "data/.cme_ts_backfilled"


def _shift_cme_rows(table: pa.Table) -> tuple[pa.Table, int]:
    """Return (new_table, n_cme_rows_shifted).

    Skips files where CME rows are already at bar time (post-merge captures):
    detect by checking if any CME row's ts has non-zero seconds/microseconds.
    Yahoo 1m bars always land on :00 minute boundaries; capture timestamps
    almost never do (cron + network jitter).
    """
    venue = table.column("venue")
    cme_mask = pc.equal(venue, "cme_yahoo")
    n_cme = int(pc.sum(pc.cast(cme_mask, pa.int64())).as_py() or 0)
    if n_cme == 0:
        return table, 0

    ts = table.column("ts")
    # If every CME ts is already on a minute boundary, this file is already
    # corrected (post-merge capture) — skip.
    cme_ts_ns = pc.cast(pc.filter(ts, cme_mask), pa.int64()).to_pylist()
    if all(v % 60_000_000_000 == 0 for v in cme_ts_ns):
        return table, 0

    age_sec = table.column("data_age_sec")
    # ts is timestamp[ns, UTC]; for CME rows compute the corrected bar time as
    # truncate_to_minute(capture_ts - data_age_sec). Truncation matches Yahoo's
    # 1m bar timestamps (always on :00 boundaries) and lets the minute-boundary
    # guard above detect already-corrected rows on a re-run.
    age_ns = pc.multiply(pc.cast(age_sec, pa.int64()), pa.scalar(1_000_000_000, pa.int64()))
    offset_ns = pc.if_else(cme_mask, age_ns, pa.scalar(0, pa.int64()))
    ts_ns = pc.cast(ts, pa.int64())
    shifted_ns = pc.subtract(ts_ns, offset_ns)
    minute_ns = pa.scalar(60_000_000_000, pa.int64())
    truncated_ns = pc.multiply(pc.divide(shifted_ns, minute_ns), minute_ns)
    # Only truncate the cme rows; leave perp rows at their original ns precision.
    new_ts_ns = pc.if_else(cme_mask, truncated_ns, ts_ns)
    new_ts = pc.cast(new_ts_ns, pa.timestamp("ns", tz="UTC"))

    new_table = table.set_column(table.schema.get_field_index("ts"), "ts", new_ts)
    return new_table, n_cme


def _process_file(path: Path, *, apply: bool) -> tuple[int, int]:
    """Returns (cme_rows_shifted, total_rows). On apply, writes back atomically."""
    table = pq.read_table(path)
    new_table, n_cme = _shift_cme_rows(table)
    if n_cme == 0 or not apply:
        return n_cme, table.num_rows

    new_table = new_table.cast(ARROW_SCHEMA)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(new_table, tmp, compression="zstd")
    os.replace(tmp, path)
    return n_cme, table.num_rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually rewrite files (default is dry run)")
    ap.add_argument("--data-dir", default=str(DATA_DIR), help="override data dir (for testing)")
    args = ap.parse_args()

    root = Path(args.data_dir)
    marker = root / ".cme_ts_backfilled"

    if marker.exists() and args.apply:
        log.error("backfill: marker %s exists; refusing to re-run. Delete it to force.", marker)
        return 1

    files = sorted(
        list((root / "snapshots").rglob("*.parquet")) +
        list((root / "daily").rglob("*.parquet"))
    )
    if not files:
        log.warning("backfill: no parquet files found under %s", root)
        return 0

    log.info("backfill: found %d files (%s)", len(files), "APPLY" if args.apply else "DRY-RUN")
    total_cme, total_rows, files_touched = 0, 0, 0
    for f in files:
        try:
            n_cme, n_total = _process_file(f, apply=args.apply)
        except Exception as e:
            log.error("backfill: %s failed: %s", f, e)
            continue
        total_cme += n_cme
        total_rows += n_total
        if n_cme > 0:
            files_touched += 1
            log.info("  %s: %d cme / %d total", f.relative_to(root), n_cme, n_total)

    log.info(
        "backfill: %s — touched %d/%d files, shifted %d cme rows out of %d total",
        "APPLIED" if args.apply else "DRY-RUN",
        files_touched, len(files), total_cme, total_rows,
    )

    if args.apply:
        marker.write_text(datetime.now(timezone.utc).isoformat() + "\n")
        log.info("backfill: wrote marker %s", marker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
