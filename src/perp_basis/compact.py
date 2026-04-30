"""Daily compaction: roll yesterday's per-snapshot Parquet files into one daily file
and rebuild the manifest."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from perp_basis.config import DATA_DIR
from perp_basis.manifest import write_manifest
from perp_basis.storage import write_daily

log = logging.getLogger(__name__)


def _resolve_date(spec: str) -> date:
    if spec == "today":
        return datetime.now(timezone.utc).date()
    if spec == "yesterday":
        return (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return date.fromisoformat(spec)


def compact_day(target: date, root: Path = DATA_DIR, *, delete_source: bool = True) -> Path | None:
    src_dir = root / "snapshots" / f"{target:%Y}" / f"{target:%m}" / f"{target:%d}"
    if not src_dir.exists():
        log.warning("compact: no snapshots for %s at %s", target, src_dir)
        return None
    files = sorted(src_dir.glob("*.parquet"))
    if not files:
        log.warning("compact: no parquet files in %s", src_dir)
        return None

    tables = [pq.read_table(f) for f in files]
    table = pa.concat_tables(tables, promote_options="default")
    table = table.sort_by([("ts", "ascending"), ("venue", "ascending"), ("product", "ascending")])

    out = write_daily(table, target.isoformat(), root=root)
    log.info("compact: %s → %s (%d rows from %d files)", target, out, table.num_rows, len(files))

    if delete_source:
        shutil.rmtree(src_dir)
        log.info("compact: removed source dir %s", src_dir)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="yesterday", help="YYYY-MM-DD | today | yesterday")
    ap.add_argument("--keep-snapshots", action="store_true", help="don't delete the source per-snapshot files")
    args = ap.parse_args()

    target = _resolve_date(args.date)
    compact_day(target, delete_source=not args.keep_snapshots)
    write_manifest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
