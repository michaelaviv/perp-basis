"""Maintain `data/manifest.json` (list of available daily files) and `data/latest.json`
(last snapshot, for the dashboard's at-a-glance panel)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from perp_basis.config import DATA_DIR
from perp_basis.schema import PriceSnapshot


def _json_default(o):
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, float) and math.isnan(o):
        return None
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def write_manifest(root: Path = DATA_DIR) -> Path:
    """Scan data/daily/**/*.parquet AND today's data/snapshots/<today>/*.parquet,
    write manifest.json so the dashboard can find both compacted daily files
    AND the still-loose snapshots from the current day (uncompacted until
    00:05 UTC the next morning)."""
    daily_root = root / "daily"
    dates: list[str] = []
    if daily_root.exists():
        for p in daily_root.rglob("*.parquet"):
            try:
                # data/daily/YYYY/MM/DD.parquet → date "YYYY-MM-DD"
                y, m = p.parent.parent.name, p.parent.name
                d = p.stem
                dates.append(f"{y}-{m}-{d}")
            except Exception:
                continue
    dates.sort()

    # Intraday: list today's loose snapshot files so the dashboard can fetch
    # them in addition to the daily files. These exist between snapshots and
    # the next compact run.
    today = datetime.now(timezone.utc).date()
    today_dir = root / "snapshots" / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
    intraday_paths: list[str] = []
    if today_dir.exists():
        for p in sorted(today_dir.glob("*.parquet")):
            # Path relative to root (e.g. "snapshots/2026/05/01/120304.parquet")
            intraday_paths.append(str(p.relative_to(root)).replace("\\", "/"))

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dates": dates,
        "count": len(dates),
        "intraday_date": today.isoformat(),
        "intraday_files": intraday_paths,
    }
    out = root / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    return out


def write_latest(rows: list[PriceSnapshot], root: Path = DATA_DIR) -> Path:
    payload = {
        "ts": rows[0].ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if rows
        else None,
        "rows": [asdict(r) for r in rows],
    }
    out = root / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
    return out
