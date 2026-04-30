from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture
def ts() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
