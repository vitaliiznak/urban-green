"""Shared test setup: keep the upstream disk cache out of the project tree."""
from __future__ import annotations

import pytest

from server.adapters import osm


@pytest.fixture(autouse=True)
def _isolated_disk_cache(tmp_path, monkeypatch):
    """Every test gets an empty on-disk cache so TTL/refetch assertions hold."""
    monkeypatch.setattr(osm, "DISK_CACHE_DIR", str(tmp_path / "upstream"))
    osm.cache_clear()
    yield
    osm.cache_clear()
