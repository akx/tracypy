from __future__ import annotations

import pytest

import tracypy
from tracypy._core import _zone_depth


@pytest.fixture(autouse=True)
def _clean_profiler_state():
    """Assert the zone stack balances, and never leak an enabled profiler."""
    assert _zone_depth() == 0, "a previous test left zones open"
    try:
        yield
        # Check before disabling: disable() unwinds the stack itself, which
        # would mask exactly the leak this is here to catch.
        assert _zone_depth() == 0, "test left zones open"
    finally:
        tracypy.disable()
