/*
 * tracypy._core — the C hot path bridging CPython's sys.monitoring (PEP 669)
 * to the Tracy profiler.
 *
 * sys.monitoring delivers per-frame events; we turn each Python frame's span of
 * execution into a Tracy zone. Entry events (PY_START / PY_RESUME / PY_THROW)
 * begin a zone and push its context onto a per-thread stack; exit events
 * (PY_RETURN / PY_YIELD / PY_UNWIND) pop and end it. Per PEP 669 every frame
 * exits via exactly one exit event, and a generator/coroutine resumed by
 * throw() re-enters via PY_THROW, so entries and exits stay balanced — which is
 * exactly what Tracy's strictly-nested, per-thread zones require.
 *
 * Registration is done from Python (tracypy/__init__.py) against sys.monitoring;
 * this module only exports the two fast callbacks.
 *
 * Hot-path design:
 *   - When no viewer is connected (the on-demand idle case) an entry skips all
 *     zone work and pushes an inactive ctx; the matching exit's
 *     ___tracy_emit_zone_end no-ops on it. Every entry still pushes and every
 *     exit still pops, so the per-thread stack stays balanced even if a viewer
 *     connects or disconnects mid-run.
 *   - When connected, each code object's Tracy source location is computed once
 *     and cached on its co_extra slot, so the steady-state begin is a single
 *     pointer-based ___tracy_emit_zone_begin with no per-call string work.
 *   - The cold paths (building/caching a source location, naming a thread, and
 *     the alloc-srcloc fallback) are kept out of line so the fast path inlines
 *     into the callback.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <tracy/TracyC.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if defined(_MSC_VER)
#  define TPY_TLS __declspec(thread)
#  define TPY_NOINLINE __declspec(noinline)
#else
#  include <pthread.h>
#  define TPY_TLS _Thread_local
#  define TPY_NOINLINE __attribute__((noinline))
#endif

/*
 * co_extra index for the cached source location. Requested once at module init
 * and read-only thereafter, so it needs no synchronization. -1 means caching is
 * unavailable and the connected path falls back to copying strings each call.
 */
static Py_ssize_t srcloc_index = -1;

/*
 * Id of the main thread, captured at import. The main thread has no OS-level
 * thread name, so we name it from its Python name instead. 0 means unknown.
 */
static unsigned long main_thread_ident = 0;

/*
 * A thread's first events fire during its bootstrap, before threading has set
 * the OS thread name or registered the thread. We retry reading the OS name for
 * up to this many events before falling back to the Python name.
 */
#define TPY_NAME_MAX_ATTEMPTS 200

/*
 * Per-thread LIFO stack of open Tracy zones. TracyCZoneCtx is a small by-value
 * struct, so we store contexts contiguously and grow geometrically.
 *
 * The buffer is intentionally leaked at thread exit: _Thread_local has no
 * destructor, and for a profiler a single per-thread allocation that lives as
 * long as the thread is an acceptable trade for keeping the hot path branchless.
 */
typedef struct {
    TracyCZoneCtx *data;
    size_t len;
    size_t cap;
    int named;             /* 0 until this thread has been named in Tracy */
    unsigned name_attempts;
} zone_stack_t;

static TPY_TLS zone_stack_t tls_stack = {0};

/* Push ctx; returns 0 on success, -1 if the stack could not grow. */
static inline int stack_push(zone_stack_t *st, TracyCZoneCtx ctx)
{
    if (st->len == st->cap) {
        size_t ncap = st->cap ? st->cap * 2 : 64;
        TracyCZoneCtx *nd = realloc(st->data, ncap * sizeof(*nd));
        if (nd == NULL) {
            return -1;
        }
        st->data = nd;
        st->cap = ncap;
    }
    st->data[st->len++] = ctx;
    return 0;
}

