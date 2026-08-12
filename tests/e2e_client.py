# Waits for a viewer to connect, then generates zones via deep recursion for
# longer than the capture window. is_connected() makes this deterministic:
# on-demand capture records nothing until tracy-capture has connected, so the
# work must come after, and it must keep flowing while capture is recording.

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

    # Messages only take the real wire path once connected, so emit each
    # severity here. The oversized ones cover the uint16_t clamp end to end;
    # capture can't report message text, so this proves they don't break the
    # stream, not that the truncation lands where we think it does.
    for severity in ("trace", "debug", "info", "warning", "error", "fatal"):
        tracypy.message(f"hello from {severity}", severity)
    tracypy.message("colored", "warning", 0xFF8800)
    tracypy.message("x" * 200_000)
    tracypy.message("€" * 100_000)

    # Explicit zones and annotations, on the wire. Tracy validates zone nesting
    # server-side (TRACY_NO_VERIFY is off), so if these mis-nested against the
    # per-frame zones sys.monitoring pushes, capture would fail rather than
    # quietly produce a wrong trace.
    def annotated(n):
        with tracypy.zone("annotated", text=f"n={n}", value=n, color=0x0088FF):
            tracypy.zone_text("added inside")
            tracypy.zone_value(n * 2)
            return fib(n)

    def spans_a_yield():
        with tracypy.zone("spans a yield"):
            yield 1
            yield 2

    end = time.monotonic() + 10
    while time.monotonic() < end:
        fib(16)
        with tracypy.zone("outer"):
            with tracypy.zone("inner", text="nested"):
                annotated(10)
        list(spans_a_yield())
        try:
            with tracypy.zone("raises"):
                raise ValueError("unwind through a zone")
        except ValueError:
            pass
