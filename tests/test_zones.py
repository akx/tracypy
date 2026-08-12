"""Tests for explicit zones and zone annotations.

No viewer is connected, so nothing is transmitted — but the per-thread zone
stack is maintained either way, and keeping it balanced is what stops tracypy
from feeding Tracy mis-nested zones. ``_zone_depth()`` exposes that stack so the
invariant is actually checked rather than assumed.
"""

from __future__ import annotations

import threading

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
