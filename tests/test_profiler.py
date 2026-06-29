"""Behavioural tests for the profiler wiring and frame marks.

Like the smoke tests, these run without a Tracy viewer: zone work is inert, so
we are checking that the C callbacks and Python wrappers keep their invariants
(balanced enable/disable, exceptions propagating, no crash) under the event
shapes that are easy to get wrong — exceptions, generators, and threads.
"""

from __future__ import annotations

import sys
import threading

import pytest

import tracypy


@pytest.fixture(autouse=True)
def _ensure_disabled():
    yield
    tracypy.disable()


def _free_tool_id() -> int:
    """A sys.monitoring tool id that nothing (incl. tracypy) currently holds."""
    for tid in range(6):
        if tid != tracypy.PROFILER_ID and sys.monitoring.get_tool(tid) is None:
            return tid
    pytest.skip("no free sys.monitoring tool id available")


def test_enable_rejects_tool_id_already_in_use() -> None:
    tid = _free_tool_id()
    sys.monitoring.use_tool_id(tid, "someone-else")
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            tracypy.enable(tool_id=tid)
        # The other tool's claim on the id is untouched.
        assert sys.monitoring.get_tool(tid) == "someone-else"
    finally:
        sys.monitoring.free_tool_id(tid)


def test_failed_registration_frees_the_tool_id(monkeypatch) -> None:
    # Force the final set_events() to blow up, exercising enable()'s cleanup path.
    def boom(*args, **kwargs):
        raise RuntimeError("set_events failed")

    monkeypatch.setattr(sys.monitoring, "set_events", boom)
    with pytest.raises(RuntimeError, match="set_events failed"):
        tracypy.enable()

    assert not tracypy.is_enabled()
    # The half-registered tool id must have been released, not left dangling.
    assert sys.monitoring.get_tool(tracypy.PROFILER_ID) is None

    # ...so a normal enable still works afterwards.
    monkeypatch.undo()
    tracypy.enable()
    assert tracypy.is_enabled()
    tracypy.disable()


def test_exception_unwinding_through_profiled_frames() -> None:
    def deep(n: int) -> int:
        if n == 0:
            raise ValueError("bottom")
        return deep(n - 1)

    with tracypy.profile():
        with pytest.raises(ValueError, match="bottom"):
            deep(6)  # PY_UNWIND through several frames
    # The stack must be balanced again: re-enabling would fail if state were stuck.
    assert not tracypy.is_enabled()


def test_generator_yield_resume_and_throw() -> None:
    def gen():
        try:
            yield 1
            yield 2
        except KeyError:
            yield 99

    with tracypy.profile():
        g = gen()
        assert next(g) == 1  # PY_START then PY_YIELD
        assert next(g) == 2  # PY_RESUME then PY_YIELD
        g2 = gen()
        next(g2)
        assert g2.throw(KeyError()) == 99  # PY_THROW into a suspended frame
    assert not tracypy.is_enabled()


@pytest.mark.parametrize(
    "call",
    [
        lambda: tracypy.frame_mark(123),
        lambda: tracypy.frame_mark_start(123),
        lambda: tracypy.frame_mark_start(None),
        lambda: tracypy.frame_mark_end(123),
        lambda: tracypy.frame_mark_end(None),
    ],
)
def test_frame_marks_reject_bad_names(call) -> None:
    with pytest.raises(TypeError):
        call()


@pytest.fixture
def _record_frame_marks(monkeypatch):
    # frame.__enter__/__exit__ call the module-global frame_mark_start/end, so
    # patching them lets us observe the calls without a connected viewer.
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(tracypy, "frame_mark_start", lambda n: calls.append(("start", n)))
    monkeypatch.setattr(tracypy, "frame_mark_end", lambda n: calls.append(("end", n)))
    return calls


def test_frame_cm_pairs_start_and_end(_record_frame_marks) -> None:
    with tracypy.frame("request"):
        _record_frame_marks.append(("body", "request"))
    assert _record_frame_marks == [("start", "request"), ("body", "request"), ("end", "request")]


def test_frame_cm_closes_on_exception(_record_frame_marks) -> None:
    # The documented guarantee: the frame is closed even if the block raises,
    # and the exception is not suppressed.
    with pytest.raises(RuntimeError, match="boom"):
        with tracypy.frame("request"):
            raise RuntimeError("boom")
    assert _record_frame_marks == [("start", "request"), ("end", "request")]


def test_flush_on_exit_disables_before_shutdown(monkeypatch) -> None:
    # The atexit hook must stop monitoring before finalizing Tracy, so no
    # in-flight callback can emit into a profiler being torn down. Patch both so
    # the ordering can be asserted without actually shutting Tracy down (which
    # would break the rest of the suite).
    calls: list[str] = []
    monkeypatch.setattr(tracypy, "disable", lambda: calls.append("disable"))
    monkeypatch.setattr(tracypy, "_shutdown", lambda: calls.append("shutdown"))
    tracypy._flush_on_exit()
    assert calls == ["disable", "shutdown"]


def test_profiling_across_threads() -> None:
    # Each worker thread builds its own per-thread zone stack in the C extension.
    errors: list[BaseException] = []

    def work() -> None:
        try:
            total = sum(i * i for i in range(1000))
            assert total > 0
        except BaseException as exc:  # noqa: BLE001 - surface worker failures to the test
            errors.append(exc)

    with tracypy.profile():
        threads = [threading.Thread(target=work, name=f"w{i}") for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors
    assert not tracypy.is_enabled()
