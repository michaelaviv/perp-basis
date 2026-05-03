"""Shared yfinance lock for the vol collectors.

The vol orchestrator fans out collectors via `asyncio.gather`, and each
collector wraps its yfinance call in `asyncio.to_thread`. yfinance keeps a
shared sqlite timezone cache under `~/.cache/py-yfinance/` and the threads
race for the write lock — one collector gets `sqlite3.OperationalError:
database is locked`, returns no underlying price, and silently drops its
entire snapshot.

This module exposes a single process-wide `threading.Lock` that all
yfinance-using collectors should acquire. Per-call cost is ~1s
(serialized 4 venues × ~1s yfinance call); not worth the architectural
churn of plumbing a single shared underlying fetch through the
orchestrator just to save that.
"""

from __future__ import annotations

import threading

YF_LOCK = threading.Lock()
