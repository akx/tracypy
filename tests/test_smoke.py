"""Smoke tests: drive the whole public API once, without a Tracy viewer.

Tracy is built in on-demand mode, so every entry point exercised here is inert
(nothing is actually transmitted) until a viewer connects. That is exactly what
makes these safe in CI: we are checking that the compiled ``_core`` extension
loads and that the Python wrappers drive ``sys.monitoring`` and the frame-mark
calls without raising or corrupting state.
"""

from __future__ import annotations

import pytest

import tracypy


@pytest.fixture(autouse=True)
def _ensure_disabled():
    # Never let a failing test leave the profiler enabled for the next one.
    yield
    tracypy.disable()


def _workload() -> int:
    """A little nested call + generator so entry/exit callbacks actually fire."""

    def fib(n: int) -> int:
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    return sum(fib(i) for i in range(8))


def test_profile_context_manager() -> None:
    assert not tracypy.is_enabled()
    with tracypy.profile():
        assert tracypy.is_enabled()
        _workload()
    assert not tracypy.is_enabled()


def test_double_enable_raises_and_keeps_state() -> None:
    with tracypy.profile():
        with pytest.raises(RuntimeError):
            tracypy.enable()
        # The failed second enable must not have torn down the active profiler.
        assert tracypy.is_enabled()
    assert not tracypy.is_enabled()


def test_frame_marks_do_not_raise() -> None:
    tracypy.frame_mark()
    tracypy.frame_mark("render")
    tracypy.frame_mark_start("request")
    tracypy.frame_mark_end("request")
    with tracypy.frame("request"):
        _workload()


def test_is_connected_is_false_without_viewer() -> None:
    # On-demand: no viewer connects during a normal test run, so this is False.
    assert tracypy.is_connected() is False
