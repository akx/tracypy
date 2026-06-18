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
3.14t build) on Linux, macOS, and Windows, with the Tracy client statically linked —
no toolchain or submodule needed. There is no source distribution, so on a platform
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
and `tracypy.is_enabled()`.

A runnable demo lives in [`examples/demo.py`](examples/demo.py) — connect a viewer,
then:

```sh
TRACY_NO_EXIT=1 python -m tracypy examples/demo.py
```

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

## Viewing a trace

Download or build the [Tracy profiler UI](https://github.com/wolfpld/tracy/releases)
and **Connect** to `localhost` before (or while) your program runs.
Because Tracy runs on-demand, nothing is captured until you connect.

tracypy vendors the Tracy client **v0.13.1**, so connect with a matching
**Tracy 0.13.x** viewer — the network protocol is versioned, and a mismatched
viewer won't connect.

For short scripts, the process may exit before the trace is fully sent. Try
`TRACY_NO_EXIT=1` so Tracy blocks at exit until the viewer has received everything:

```sh
TRACY_NO_EXIT=1 python -m tracypy my_script.py
```
