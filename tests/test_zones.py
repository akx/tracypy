"""Tests for explicit zones and zone annotations.

No viewer is connected, so nothing is transmitted — but the per-thread zone
stack is maintained either way, and keeping it balanced is what stops tracypy
from feeding Tracy mis-nested zones. ``_zone_depth()`` exposes that stack so the
invariant is actually checked rather than assumed.
"""

from __future__ import annotations

import threading
from sys import monitoring as mon

import pytest

import tracypy
from tracypy._core import _zone_depth


def test_annotations_without_a_zone_are_inert() -> None:
    # Called at module level there is no zone to annotate; must not raise.
    tracypy.zone_text("nothing to annotate")
    tracypy.zone_name("nor this")
    tracypy.zone_color(0xFF0000)
    tracypy.zone_value(42)


def test_zone_opens_and_closes() -> None:
    with tracypy.zone("work"):
        assert _zone_depth() == 1


def test_zones_nest() -> None:
    with tracypy.zone("outer"):
        assert _zone_depth() == 1
        with tracypy.zone("inner"):
            assert _zone_depth() == 2
        assert _zone_depth() == 1


def test_zone_closes_on_exception() -> None:
    # Also pins that the zone never swallows the exception.
    with pytest.raises(ValueError, match="boom"), tracypy.zone("boom"):
        raise ValueError("boom")


def test_zone_annotations_at_construction() -> None:
    with tracypy.zone("annotated", text="user=42", value=1234, color=0x00FF00):
        assert _zone_depth() == 1


def test_zone_annotations_inside_block() -> None:
    with tracypy.zone("z"):
        tracypy.zone_text("added later")
        tracypy.zone_value(7)
        tracypy.zone_color(0x0088FF)
        tracypy.zone_name("renamed")


def test_zone_under_profiling_stays_balanced() -> None:
    def work() -> None:
        with tracypy.zone("inner", text="x"):
            pass

    with tracypy.profile():
        for _ in range(100):
            work()


def test_zone_balanced_across_generator_suspend() -> None:
    # Documented caveat: a zone spanning a yield closes at the suspend point and
    # the timings get cross-attributed. What must NOT happen is an unbalanced
    # stack, which would hand Tracy mis-nested zones.
    def gen():
        with tracypy.zone("spans a yield"):
            yield 1
            yield 2

    with tracypy.profile():
        assert list(gen()) == [1, 2]


def test_zone_balanced_on_abandoned_generator() -> None:
    # A generator that is never exhausted still runs the with-block's __exit__
    # when it is closed/collected.
    def gen():
        with tracypy.zone("abandoned"):
            yield 1
            yield 2

    with tracypy.profile():
        g = gen()
        next(g)
        g.close()


def test_zone_stack_is_per_thread() -> None:
    depths: list[int] = []

    def worker() -> None:
        with tracypy.zone("worker zone"):
            depths.append(_zone_depth())
        depths.append(_zone_depth())

    with tracypy.zone("main zone"):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # The worker's zone lives on its own stack, not on ours.
        assert _zone_depth() == 1
    assert depths == [1, 0]


def test_zone_name_must_be_str() -> None:
    with pytest.raises(TypeError, match="must be a str"), tracypy.zone(b"bytes"):
        pass


def test_zone_bad_color() -> None:
    with pytest.raises(ValueError, match="0xRRGGBB"), tracypy.zone("z", color=-1):
        pass


@pytest.mark.parametrize("text", ["x" * 200_000, "€" * 100_000], ids=["x", "eur"])
def test_oversized_zone_text(text: str) -> None:
    with tracypy.zone("z"):
        tracypy.zone_text(text)


def test_zone_creates_no_python_frame_of_its_own(free_tool_id: int) -> None:
    """The context manager must not be a Python function.

    sys.monitoring reports PY_START/PY_RETURN for Python functions only. A
    context manager written in Python gets a frame zone around __enter__, and
    that frame *returns before the body runs* — so its PY_RETURN pops the zone
    __enter__ just opened, collapsing the explicit zone to nothing and leaving
    the body covered by tracypy's own __enter__ frame instead. Implementing the
    type in C is what prevents that, so assert the frames really are absent.
    """
    depths: list[int] = []
    seen: list[str] = []

    def work() -> None:
        with tracypy.zone("z", text="t", value=1):
            depths.append(_zone_depth())

    tool = free_tool_id
    mon.use_tool_id(tool, "observer")
    try:
        mon.register_callback(tool, mon.events.PY_START, lambda code, off: seen.append(code.co_qualname))
        with tracypy.profile():
            mon.set_events(tool, mon.events.PY_START)
            work()
            mon.set_events(tool, 0)
    finally:
        mon.free_tool_id(tool)

    # e.g. "zone.__enter__" / "zone.__exit__" / "zone.__init__"
    assert not [q for q in seen if q.startswith("zone.")], f"zone created Python frames: {seen}"
    # work() was entered under profiling, so it has a frame zone; the explicit
    # zone sits on top of it and stays innermost for the body.
    assert depths == [2]


def test_zone_annotations_are_validated_at_construction() -> None:
    # __exit__ is not called when __enter__ raises, so a zone opened before a
    # bad annotation would leak. Everything is checked in __init__ instead,
    # which also reports the mistake at the line that made it.
    with pytest.raises(TypeError, match="zone text must be a str"):
        tracypy.zone("z", text=b"not a str")
    with pytest.raises(OverflowError):
        tracypy.zone("z", value=-1)
    with pytest.raises(ValueError, match="0xRRGGBB"):
        tracypy.zone("z", color=-1)


def test_zone_is_reusable() -> None:
    z = tracypy.zone("reused")
    with z:
        assert _zone_depth() == 1
    with z:
        assert _zone_depth() == 1


def test_zone_exposes_its_arguments() -> None:
    z = tracypy.zone("n", text="t", value=5, color=0x112233)
    assert (z.name, z.text, z.value, z.color) == ("n", "t", 5, 0x112233)
    bare = tracypy.zone("n")
    assert (bare.text, bare.value, bare.color) == (None, None, 0)
