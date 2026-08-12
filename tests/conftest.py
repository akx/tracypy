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


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Capture the (text, severity, color) triples reaching the extension.

    Patching the C entry point rather than ``message()`` keeps the severity
    coercion in the code under test.
    """
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(tracypy, "_message", lambda text, severity, color: calls.append((text, severity, color)))
    return calls
