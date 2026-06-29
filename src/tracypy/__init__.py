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
from sys import monitoring as _mon
from typing import Self

from tracypy._core import (
    _on_entry,
    _on_exit,
    _shutdown,
    frame_mark,
    frame_mark_end,
    frame_mark_start,
)

__all__ = [
    "enable",
    "disable",
    "is_enabled",
    "profile",
    "PROFILER_ID",
    "frame_mark",
    "frame_mark_start",
    "frame_mark_end",
    "frame",
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
    """Stop profiling and release the tool id. A no-op if not enabled."""
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
