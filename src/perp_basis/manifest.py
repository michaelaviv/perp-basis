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
from perp_basis.schema_vol import VolSnapshot


def _json_default(o):
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, float) and math.isnan(o):
        return None
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _scan_dates_and_intraday(root: Path, daily_subdir: str, intraday_subdir: str
                             ) -> tuple[list[str], list[str], str]:
    """Shared scan: enumerate compacted daily-file dates AND today's loose
    intraday files. Used for both basis (daily/snapshots) and options
    (options_daily/options_snapshots) trees."""
    daily_root = root / daily_subdir
    dates: list[str] = []
    if daily_root.exists():
        for p in daily_root.rglob("*.parquet"):
            try:
                y, m = p.parent.parent.name, p.parent.name
                d = p.stem
                dates.append(f"{y}-{m}-{d}")
            except Exception:
                continue
    dates.sort()

    today = datetime.now(timezone.utc).date()
    today_dir = root / intraday_subdir / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
    intraday_paths: list[str] = []
    if today_dir.exists():
        for p in sorted(today_dir.glob("*.parquet")):
            intraday_paths.append(str(p.relative_to(root)).replace("\\", "/"))

    return dates, intraday_paths, today.isoformat()


def write_manifest(root: Path = DATA_DIR) -> Path:
    """Scan data/daily/**/*.parquet AND today's data/snapshots/<today>/*.parquet
    PLUS the parallel data/options_daily/ and data/options_snapshots/ trees,
    and write manifest.json so the dashboard can find both basis and vol
    parquets — compacted-daily plus today's still-loose intraday."""
    dates, intraday_paths, today_iso = _scan_dates_and_intraday(root, "daily", "snapshots")
    options_dates, options_intraday, _ = _scan_dates_and_intraday(
        root, "options_daily", "options_snapshots"
    )

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dates": dates,
        "count": len(dates),
        "intraday_date": today_iso,
        "intraday_files": intraday_paths,
        # Vol pipeline (separate file tree, single manifest by design):
        "options_dates": options_dates,
        "options_count": len(options_dates),
        "options_intraday_files": options_intraday,
    }
    out = root / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    return out


def write_latest(rows: list[PriceSnapshot], capture_ts: datetime, root: Path = DATA_DIR) -> Path:
    # `capture_ts` is the snapshot's wall-clock capture time. Don't derive from
    # rows[0].ts: CME rows now carry their bar's market time (~15 min stale).
    payload = {
        "ts": capture_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rows": [asdict(r) for r in rows],
    }
    out = root / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
    return out


def write_vol_latest(rows: list[VolSnapshot], capture_ts: datetime, root: Path = DATA_DIR) -> Path:
    """Most-recent VolSnapshot per venue, for the dashboard's `vol.html` Now panel."""
    payload = {
        "ts": capture_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rows": [asdict(r) for r in rows],
    }
    out = root / "vol_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
    return out