/*
 * Build a persistent Tracy source location for a code object and cache it on the
 * object's co_extra slot. Cold path: only the first time we see each code object.
 *
 * The cached block (the source-location struct followed by its two strings) is
 * deliberately never freed, and the co_extra index is requested with a NULL free
 * function: when a viewer is connected Tracy may query a source-location pointer
 * lazily, long after the originating call, so the data must outlive the code
 * object. The leak is bounded by the number of distinct code objects executed.
 *
 * On the free-threaded build, two threads first-seeing the same code object may
 * each build and store a srcloc; one store wins and the other block is leaked
 * (bounded, as above). Publication relies on pointer-sized stores being atomic on
 * the supported platforms, so a reader observes either NULL (and builds its own)
 * or a valid pointer, never a torn one; the block's fields are written before it
 * is stored and are immutable afterwards. The practical worst case is the
 * redundant leak, not corruption.
 *
 * Returns NULL if caching is unavailable (the caller then uses the alloc path).
 */
static TPY_NOINLINE const struct ___tracy_source_location_data *
build_srcloc(PyCodeObject *code)
{
    Py_ssize_t fnsz = 0, qnsz = 0;
    const char *file = PyUnicode_AsUTF8AndSize(code->co_filename, &fnsz);
    const char *qual = PyUnicode_AsUTF8AndSize(code->co_qualname, &qnsz);
    if (file == NULL) { PyErr_Clear(); file = "<unknown>"; fnsz = 9; }
    if (qual == NULL) { PyErr_Clear(); qual = "<unknown>"; qnsz = 9; }

    /* One allocation holds the struct and copies of both strings. */
    size_t hdr = sizeof(struct ___tracy_source_location_data);
    char *block = (char *)malloc(hdr + (size_t)fnsz + 1 + (size_t)qnsz + 1);
    if (block == NULL) {
        return NULL;
    }
    char *file_copy = block + hdr;
    char *qual_copy = file_copy + fnsz + 1;
    memcpy(file_copy, file, (size_t)fnsz);
    file_copy[fnsz] = '\0';
    memcpy(qual_copy, qual, (size_t)qnsz);
    qual_copy[qnsz] = '\0';

    struct ___tracy_source_location_data *sl =
        (struct ___tracy_source_location_data *)block;
    sl->name = qual_copy;      /* zone name shown in the viewer */
    sl->function = qual_copy;  /* function (same as the name here) */
    sl->file = file_copy;
    sl->line = (uint32_t)code->co_firstlineno;
    sl->color = 0;

    if (PyUnstable_Code_SetExtra((PyObject *)code, srcloc_index, sl) != 0) {
        /* Couldn't attach the cache (e.g. OOM). Drop this copy rather than leak
         * one on every future call, and fall back to the alloc path. */
        PyErr_Clear();
        free(block);
        return NULL;
    }
    return sl;
}

/* Fast lookup of the cached source location, building it on first sight. */
static inline const struct ___tracy_source_location_data *
get_srcloc(PyCodeObject *code)
{
    if (srcloc_index < 0) {
        return NULL;
    }
    void *cached = NULL;
    if (PyUnstable_Code_GetExtra((PyObject *)code, srcloc_index, &cached) == 0
        && cached != NULL) {
        return (const struct ___tracy_source_location_data *)cached;
    }
    return build_srcloc(code);
}

/*
 * Fallback begin when caching is unavailable: copy the strings into Tracy's own
 * buffer on every call. Cold path, only reached if co_extra is unusable.
 */
static TPY_NOINLINE TracyCZoneCtx
begin_zone_alloc(PyCodeObject *code)
{
    Py_ssize_t fnsz = 0, qnsz = 0;
    const char *file = PyUnicode_AsUTF8AndSize(code->co_filename, &fnsz);
    const char *qual = PyUnicode_AsUTF8AndSize(code->co_qualname, &qnsz);
    if (file == NULL) { PyErr_Clear(); file = "<unknown>"; fnsz = 9; }
    if (qual == NULL) { PyErr_Clear(); qual = "<unknown>"; qnsz = 9; }
    uint64_t srcloc = ___tracy_alloc_srcloc_name(
        (uint32_t)code->co_firstlineno,
        file, (size_t)fnsz,
        qual, (size_t)qnsz,
        qual, (size_t)qnsz,
        0);
    return ___tracy_emit_zone_begin_alloc(srcloc, 1 /* active */);
}

