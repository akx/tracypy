"""Lightweight Tracy profiler integration for Python via ``sys.monitoring``.

Enable profiling, run your code with a Tracy viewer connected, and every Python
function call shows up as a zone::

    import tracypy

    with tracypy.profile():
        my_workload()

Or without editing your code::

    python -m tracypy my_script.py

Tracy is built in on-demand mode, so enabling profiling is essentially free
until a Tracy viewer actually connects. On a clean interpreter exit tracypy
flushes any buffered trace data to a connected viewer automatically (via an
``atexit`` hook), so even short scripts profiled with a viewer attached don't
lose their tail.
"""

from __future__ import annotations

import atexit as _atexit
import logging as _logging
from enum import IntEnum
from sys import monitoring as _mon
from typing import Self

from tracypy._core import (
    _on_entry,
    _on_exit,
    _shutdown,
    _zone_unwind,
    frame_mark,
    frame_mark_end,
    frame_mark_start,
    is_connected,
    zone,
    zone_color,
    zone_name,
    zone_text,
    zone_value,
)
from tracypy._core import message as _message

__all__ = [
    "enable",
    "disable",
    "is_enabled",
    "is_connected",
    "profile",
    "PROFILER_ID",
    "frame_mark",
    "frame_mark_start",
    "frame_mark_end",
    "frame",
    "message",
    "trace",
    "debug",
    "info",
    "warning",
    "error",
    "fatal",
    "critical",
    "Severity",
    "LogHandler",
    "severity_for_level",
    "zone",
    "zone_text",
    "zone_name",
    "zone_color",
    "zone_value",
]

PROFILER_ID = _mon.PROFILER_ID

_events = _mon.events
# Entry events begin a zone; exit events end one. Every Python frame is bounded
# by exactly one of each, including generators/coroutines (suspend = YIELD,
# resume = RESUME, throw-into = THROW).
_ENTRY_EVENTS = (_events.PY_START, _events.PY_RESUME, _events.PY_THROW)
_EXIT_EVENTS = (_events.PY_RETURN, _events.PY_YIELD, _events.PY_UNWIND)
_ALL_EVENTS = (
    _events.PY_START | _events.PY_RESUME | _events.PY_THROW | _events.PY_RETURN | _events.PY_YIELD | _events.PY_UNWIND
)

_active_tool_id: int | None = None


def is_enabled() -> bool:
    """Return whether tracypy is currently profiling."""
    return _active_tool_id is not None


def enable(tool_id: int = PROFILER_ID, name: str = "tracypy") -> None:
    """Start profiling: register callbacks with ``sys.monitoring`` and turn events on.

    ``tool_id`` must be a free ``sys.monitoring`` tool id (0-5); it defaults to
    ``sys.monitoring.PROFILER_ID`` (2). Raises ``RuntimeError`` if tracypy is
    already enabled or the chosen id is taken by another tool.
    """
    global _active_tool_id
    if _active_tool_id is not None:
        raise RuntimeError("tracypy is already enabled")

    in_use = _mon.get_tool(tool_id)
    if in_use is not None:
        raise RuntimeError(
            f"sys.monitoring tool id {tool_id} is already in use by {in_use!r}; "
            f"pass a different tool_id (0-5) to tracypy.enable()",
        )

    _mon.use_tool_id(tool_id, name)
    try:
        for event in _ENTRY_EVENTS:
            _mon.register_callback(tool_id, event, _on_entry)
        for event in _EXIT_EVENTS:
            _mon.register_callback(tool_id, event, _on_exit)
        _mon.set_events(tool_id, _ALL_EVENTS)
    except BaseException:
        # Never leave a half-registered tool behind.
        _mon.free_tool_id(tool_id)
        raise

    _active_tool_id = tool_id


def disable() -> None:
    """Stop profiling and release the tool id. A no-op if not enabled.

    Turning events off takes effect immediately, including for frames already
    executing — this one and its callers included — so their exit events never
    arrive and their zones are closed here instead. That closes every zone open
    on this thread, so an explicit :class:`zone` block wrapping the call ends
    early too. Frames in flight on *other* threads are not reachable and stay
    open until the trace ends; that's inherent to stopping mid-call, so prefer
    disabling from a quiet moment.
    """
    global _active_tool_id
    if _active_tool_id is None:
        return

    tool_id = _active_tool_id
    # Stop delivery before unregistering so no event races a removed callback.
    _mon.set_events(tool_id, 0)
    for event in (*_ENTRY_EVENTS, *_EXIT_EVENTS):
        _mon.register_callback(tool_id, event, None)
    _mon.free_tool_id(tool_id)
    _active_tool_id = None
    _zone_unwind()


