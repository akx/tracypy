"""tracypy demo — a few pure-Python workloads that make for an interesting Tracy trace.

Run it directly (it enables tracypy itself)::

    python examples/demo.py

…or under the runner, which is equivalent::

    python -m tracypy examples/demo.py

Connect a Tracy viewer to ``localhost`` to capture the trace. Because Tracy runs
on-demand, nothing is recorded until you connect. For a short run like this, set
``TRACY_NO_EXIT=1`` so the process waits for Tracy to receive everything before
exiting::

    TRACY_NO_EXIT=1 python examples/demo.py

Only *Python* function calls become zones (that is what tracypy captures), so the
work here is deliberately pure Python — no numpy, no C builtins doing the heavy
lifting. Each phase has a different call-tree shape, so they look distinct in the
viewer. Tweak the knobs below to make the run longer or shorter.
"""

from __future__ import annotations

import random
import threading
import time

import tracypy

# --- knobs: dial these up for a longer run, down for a quicker one -----------
# Without a Tracy viewer connected the callbacks are nearly free, so this races
# by in a second or two. With a viewer attached every call is recorded, so the
# same run takes meaningfully longer and produces a multi-million-zone trace.
FIB_N = 33  # naive fib(33) ≈ 11M calls — the deep recursive flame graph
SORT_SIZE = 60_000
MANDEL_WIDTH, MANDEL_HEIGHT, MANDEL_ITERS = 320, 220, 100
QUEENS_N = 11
THREAD_WORKERS, THREAD_ROUNDS = 4, 8


# --- Phase 1: naive recursive Fibonacci (deep self-recursion) ----------------
def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


# --- Phase 2: merge sort (divide-and-conquer recursion) ----------------------
def merge_sort(values: list[float]) -> list[float]:
    if len(values) <= 1:
        return values
    mid = len(values) // 2
    return merge(merge_sort(values[:mid]), merge_sort(values[mid:]))


def merge(left: list[float], right: list[float]) -> list[float]:
    out: list[float] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            out.append(left[i])
            i += 1
        else:
            out.append(right[j])
            j += 1
    out.extend(left[i:])
    out.extend(right[j:])
    return out


# --- Phase 3: Mandelbrot set (nested loops, one call per pixel) ---------------
def escape_time(cx: float, cy: float, max_iter: int) -> int:
    x = y = 0.0
    for i in range(max_iter):
        x2, y2 = x * x, y * y
        if x2 + y2 > 4.0:
            return i
        y = 2.0 * x * y + cy
        x = x2 - y2 + cx
    return max_iter


def mandelbrot(width: int, height: int, max_iter: int) -> int:
    total = 0
    for py in range(height):
        cy = (py / height) * 2.4 - 1.2
        for px in range(width):
            cx = (px / width) * 3.0 - 2.0
            total += escape_time(cx, cy, max_iter)
    return total


# --- Phase 4: N-Queens (variable-depth backtracking recursion) ---------------
def n_queens(n: int) -> int:
    cols: set[int] = set()
    diag1: set[int] = set()
    diag2: set[int] = set()

    def place(row: int) -> int:
        if row == n:
            return 1
        found = 0
        for col in range(n):
            if col in cols or (row - col) in diag1 or (row + col) in diag2:
                continue
            cols.add(col)
            diag1.add(row - col)
            diag2.add(row + col)
            found += place(row + 1)
            cols.discard(col)
            diag1.discard(row - col)
            diag2.discard(row + col)
        return found

    return place(0)


# --- Phase 5: worker threads (each gets its own Tracy thread timeline) --------
def worker(name: str, rounds: int) -> None:
    rng = random.Random(hash(name) & 0xFFFF)
    for _ in range(rounds):
        merge_sort([rng.random() for _ in range(2000)])


def run_threads(workers: int, rounds: int) -> int:
    threads = [
        threading.Thread(
            target=worker,
            args=(f"worker-{i}", rounds),
            name=f"worker-{i}",
        )
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return workers


def _summarize(result: object) -> str:
    if isinstance(result, list):
        return f"list[{len(result)}]"
    return repr(result)


def timed(label: str, fn, *args) -> object:
    t0 = time.perf_counter()
    result = fn(*args)
    print(f"  {label:<22} {time.perf_counter() - t0:6.2f}s  -> {_summarize(result)}")
    return result


def workload() -> None:
    print("running workloads (this is the part Tracy captures)...")
    timed(f"fib({FIB_N})", fib, FIB_N)

    rng = random.Random(1234)
    data = [rng.randint(0, 1_000_000) for _ in range(SORT_SIZE)]
    timed(f"merge_sort({SORT_SIZE})", merge_sort, data)

    timed(
        f"mandelbrot({MANDEL_WIDTH}x{MANDEL_HEIGHT})",
        mandelbrot,
        MANDEL_WIDTH,
        MANDEL_HEIGHT,
        MANDEL_ITERS,
    )
    timed(f"n_queens({QUEENS_N})", n_queens, QUEENS_N)
    timed(f"{THREAD_WORKERS} sort threads", run_threads, THREAD_WORKERS, THREAD_ROUNDS)


def main() -> None:
    print("tracypy demo — connect a Tracy viewer to localhost to capture the trace.")
    t0 = time.perf_counter()
    # Works whether you run this directly or via `python -m tracypy demo.py`.
    if tracypy.is_enabled():
        workload()
    else:
        with tracypy.profile():
            workload()
    print(f"done in {time.perf_counter() - t0:.2f}s total.")


if __name__ == "__main__":
    main()