/* Set the current thread's Tracy name from threading.current_thread().name. */
static void
name_thread_from_python(void)
{
    /* Runs Python and so re-enters our callback; callers must have set st->named
     * first. Save/restore any in-flight exception (e.g. on a PY_THROW entry). */
    PyObject *saved_exc = PyErr_GetRaisedException();
    PyObject *threading = PyImport_ImportModule("threading");
    if (threading != NULL) {
        PyObject *thread = PyObject_CallMethod(threading, "current_thread", NULL);
        if (thread != NULL) {
            PyObject *name = PyObject_GetAttrString(thread, "name");
            if (name != NULL) {
                const char *s = PyUnicode_AsUTF8(name);
                if (s != NULL) {
                    ___tracy_set_thread_name(s);
                }
                Py_DECREF(name);
            }
            Py_DECREF(thread);
        }
        Py_DECREF(threading);
    }
    PyErr_Clear();
    if (saved_exc != NULL) {
        PyErr_SetRaisedException(saved_exc);
    }
}

/*
 * Name the current thread in Tracy, once. Cold path, called on early connected
 * events until the thread is named.
 *
 * Prefer the OS thread name, which CPython sets for spawned threads (no Python
 * calls needed). A thread's earliest events fire during bootstrap, before that
 * name exists, so if it is still empty we retry on later events — except for the
 * main thread (which never gets an OS name) and any thread whose name never
 * appears, which fall back to the Python-level name.
 */
static TPY_NOINLINE void
name_current_thread(zone_stack_t *st)
{
#if !defined(_MSC_VER)
    char buf[64];
    if (pthread_getname_np(pthread_self(), buf, sizeof(buf)) == 0 && buf[0] != '\0') {
        ___tracy_set_thread_name(buf);
        st->named = 1;
        return;
    }
#endif
    int is_main = (main_thread_ident != 0
                   && PyThread_get_thread_ident() == main_thread_ident);
    if (!is_main && ++st->name_attempts < TPY_NAME_MAX_ATTEMPTS) {
        return;  /* probably a worker still bootstrapping; retry on a later event */
    }
    st->named = 1;  /* set before the Python calls below, which re-enter us */
    name_thread_from_python();
}

/*
 * sys.monitoring callbacks. The code object is always argument 0 for every event
 * we subscribe to; later arguments (offset, retval, exception) are ignored. We
 * return None — never sys.monitoring.DISABLE, which would permanently silence the
 * location and unbalance the zone stack.
 */
