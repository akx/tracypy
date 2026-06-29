cmake_minimum_required(VERSION 3.15...4.0)
project(${SKBUILD_PROJECT_NAME} LANGUAGES C CXX)

# _core.c uses C11 (_Thread_local for the per-thread zone stack).
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
# Tracy's client needs C++17 (e.g. `std::atomic<T*> x = nullptr;` in TracyProfiler.cpp
# relies on guaranteed copy elision); it under-declares cxx_std_11, so pin 17 here.
# Without this, GCC builds it as -std=gnu++11 and fails on the deleted atomic copy ctor.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# _core is a shared module that statically links Tracy, so everything it pulls in
# must be position-independent. Without this, Tracy's static libTracyClient.a is
# built without -fPIC and the link fails on x86-64 Linux (R_X86_64_TPOFF32 against
# a thread-local symbol can't go into a .so).
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# 3.13 is the floor: the C module uses PyMutex and Py_MOD_GIL_NOT_USED.
find_package(Python 3.13 REQUIRED COMPONENTS Development.Module)

# EXCLUDE_FROM_ALL drops Tracy's own install() rules, so the wheel ships only our _core
# module instead of Tracy's headers and CMake package files under site-packages/{include,lib}.
set(TRACY_ENABLE    ON CACHE BOOL "" FORCE)
set(TRACY_ON_DEMAND ON CACHE BOOL "" FORCE)
# Manual lifetime (which requires delayed init): tracypy starts Tracy's worker
# explicitly at module import and shuts it down from an atexit hook, so a clean
# Python exit flushes the buffered trace tail to a connected viewer. The default
# mode leaks the profiler and relies on C++ static destruction, which in an
# embedded .so runs too late (after Python's atexit) or not at all.
set(TRACY_DELAYED_INIT    ON CACHE BOOL "" FORCE)
set(TRACY_MANUAL_LIFETIME ON CACHE BOOL "" FORCE)
# Use a lower-resolution timer instead of exit(1) on CPUs/VMs without an invariant TSC.
set(TRACY_TIMER_FALLBACK ON CACHE BOOL "" FORCE)
# Don't install Tracy's signal/crash handlers into the host Python process (they'd
# stomp faulthandler, debuggers, and the app's own handlers).
set(TRACY_NO_CRASH_HANDLER ON CACHE BOOL "" FORCE)
add_subdirectory(third_party/tracy EXCLUDE_FROM_ALL)

# WITH_SOABI gives the correct ABI-tagged filename (e.g. _core.cpython-314-darwin.so).
python_add_library(_core MODULE WITH_SOABI src/_core.c)
target_link_libraries(_core PRIVATE Tracy::TracyClient)  # also propagates the TRACY_ENABLE define

install(TARGETS _core DESTINATION ${SKBUILD_PROJECT_NAME})