class profile:
    """Context manager that enables tracypy on entry and disables it on exit."""

    def __init__(self, tool_id: int = PROFILER_ID, name: str = "tracypy") -> None:
        """Store the ``tool_id`` and ``name`` to pass to :func:`enable` on entry."""
        self.tool_id = tool_id
        self.name = name

    def __enter__(self) -> Self:
        """Enable profiling and return the context manager."""
        enable(self.tool_id, self.name)
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """Disable profiling; never suppress an exception from the block."""
        disable()
        return False


class Severity(IntEnum):  # noqa: D101
    TRACE = 0
    DEBUG = 1
    INFO = 2
    WARNING = 3
    ERROR = 4
    FATAL = 5


_SEVERITIES = {member.name.lower(): member for member in Severity} | {
    "warn": Severity.WARNING,
    "critical": Severity.FATAL,
}


def message(text: str, severity: Severity | str | int = "info", color: int = 0) -> None:
    """Emit ``text`` as a message on the calling thread's Tracy timeline.

    ``severity`` may be a name (``"info"``, ``"warning"``, …), a :class:`Severity`,
    or the underlying int; it defaults to ``"info"``. ``color`` is ``0xRRGGBB``,
    or 0 to let the viewer color by severity::

        tracypy.message("cache miss", "warning")

    Messages are independent of :func:`enable`/zone capture and, like frame marks,
    are inert until a viewer connects. Text longer than 65534 bytes (Tracy's wire
    limit) is truncated at a UTF-8 boundary.
    """
    if isinstance(severity, str):
        try:
            severity = _SEVERITIES[severity.lower()]
        except KeyError:
            raise ValueError(f"unknown severity {severity!r}") from None
    _message(text, severity, color)


def trace(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.TRACE`. Shorthand for :func:`message`."""
    _message(text, 0, color)


def debug(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.DEBUG`. Shorthand for :func:`message`."""
    _message(text, 1, color)


def info(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.INFO`. Shorthand for :func:`message`."""
    _message(text, 2, color)


def warning(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.WARNING`. Shorthand for :func:`message`."""
    _message(text, 3, color)


def error(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.ERROR`. Shorthand for :func:`message`."""
    _message(text, 4, color)


def fatal(text: str, color: int = 0) -> None:
    """Emit ``text`` at :attr:`Severity.FATAL`. Shorthand for :func:`message`."""
    _message(text, 5, color)


# logging spells this level CRITICAL, and message() already takes that name.
critical = fatal


# Python's levels are coarser than Tracy's and don't line up numerically, so map
# by threshold: anything at or above a level takes that severity. Levels below
# DEBUG (custom TRACE levels, typically 5) fall through to Severity.TRACE.
_LEVEL_SEVERITIES = (
    (_logging.CRITICAL, Severity.FATAL),
    (_logging.ERROR, Severity.ERROR),
    (_logging.WARNING, Severity.WARNING),
    (_logging.INFO, Severity.INFO),
    (_logging.DEBUG, Severity.DEBUG),
)


def severity_for_level(levelno: int) -> Severity:
    """Map a :mod:`logging` level number onto the closest Tracy severity."""
    for level, severity in _LEVEL_SEVERITIES:
        if levelno >= level:
            return severity
    return Severity.TRACE


class LogHandler(_logging.Handler):
    """A :mod:`logging` handler that mirrors records into the Tracy timeline."""

    def emit(self, record: _logging.LogRecord) -> None:
        """Format ``record`` and emit it as a message at the mapped severity."""
        if not is_connected():
            return
        try:
            _message(self.format(record), severity_for_level(record.levelno), 0)
        except Exception:
            # A handler must never raise into the logging call site.
            self.handleError(record)


class frame:
    """Mark the wrapped block as a discontinuous Tracy frame named ``name``.

    Each named frame is its own timeline in the Tracy viewer, so a workload like
    a web request handler reads as one frame per request::

        with tracypy.frame("request"):
            handle(request)

    Frame marks are independent of :func:`enable`/zone capture and are inert
    until a viewer connects. The frame is always closed, even if the block
    raises.

    Frame names are a *global* concept in Tracy, not per-thread: two overlapping
    blocks with the *same* name (e.g. concurrent requests in one process) would
    interleave and corrupt that timeline. Give concurrent work distinct names,
    or use :func:`frame_mark` instead.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Store the frame ``name`` to open on entry and close on exit."""
        self.name = name

    def __enter__(self) -> Self:
        """Begin the named frame and return the context manager."""
        frame_mark_start(self.name)
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """End the named frame; never suppress an exception from the block."""
        frame_mark_end(self.name)
        return False


@_atexit.register
def _flush_on_exit() -> None:
    """Flush the trace tail to a connected viewer when the interpreter exits.

    Registered at import, so it runs after the user's own atexit handlers (LIFO).
    Stop sys.monitoring first so no in-flight callback emits into a profiler that
    ``_shutdown`` is finalizing, then tear Tracy down. Both calls are idempotent.
    """
    disable()
    _shutdown()
