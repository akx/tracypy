"""Tests for tracypy.message().

Like the smoke tests, these run without a viewer, so every emit is inert. What
they pin down is the argument contract: which severity spellings are accepted,
and which bad inputs raise rather than reaching Tracy.
"""

from __future__ import annotations

import pytest

import tracypy
from tracypy import Severity


def test_severity_values_match_tracy() -> None:
    # These are wire values in Tracy's MessageSeverity enum; changing them
    # silently would mislabel every message in the viewer.
    assert [s.value for s in Severity] == [0, 1, 2, 3, 4, 5]
    assert Severity.TRACE == 0
    assert Severity.FATAL == 5


def test_unknown_severity_name() -> None:
    with pytest.raises(ValueError, match="unknown severity 'nope'"):
        tracypy.message("x", "nope")


@pytest.mark.parametrize("severity", ["info", Severity.FATAL, 0, 5])
def test_message_accepts_every_severity_form(severity: object) -> None:
    tracypy.message("hello", severity)


def test_message_defaults_to_info() -> None:
    tracypy.message("no severity given")


def test_message_with_color() -> None:
    tracypy.message("red", "error", 0xFF0000)
    tracypy.message("max", "info", 0xFFFFFF)


@pytest.mark.parametrize("color", [-1, 0x1000000, 1 << 200])
def test_bad_color(color: int) -> None:
    with pytest.raises(ValueError, match="0xRRGGBB"):
        tracypy.message("x", "info", color)


@pytest.mark.parametrize("severity", [-1, 6, 99])
def test_out_of_range_severity_int(severity: int) -> None:
    with pytest.raises(ValueError, match="severity must be in range 0-5"):
        tracypy.message("x", severity)


def test_non_str_text() -> None:
    with pytest.raises(TypeError, match="must be a str"):
        tracypy.message(b"bytes")


def test_message_works_while_profiling() -> None:
    with tracypy.profile():
        tracypy.message("inside a profiled block", "debug")


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("trace", Severity.TRACE),
        ("debug", Severity.DEBUG),
        ("info", Severity.INFO),
        ("warning", Severity.WARNING),
        ("error", Severity.ERROR),
        ("fatal", Severity.FATAL),
        ("critical", Severity.FATAL),
    ],
)
def test_alias_severity(alias: str, expected: Severity, emitted: list[tuple[str, int, int]]) -> None:
    getattr(tracypy, alias)("hello")
    assert emitted == [("hello", expected, 0)]


def test_alias_takes_color(emitted: list[tuple[str, int, int]]) -> None:
    tracypy.warning("careful", 0xFF8800)
    assert emitted == [("careful", Severity.WARNING, 0xFF8800)]


def test_aliases_are_exported() -> None:
    for name in ("trace", "debug", "info", "warning", "error", "fatal", "critical"):
        assert name in tracypy.__all__, name


def test_aliases_reach_real_tracy() -> None:
    # Without the monkeypatch: the real (disconnected, inert) path.
    tracypy.warning("a real inert warning")
    tracypy.critical("a real inert critical", 0xFF0000)
