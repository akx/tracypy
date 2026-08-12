# tracypy

A lightweight bridge from Python's [`sys.monitoring`](https://docs.python.org/3.13/library/sys.monitoring.html)
(PEP 669) to the [Tracy](https://github.com/wolfpld/tracy) profiler, as a plain-C
extension module. Every Python function call becomes a Tracy zone, so you can see
your program's call tree and timing in the Tracy viewer.

## How it works

`sys.monitoring` delivers per-frame events; tracypy turns each frame's execution
span into a Tracy zone. Entry events (`PY_START` / `PY_RESUME` / `PY_THROW`) begin a
zone and exit events (`PY_RETURN` / `PY_YIELD` / `PY_UNWIND`) end it, pushed/popped on
a per-thread stack. The callbacks are fast C functions; registration is the only
part done in Python. Tracy is compiled in **on-demand** mode, so enabling tracypy is
essentially free until a Tracy viewer connects.

The extension is built free-threading-ready (`Py_MOD_GIL_NOT_USED`), so it works on
the free-threaded (no-GIL) builds of CPython too. Only *Python* function calls become
zones — work inside C builtins or extension modules doesn't show up as its own zone.
Each thread's zone stack is released when the process exits rather than when the thread
does, so profiling a process that spawns unbounded short-lived threads grows memory
slowly over time; for ordinary worker pools this is a non-issue.

## Install

```sh
pip install tracypy        # or: uv pip install tracypy
```

Prebuilt wheels are published for CPython 3.13 and 3.14 (including the free-threaded
3.14t build) on Linux, macOS, and 64-bit Windows, with the Tracy client statically
linked — no toolchain or submodule needed. 32-bit Windows is not supported: Tracy's
client can't be built for it. There is no source distribution, so on a platform
or Python without a matching wheel, install from a Git checkout (below) instead.

### From source

Building needs Python ≥ 3.13, a C/C++ toolchain, and CMake. The Tracy client is
vendored as a git submodule, so clone recursively (or init the submodule), then
install:

```sh
git submodule update --init        # if you didn't clone with --recurse-submodules
uv pip install .                   # or: pip install .
```

## Usage

```python
import tracypy

with tracypy.profile():
    my_workload()
```

Or run a script/module under the profiler without editing it:

```sh
python -m tracypy my_script.py [args...]
python -m tracypy -m my.module [args...]
```

`profile()` forwards its `tool_id` / `name` arguments to `enable()`. The low-level
API is `tracypy.enable(tool_id=PROFILER_ID, name="tracypy")`, `tracypy.disable()`,
and `tracypy.is_enabled()`. `tracypy.is_connected()` reports whether a Tracy
viewer is currently attached — useful since on-demand capture records nothing
until one connects.

A runnable demo lives in [`examples/demo.py`](examples/demo.py) — connect a viewer,
then:

```sh
python -m tracypy examples/demo.py
```

## Zones

Every Python function call is already its own zone.
When a function is too coarse, open an explicit zone around just the part you care about:

```python
with tracypy.zone("db query", text=sql, color=0x0088FF):
    cursor.execute(sql)
```

Explicit zones nest inside the automatic per-function ones.

You can also annotate whichever zone is currently open:

```python
def handle(request):
    tracypy.zone_text(f"user={request.user_id}")  # extra detail in the viewer
    tracypy.zone_value(len(request.body))         # a number shown on the zone
    tracypy.zone_color(0xAA0000)
    tracypy.zone_name("handle:" + request.path)   # override the displayed name
```

Passing `text=` / `value=` / `color=` to `tracypy.zone(...)`
does the same thing in one step, and is checked when the zone is created —
so a bad argument is reported at the line that wrote it.


> [!NOTE]
> **Don't suspend inside a `tracypy.zone(...)` block.**
> A `yield` or `await` between entry and exit interleaves with the per-frame zones
> `sys.monitoring` pushes, and Tracy's zones are a strict per-thread stack.
> Nothing corrupts, but the zone closes at the wrong point.

## Frames

Tracy frames delimit recurring units of work,
so you can see per-unit timing statistics (min/max/avg, a frame-time graph) on top of the zone call tree. For web apps, for instance,
a natural fit is "one frame per request":

```python
import tracypy

# Discontinuous frame: an explicit span with a name of its own timeline.
with tracypy.frame("request"):
    handle(request)
```

For a Django app, drop in a middleware:

```python
import tracypy

class TracyFrameMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with tracypy.frame("request"):
            return self.get_response(request)
```

There's also a continuous-frame boundary marker,
for the classic game-loop style where every tick is one frame:

```python
tracypy.frame_mark()          # default frame boundary
tracypy.frame_mark("render")  # a named continuous frame
```

and the bare discontinuous primitives,

* `tracypy.frame_mark_start(name)`
* `tracypy.frame_mark_end(name)`

if the context manager doesn't fit.

Frame marks are independent of zone capture — they work whether or not
`enable()` is on, and are inert until a viewer connects.

## Messages

Messages are timestamped strings on the emitting thread's timeline.

```python
tracypy.message("cache miss")                      # defaults to "info"
tracypy.message("retrying upload", "warning")
tracypy.message("checkpoint", "info", 0x00AA00)    # 0xRRGGBB, 0 = viewer default
```

Severity is one of `trace`, `debug`, `info`, `warning`, `error`, `fatal`
(`warn` and `critical` are accepted as aliases, and case doesn't matter).

If you prefer symbols to strings, `tracypy.Severity.WARNING` and the raw ints work too.

Each severity also has a fast shorthand, named as in `logging`:

```python
tracypy.warning("retrying upload")
tracypy.critical("out of disk", 0xFF0000)
```

Since Python's logging levels line up with those severities,
you can mirror your existing log output into the trace with a handler:

```python
import logging, tracypy

logging.getLogger().addHandler(tracypy.LogHandler())
```

Log records then appear inline with the zones that produced them, colored by level.
`LogHandler` is a plain `logging.Handler`, so levels, filters, and formatters work as usual.
Text longer than 65534 bytes (Tracy's wire limit) is truncated at a UTF-8 boundary.

## Viewing a trace

Download or build the [Tracy profiler UI](https://github.com/wolfpld/tracy/releases)
and **Connect** to `localhost` before (or while) your program runs.
Because Tracy runs on-demand, nothing is captured until you connect.

tracypy vendors the Tracy client **v0.14.0**, so connect with a matching
**Tracy 0.14.x** viewer — the network protocol is versioned, and a mismatched
viewer won't connect.

On a clean exit tracypy flushes the buffered trace to a connected viewer
automatically (via an `atexit` hook), so even short scripts don't lose their
tail — just keep the viewer connected until the run finishes.
