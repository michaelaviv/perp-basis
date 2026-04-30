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
    """Scan data/daily/**/*.parquet, write manifest.json with sorted list of dates."""
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
    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dates": dates,
        "count": len(dates),
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
