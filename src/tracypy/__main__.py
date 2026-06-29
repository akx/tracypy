"""Run a script or module under tracypy without editing it.

Usage::

    python -m tracypy my_script.py [args...]
    python -m tracypy -m my.module [args...]

Connect a Tracy viewer to localhost to capture the trace. tracypy flushes the
buffered trace on a clean exit, so keep the viewer connected until the run ends.
"""

from __future__ import annotations

import os
import runpy
import sys

import tracypy

_USAGE = (
    "usage: python -m tracypy <script.py> [args...]\n"
    "       python -m tracypy -m <module> [args...]\n"
    "\n"
    "Connect a Tracy viewer to localhost to capture the trace.\n"
    "The buffered trace is flushed on a clean exit; keep the viewer connected."
)


def main(argv: list[str] | None = None) -> int:
    """Profile the script or module named in ``argv`` and return an exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    if not args:
        # No target is a usage error: message to stderr, nonzero exit.
        print(_USAGE, file=sys.stderr)
        return 1

    if args[0] == "-m":
        if len(args) < 2:
            print(_USAGE, file=sys.stderr)
            return 1
        run_module, target, rest = True, args[1], args[2:]
    else:
        run_module, target, rest = False, args[0], args[1:]

    # Present the target with the argv and sys.path[0] it would see when run directly.
    sys.argv = [target, *rest]

    tracypy.enable()
    try:
        if run_module:
            sys.path.insert(0, "")  # cwd, as `python -m` does
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
            runpy.run_path(target, run_name="__main__")
    finally:
        tracypy.disable()
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
