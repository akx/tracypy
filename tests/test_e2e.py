"""End-to-end test: capture a real trace with ``tracy-capture``.

Runs a Python workload under tracypy as a client, connects the headless
``tracy-capture`` tool to it, and asserts a non-trivial trace comes out the
other end. Skipped unless ``tracy-capture`` is on PATH (or pointed at by the
``TRACY_CAPTURE`` env var); CI builds it from the vendored Tracy submodule.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_CAPTURE = os.environ.get("TRACY_CAPTURE") or shutil.which("tracy-capture")

pytestmark = pytest.mark.skipif(
    _CAPTURE is None,
    reason="tracy-capture not found (set TRACY_CAPTURE or put it on PATH)",
)

# Waits for a viewer to connect, then generates zones via deep recursion for
# longer than the capture window. is_connected() makes this deterministic:
# on-demand capture records nothing until tracy-capture has connected, so the
# work must come after, and it must keep flowing while capture is recording.
_CLIENT = """
import time
import tracypy

with tracypy.profile():
    deadline = time.monotonic() + 20
    while not tracypy.is_connected() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not tracypy.is_connected():
        raise SystemExit("no viewer connected")

    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    end = time.monotonic() + 10
    while time.monotonic() < end:
        fib(16)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_capture_records_zones(tmp_path: Path) -> None:
    script = tmp_path / "client.py"
    script.write_text(_CLIENT)
    out = tmp_path / "trace.tracy"
    port = _free_port()

    # The client opens its listen socket at import, so start it first; the
    # client then blocks until capture connects (well within its 20s window).
    # TRACY_ONLY_IPV4 forces an IPv4 listen socket to match capture's IPv4
    # connect: on Linux the client otherwise listens on IPv6 without clearing
    # IPV6_V6ONLY, so `-a 127.0.0.1` can't reach it where IPv6 is restricted
    # (Docker, CI runners).
    client = subprocess.Popen(
        [sys.executable, str(script)],
        env={**os.environ, "TRACY_PORT": str(port), "TRACY_ONLY_IPV4": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # -s bounds the capture to a fixed window so the test doesn't hinge on
    # client-disconnect timing (capture is silent on a non-TTY until it
    # finalizes). The client keeps producing zones for longer than this window.
    capture = subprocess.Popen(
        [_CAPTURE, "-f", "-o", str(out), "-a", "127.0.0.1", "-p", str(port), "-s", "4"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Capture writes the trace and exits once its -s window elapses.
        cap_out, _ = capture.communicate(timeout=60)
        client_out, _ = client.communicate(timeout=60)
    finally:
        for proc in (capture, client):
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    assert client.returncode == 0, client_out
    assert capture.returncode == 0, cap_out
    # A leading non-zero digit means we actually recorded zones (RealToString
    # formats with separators, e.g. "Zones: 40,000").
    assert re.search(r"Zones:\s*[1-9]", cap_out), cap_out
    assert out.exists() and out.stat().st_size > 0
