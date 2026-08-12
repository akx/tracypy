"""Tests for the logging -> Tracy bridge.

No viewer is ever really connected here, so nothing is transmitted. The handler
skips its work entirely while disconnected, so the ``connected`` fixture fakes a
connection for the tests that check what reaches the extension; one test covers
the disconnected short-circuit itself.
"""

from __future__ import annotations

import logging

import pytest

import tracypy
from tracypy import LogHandler, Severity, severity_for_level


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make LogHandler.emit believe a viewer is attached."""
    monkeypatch.setattr(tracypy, "is_connected", lambda: True)


@pytest.fixture
def logger(connected: None) -> logging.Logger:
    log = logging.getLogger("tracypy.test")
    log.setLevel(1)  # let everything through, including custom sub-DEBUG levels
    log.propagate = False
    handler = LogHandler()
    log.addHandler(handler)
    yield log
    log.removeHandler(handler)


@pytest.mark.parametrize(
    ("levelno", "expected"),
    [
        (logging.CRITICAL, Severity.FATAL),
        (logging.CRITICAL + 10, Severity.FATAL),
        (logging.ERROR, Severity.ERROR),
        (logging.WARNING, Severity.WARNING),
        (logging.INFO, Severity.INFO),
        (logging.DEBUG, Severity.DEBUG),
        (5, Severity.TRACE),  # the conventional custom TRACE level
        (0, Severity.TRACE),
        (25, Severity.INFO),  # between INFO and WARNING -> rounds down
    ],
)
def test_severity_for_level(levelno: int, expected: Severity) -> None:
    assert severity_for_level(levelno) == expected


def test_handler_forwards_records(logger: logging.Logger, emitted: list[tuple[str, int, int]]) -> None:
    logger.warning("cache miss for %s", "key-1")
    assert emitted == [("cache miss for key-1", Severity.WARNING, 0)]


def test_handler_maps_each_level(logger: logging.Logger, emitted: list[tuple[str, int, int]]) -> None:
    logger.debug("d")
    logger.info("i")
    logger.warning("w")
    logger.error("e")
    logger.critical("c")
    assert [severity for _, severity, _color in emitted] == [
        Severity.DEBUG,
        Severity.INFO,
        Severity.WARNING,
        Severity.ERROR,
        Severity.FATAL,
    ]


def test_handler_uses_its_formatter(logger: logging.Logger, emitted: list[tuple[str, int, int]]) -> None:
    logger.handlers[0].setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.info("hello")
    assert emitted == [("[INFO] hello", Severity.INFO, 0)]


def test_handler_never_raises(logger: logging.Logger, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing emit must go through handleError, not escape into the caller.
    handled: list[logging.LogRecord] = []

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tracy exploded")

    monkeypatch.setattr(tracypy, "_message", boom)
    monkeypatch.setattr(LogHandler, "handleError", lambda self, record: handled.append(record))

    logger.info("this must not propagate")
    assert len(handled) == 1


def test_handler_level_filtering(logger: logging.Logger, emitted: list[tuple[str, int, int]]) -> None:
    logger.handlers[0].setLevel(logging.ERROR)
    logger.info("dropped")
    logger.error("kept")
    assert [text for text, *_ in emitted] == ["kept"]


def test_handler_skips_work_while_disconnected(emitted: list[tuple[str, int, int]]) -> None:
    # No `connected` fixture: emit must return before formatting, which is the
    # whole point of the check — a formatter that raises proves it never ran.
    class Exploding(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            raise AssertionError("formatted a record with nobody listening")

    log = logging.getLogger("tracypy.test.disconnected")
    log.propagate = False
    handler = LogHandler()
    handler.setFormatter(Exploding())
    log.addHandler(handler)
    try:
        log.error("dropped on the floor")
    finally:
        log.removeHandler(handler)
    assert emitted == []


def test_handler_against_real_tracy(logger: logging.Logger) -> None:
    # Without the emitted fixture: the real format-and-emit path, inert in C.
    logger.error("a real message")
    logger.exception("with a traceback", exc_info=ValueError("boom"))