static PyObject *
cb_entry(PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs >= 1 && PyCode_Check(args[0])) {
        zone_stack_t *st = &tls_stack;  /* resolve TLS once */
        TracyCZoneCtx ctx;
        if (___tracy_connected()) {
            if (!st->named) {
                name_current_thread(st);
            }
            const struct ___tracy_source_location_data *sl =
                get_srcloc((PyCodeObject *)args[0]);
            ctx = sl != NULL ? ___tracy_emit_zone_begin(sl, 1 /* active */)
                             : begin_zone_alloc((PyCodeObject *)args[0]);
        } else {
            /* No viewer: skip all zone work. An inactive ctx keeps the stack
             * balanced; the matching exit's emit_zone_end no-ops on it. */
            ctx.id = 0;
            ctx.active = 0;
        }
        if (stack_push(st, ctx) != 0) {
            /* Out of memory: end immediately to keep the stack balanced. */
            ___tracy_emit_zone_end(ctx);
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
cb_exit(PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    (void)args;
    (void)nargs;
    zone_stack_t *st = &tls_stack;  /* resolve TLS once */
    if (st->len != 0) {
        /* emit_zone_end no-ops on an inactive ctx (idle-pushed or pre-enable). */
        ___tracy_emit_zone_end(st->data[--st->len]);
    }
    Py_RETURN_NONE;
}

/* ------------------------------------------------------------------------- *
 * Frame marks.
 *
 * Tracy frames delimit recurring units of work (classically one render frame;
 * here, e.g. one Django request). Two flavors are exposed:
 *   - continuous:    frame_mark(name)       — a boundary between frames
 *   - discontinuous: frame_mark_start(name) — an explicit begin / end pair for
 *                    frame_mark_end(name)     work that doesn't run every tick
 *
 * Tracy stores the name POINTER and never copies it, and matches a discontinuous
 * start to its end by that same pointer. A given frame name must therefore
 * resolve to one stable, persistent address, so we intern names into leaked C
 * copies keyed by content. The set of distinct frame names is tiny, so a linear
 * scan under a lightweight mutex is ample. The mutex (not the GIL) guards this
 * state, preserving the module's Py_MOD_GIL_NOT_USED promise.
 * ------------------------------------------------------------------------- */
typedef struct intern_node {
    struct intern_node *next;
    size_t len;
    char name[];  /* NUL-terminated, content-addressed, deliberately never freed */
} intern_node;

static intern_node *intern_head = NULL;
static PyMutex intern_lock = {0};

/* Return a stable, persistent pointer to an interned copy of s, or NULL on OOM. */
static const char *
intern_frame_name(const char *s, size_t len)
{
    const char *result = NULL;
    PyMutex_Lock(&intern_lock);
    for (intern_node *n = intern_head; n != NULL; n = n->next) {
        if (n->len == len && memcmp(n->name, s, len) == 0) {
            result = n->name;
            break;
        }
    }
    if (result == NULL) {
        intern_node *n = (intern_node *)malloc(sizeof(*n) + len + 1);
        if (n != NULL) {
            n->len = len;
            memcpy(n->name, s, len);
            n->name[len] = '\0';
            n->next = intern_head;
            intern_head = n;
            result = n->name;
        }
    }
    PyMutex_Unlock(&intern_lock);
    return result;
}

/*
 * Resolve a Python str argument to an interned frame name. On error sets an
 * exception and returns -1; on success stores the pointer into *out (NULL only
 * for an absent or None name) and returns 0.
 */
static int
resolve_frame_name(PyObject *arg, const char **out)
{
    if (arg == NULL || arg == Py_None) {
        *out = NULL;
        return 0;
    }
    if (!PyUnicode_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "frame name must be a str or None");
        return -1;
    }
    Py_ssize_t len = 0;
    const char *s = PyUnicode_AsUTF8AndSize(arg, &len);
    if (s == NULL) {
        return -1;
    }
    const char *name = intern_frame_name(s, (size_t)len);
    if (name == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    *out = name;
    return 0;
}

static PyObject *
py_frame_mark(PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    const char *name = NULL;
    if (nargs >= 1 && resolve_frame_name(args[0], &name) != 0) {
        return NULL;
    }
    ___tracy_emit_frame_mark(name);
    Py_RETURN_NONE;
}

static PyObject *
py_frame_mark_start(PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    const char *name = NULL;
    if (nargs != 1 || args[0] == Py_None) {
        PyErr_SetString(PyExc_TypeError,
                        "frame_mark_start(name) requires a str name");
        return NULL;
    }
    if (resolve_frame_name(args[0], &name) != 0) {
        return NULL;
    }
    ___tracy_emit_frame_mark_start(name);
    Py_RETURN_NONE;
}

static PyObject *
py_frame_mark_end(PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    const char *name = NULL;
    if (nargs != 1 || args[0] == Py_None) {
        PyErr_SetString(PyExc_TypeError,
                        "frame_mark_end(name) requires a str name");
        return NULL;
    }
    if (resolve_frame_name(args[0], &name) != 0) {
        return NULL;
    }
    ___tracy_emit_frame_mark_end(name);
    Py_RETURN_NONE;
}

static PyObject *
py_shutdown(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    /* Flush buffered trace data to a connected viewer and tear down Tracy's
     * worker thread. tracypy calls this from an atexit hook (see __init__.py).
     *
     * Idempotent and guarded: a no-op once the profiler is stopped, so calling
     * it twice is safe. After it returns, Tracy has been finalized and no Tracy
     * API (zones, frame marks) may be used again, so the atexit hook disables
     * sys.monitoring first. */
    if (TracyCIsStarted) {
        ___tracy_shutdown_profiler();
    }
    Py_RETURN_NONE;
}

static PyMethodDef tracypy_methods[] = {
    {"_on_entry", _PyCFunction_CAST(cb_entry), METH_FASTCALL,
     "sys.monitoring callback for PY_START/PY_RESUME/PY_THROW; begins a Tracy zone."},
    {"_on_exit", _PyCFunction_CAST(cb_exit), METH_FASTCALL,
     "sys.monitoring callback for PY_RETURN/PY_YIELD/PY_UNWIND; ends a Tracy zone."},
    {"frame_mark", _PyCFunction_CAST(py_frame_mark), METH_FASTCALL,
     "frame_mark(name=None): emit a continuous Tracy frame boundary.\n\n"
     "With no name, marks the default frame; with a name, a named continuous\n"
     "frame. Independent of zone capture and inert until a viewer connects."},
    {"frame_mark_start", _PyCFunction_CAST(py_frame_mark_start), METH_FASTCALL,
     "frame_mark_start(name): begin a discontinuous (named) Tracy frame.\n\n"
     "Pair with frame_mark_end(name). Frame names are a global timeline, so give\n"
     "concurrent work distinct names to avoid interleaving."},
    {"frame_mark_end", _PyCFunction_CAST(py_frame_mark_end), METH_FASTCALL,
     "frame_mark_end(name): end the discontinuous frame begun by "
     "frame_mark_start(name)."},
    {"_shutdown", py_shutdown, METH_NOARGS,
     "Flush buffered trace data and finalize Tracy. Called from an atexit hook;"
     " idempotent. No Tracy API may be used after this."},
    {NULL, NULL, 0, NULL},
};

/* Capture the main thread's id so we can name it (it has no OS thread name). */
static void
capture_main_thread_ident(void)
{
    PyObject *threading = PyImport_ImportModule("threading");
    if (threading == NULL) {
        PyErr_Clear();
        return;
    }
    PyObject *main_thread = PyObject_CallMethod(threading, "main_thread", NULL);
    if (main_thread != NULL) {
        PyObject *ident = PyObject_GetAttrString(main_thread, "ident");
        if (ident != NULL) {
            unsigned long value = PyLong_AsUnsignedLong(ident);
            if (!PyErr_Occurred()) {
                main_thread_ident = value;
            }
            Py_DECREF(ident);
        }
        Py_DECREF(main_thread);
    }
    Py_DECREF(threading);
    PyErr_Clear();
}

static int
exec_module(PyObject *Py_UNUSED(module))
{
    /* Manual-lifetime build: bring Tracy up now, at import, before any zone or
     * frame mark can be emitted (the emit paths assume a live profiler). The
     * matching ___tracy_shutdown_profiler() runs via atexit; see __init__.py. */
    ___tracy_startup_profiler();

    if (srcloc_index < 0) {
        /* NULL free function: cached source locations are never freed (Tracy may
         * read them lazily; see build_srcloc). A -1 result leaves caching off and
         * the connected path uses the alloc fallback. */
        srcloc_index = PyUnstable_Eval_RequestCodeExtraIndex(NULL);
    }
    capture_main_thread_ident();
    return 0;
}

static PyModuleDef_Slot tracypy_slots[] = {
    {Py_mod_exec, exec_module},
    {Py_mod_multiple_interpreters, Py_MOD_MULTIPLE_INTERPRETERS_NOT_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef tracypy_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_core",
    .m_doc = "Tracy profiler bridge for Python's sys.monitoring (PEP 669).",
    .m_size = 0,
    .m_methods = tracypy_methods,
    .m_slots = tracypy_slots,
};

PyMODINIT_FUNC
PyInit__core(void)
{
    return PyModuleDef_Init(&tracypy_module);
}
