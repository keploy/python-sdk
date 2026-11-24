"""
Keploy Python agent: per-test code coverage of the app, for keploy's dedup and
code coverage (`keploy test --dedup`).

Importing it (first thing in the app, or through the sitecustomize hook that
`python -m keploy_agent --pythonpath` puts on PYTHONPATH) binds this
process's own control socket, /tmp/coverage_control.d/<pid>.sock, before the
import returns, and starts measuring the app's own files (see _files). Every
Python process of the app does the same (a reloader and its server child,
prefork workers, which bind their own after the fork), so every process is
measured, and keploy sends each command to each and merges their records:

  START <set>/<case>  a test's window begins: the lines noted so far go.
  END <set>/<case>    the window ends: its lines go to keploy, by app-root-
                      relative path, on the data socket, with this
                      process's pid, build and the hashes of the files.
  TABLE               the build table (every app file's coverable lines,
                      _table) goes to keploy on the data socket, as frames.

Every command gets one answer line before the close: "ACK proto=2" when it
was taken, "IGNORED proto=2 <why>" when it was read and not taken. proto=2
says this agent answers every command it reads, so keploy reads a close with
no answer as a command not taken.

The first START that sees a new data socket (keploy starts a receiver per
test run) sends the INDEX record first: the build id of the app files and
how they were found, which tells keploy to ask for the table. A process's
first START also sends its STARTUP record: the lines it ran before its first
window (imports, app start), which keploy credits to no test.
"""

from __future__ import annotations

import atexit
import gc
import hashlib
import inspect
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import weakref

from . import _entries, _files, _hooks, _measure, _table

import _thread


def _original(mod: str, name: str, current):
    """The real primitive, never gevent's or eventlet's: the agent's own
    threads, locks, sleeps and sockets must not become greenlets (a
    greenlet on a hub no one runs never answers keploy). Read at import,
    before the app patches; from gevent or eventlet when they already did."""
    g = sys.modules.get("gevent.monkey")
    if g is not None:
        try:
            return g.get_original(mod, name)
        except Exception:
            pass
    e = sys.modules.get("eventlet.patcher")
    if e is not None:
        try:
            return getattr(e.original(mod), name)
        except Exception:
            pass
    return current


_start_new_thread = _original("_thread", "start_new_thread", _thread.start_new_thread)
_allocate_lock = _original("_thread", "allocate_lock", _thread.allocate_lock)
_sleep = _original("time", "sleep", time.sleep)
_Socket = _original("socket", "socket", socket.socket)


def _spawn(fn, *args) -> None:
    """Run fn on a real OS thread of the agent's own (daemonic). It is
    known as keploy's from its start (END never sees it come up as an app
    thread), and leaves _OURS only after its starter added it."""
    gate = _allocate_lock()
    gate.acquire()

    def run():
        me = _thread.get_ident()
        _OURS.add(me)
        try:
            with gate:
                pass
            fn(*args)
        except Exception as e:  # an agent thread never takes the app down
            log.debug(f"keploy agent thread {getattr(fn, '__name__', fn)} failed: {e!r}")
        finally:
            _OURS.discard(me)
            # A dummy Thread logging made for this raw thread goes with it.
            t = threading._active.get(me)
            if isinstance(t, threading._DummyThread):
                threading._active.pop(me, None)
    run.__name__ = run.__qualname__ = "keploy:" + getattr(fn, "__name__", "?")
    try:
        _OURS.add(_start_new_thread(run, ()))
    finally:
        gate.release()


_table.SOCKET = _Socket


# The agent's own threads (raw OS threads), and, before 3.12, the threads
# alive before it started (its tracer never reaches them).
_OURS: set = _measure.OURS
_UNTRACED: set = set()


class _Flag:
    """threading.Event on the real primitives (see _original)."""

    def __init__(self):
        self._on = False

    def set(self):
        self._on = True

    def is_set(self):
        return self._on

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while not self._on:
            if end is not None and time.monotonic() >= end:
                return False
            _sleep(0.01)
        return True

# ------------------------------------------------ configuration
CONTROL_DIR = "/tmp/coverage_control.d"
DATA_SOCK = "/tmp/coverage_data.sock"
# keploy names its run's own dir (a run-<id> dir it made, 1777): the agent
# binds only there. With none named, the default dir (keploy ignores it).
_ENV_DIR = os.getenv("KEPLOY_COVERAGE_CONTROL_DIR") or ""
CONTROL_DIR = _ENV_DIR or CONTROL_DIR
CONTROL_SOCK = ""                      # this process's socket, once bound

SDK_VERSION = "0.2.0"

APP_ROOT = os.path.realpath(os.getenv("KEPLOY_APP_SOURCE_DIR") or os.getcwd())
# The first process of the app (no agent above it) reads the build at its
# start; the processes below it read it at their first START, and every one
# keys files from the same root.
_FIRST = "KEPLOY_AGENT_FIRST_PID" not in os.environ
AGENT_DIR = os.path.dirname(os.path.realpath(__file__))
# Started by the sitecustomize hook (see _boot), not by the app's import:
# said in the ready line.
BOOT = bool(getattr(sys, "_keploy_agent_boot", False))


# END waits for the window's lines to settle: until no new line was noted for
# SETTLE_STEP, for at most SETTLE_MAX. Lines still coming then are a window
# cut short (windowCut): keploy keeps the test and does not use its lines.
SETTLE_STEP = 0.002
SETTLE_MAX = 0.1
# END waits this long, at most, for the test's own work still running (a
# yield-dependency's teardown, a request_finished receiver) before it judges.
TAIL_MAX = 0.05

log = logging.getLogger("keploy_agent")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[Keploy Agent] %(asctime)s pid %(process)d - %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    # The app's own logging setup is not this agent's to change.
    log.propagate = False

# The agent's answers on the control socket: one line for every command it
# reads, written before it closes the connection.
REPLY_TAKEN = b"ACK proto=2\n"
REPLY_IGNORED = b"IGNORED proto=2\n"

# ------------------------------------------------ agent state
lock = _allocate_lock()
current_id = None                      # the test whose window is open
_EXPLICIT_ROOT = bool(os.getenv("KEPLOY_APP_SOURCE_DIR"))
_root_refused = _files.refused_root(APP_ROOT, _EXPLICIT_ROOT)
_app = _files.AppFiles(APP_ROOT, AGENT_DIR,
                       _files.excludes_from_env(os.getenv("KEPLOY_COVERAGE_EXCLUDE")),
                       explicit=_EXPLICIT_ROOT)
_collector = None                      # set once the control socket is bound
_unmeasured = ""                       # why there is no collector
_owner_pid = None                      # the process that bound CONTROL_SOCK
_own_ident = None                      # the socket file it bound
_srv = None
_startup = None                        # this process's STARTUP lines, once taken
_startup_sent: set = set()             # the data sockets it went to
_window_why: list = []                 # why the open window is partial (a fork in it)
_last_fork = 0.0                       # monotonic time of this process's last fork
_snap = None                           # what START saw: threads, tasks, scheduled callbacks
_stats: dict = {}                      # each file's (size, mtime_ns, ino) at the build


def _process_start_ns() -> int:
    """When this process started, on the kernel's clock (a file's mtime is
    on it too; the app's own clock may be set back by keploy's time
    freeze): from /proc. A forked child keeps its parent's (its code is the
    parent's import). With no /proc, never (no file counts as edited)."""
    try:
        with open("/proc/self/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime"))
        # btime is whole seconds (the start reads up to 1 s early): a second
        # more, so a file written just before the start is not "edited".
        return (btime + 1) * 1_000_000_000 + ticks * 1_000_000_000 // os.sysconf("SC_CLK_TCK")
    except Exception:
        return 1 << 62


_T0_NS = _process_start_ns()
_rehashed: dict = {}                   # files re-read since: {path: (stat, sha256)}

_index_lock = _allocate_lock()
_index = None                          # (hashes, build, why), once computed
_index_ready = _Flag()
_data_ident = None                     # the data socket the INDEX went to
_table_lock = _allocate_lock()
_table_busy = False

# How long a send of the INDEX or STARTUP may take, and how long END waits
# for a build still being read.
INDEX_WAIT = 2.0


class _NotTaken(Exception):
    """A command read and not taken: the message is why."""


# ------------------------------------------------ the build and its INDEX
def _build():
    """The app files' hashes and build id, computed once: the table keploy
    asks for later is of these bytes. why says why there is no build."""
    global _index
    with _index_lock:
        if _index is None:
            try:
                paths = _app.walk()
                for f in paths:
                    try:
                        st = os.stat(os.path.join(APP_ROOT, f))
                        _stats[f] = (st.st_size, st.st_mtime_ns, st.st_ino)
                    except OSError:
                        pass
                hashes = _table.file_hashes(APP_ROOT, paths)
                build = _table.build_id(hashes, SDK_VERSION)
                why = "" if build else f"no Python file of the app under {APP_ROOT}"
            except Exception as e:  # any failure is a build with a reason, kept
                hashes, build, why = {}, "", str(e)
            _index = (hashes, build, why)
            _index_ready.set()
        return _index


def _index_record() -> dict:
    hashes, build, why = _build()
    return {
        "kind": "index", "v": 1, "language": "python", "build": build,
        "source": "root" if build else "none",
        "launch": (why or " ".join(sys.argv))[:512],
        "roots": [APP_ROOT], "rootCount": 1, "packages": [],
        "classes": len(hashes), "includes": "", "jacoco": "",
        "sdkVersion": SDK_VERSION, "jacocoVersion": "",
        "producer": _collector.name if _collector else "",
        "producerVersion": platform.python_version(),
    }


def _ident(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_ctime_ns)


def _send(payload: bytes) -> None:
    with _Socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(DATA_SOCK)
        s.sendall(payload)


def _send_index(ident) -> None:
    global _data_ident
    try:
        _send(json.dumps(_index_record(), separators=(",", ":")).encode())
    except Exception as e:
        # Sent again at the next START.
        if _data_ident == ident:
            _data_ident = None
        log.warning(f"could not send the INDEX record to {DATA_SOCK}: {e}")


def _start_build() -> None:
    if not _index_ready.is_set():
        _spawn(_build)


def _send_new_receiver(ident) -> None:
    """The INDEX and this process's STARTUP, to a data socket that has not
    had them (a new receiver knows no build, and keeps a startup bucket of
    its own)."""
    _send_index(ident)
    if _startup is not None and ident not in _startup_sent:
        _send_startup(ident)


def _index_if_new() -> None:
    """On a START: when the data socket is not the one the INDEX last went
    to, send the INDEX and the STARTUP, at once when the build is ready (so
    keploy asks for the table at the next START), else on a thread of their
    own: a START never waits for the build."""
    global _data_ident
    ident = _ident(DATA_SOCK)
    if ident is None:
        return
    if ident == _data_ident:
        if _startup is not None and ident not in _startup_sent and _index_ready.is_set():
            _send_startup(ident)
        return
    _data_ident = ident
    if _index_ready.is_set():
        _send_new_receiver(ident)
    else:
        _start_build()

        def later():
            _index_ready.wait(60)
            _send_new_receiver(ident)

        _spawn(later)


def _stream_table(build: str, hashes: dict) -> None:
    global _table_busy
    try:
        t0 = time.monotonic()
        units = _table.units(APP_ROOT, hashes)
        payload = _table.stream(units)
        _table.send_table(DATA_SOCK, build, payload)
        log.info(f"sent the build table of {build}: {len(units)} file(s), "
                 f"{len(payload)} bytes, in {time.monotonic() - t0:.2f}s")
    except Exception as e:
        log.warning(f"could not send the build table: {e}")
    finally:
        with _table_lock:
            _table_busy = False


# ------------------------------------------------ this process
def _cmdline() -> list:
    """This process's command line: sys.orig_argv, else /proc (3.9, whose
    sys.argv lacks a -c command's text)."""
    argv = list(getattr(sys, "orig_argv", None) or [])
    if argv:
        return argv
    try:
        with open("/proc/self/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return list(getattr(sys, "argv", None) or [])


def _stdlib_helper() -> str:
    """The stdlib helper this process is (multiprocessing's resource tracker,
    forkserver or semaphore tracker), "" for any other. The hook loads the
    agent in every Python process, and a helper never serves the app: one
    that took the socket left the server unmeasured. multiprocessing's
    spawn_main is the server itself (uvicorn --reload's child), never left
    out."""
    argv = _cmdline()
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            code = argv[i + 1]
            for helper in ("multiprocessing.resource_tracker", "multiprocessing.forkserver",
                           "multiprocessing.semaphore_tracker"):
                if f"from {helper} import main" in code:
                    return helper
            return ""
    return ""


# ------------------------------------------------ the window
def _settle() -> bool:
    """Wait until no new line comes for SETTLE_STEP, at most SETTLE_MAX;
    True when the lines settled."""
    deadline = time.monotonic() + SETTLE_MAX
    n = _collector.count()
    while True:
        _sleep(SETTLE_STEP)
        m = _collector.count()
        if m == n:
            return True
        if time.monotonic() >= deadline:
            return False
        n = m


def _file_hashes(paths):
    """({path: unit hash}, [why]) for the files a record names: the sha256
    of the bytes this process runs, read when the build was named. A file
    whose stat changed since is read again, and the record is partial; one
    that cannot be read is left out, and the record is partial."""
    hashes = _build()[0] if _index_ready.is_set() else {}
    out, why = {}, []
    for p in paths:
        full = os.path.join(APP_ROOT, p)
        try:
            st = os.stat(full)
        except OSError as e:
            why.append(f"{p} could not be read: {e.strerror}")
            continue
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
        h = hashes.get(p)
        if h is not None and _stats.get(p) == key:
            out[p] = h
            if st.st_mtime_ns > _T0_NS:
                why.append(f"{p} was edited after this process started")
            continue
        got = _rehashed.get(p)
        if got is None or got[0] != key:
            try:
                with open(full, "rb") as f:
                    got = (key, hashlib.sha256(f.read()).hexdigest())
            except OSError as e:
                why.append(f"{p} could not be read: {e.strerror}")
                continue
            _rehashed[p] = got
        out[p] = got[1]
        if h is not None:
            why.append(f"{p} changed after this process's build was read")
        elif st.st_mtime_ns > _T0_NS:
            # Edited after the process started, before its build was read:
            # the bytes it runs may be the ones from before.
            why.append(f"{p} was edited after this process started")
    return out, why


class _Odd(Exception):
    """An object of the app's that could not be read."""


def _app_code(fn) -> bool:
    """Whether a callable is the app's own code (a thread's or Timer's
    target, a scheduled callback): a wrapper's __wrapped__ first (tenacity
    and the like), then partial's func, a method's __func__, its __code__.
    An object that raises when read is _Odd."""
    try:
        for _ in range(6):
            if fn is None:
                return False
            inner = getattr(fn, "__wrapped__", None) or getattr(fn, "func", None) or \
                getattr(fn, "__func__", None)
            if inner is not None and inner is not fn:
                fn = inner
                continue
            code = getattr(fn, "__code__", None)
            if code is None:
                code = getattr(getattr(getattr(fn, "__call__", None), "__func__", None), "__code__", None)
            return code is not None and _app.rel(code.co_filename) is not None
        return False
    except Exception as e:
        raise _Odd(f"{type(e).__name__}: {e}"[:120]) from e


def _is_app(fn) -> bool:
    """_app_code for the hooks: an object that cannot be read counts as the
    app's (it is then kept: fail closed)."""
    try:
        return _app_code(fn)
    except _Odd:
        return True


def _app_coroutine(coro) -> str:
    """The name of the first app coroutine in a task's await chain (the
    task's own, or one it awaits: a Starlette background task awaits the
    app's function under the server's), or ""."""
    for _ in range(64):
        if coro is None:
            return ""
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None) or \
            getattr(coro, "ag_code", None)
        if code is not None and _app.rel(code.co_filename) is not None:
            return code.co_name
        nxt = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None) or \
            getattr(coro, "ag_await", None)
        if nxt is None and type(coro).__name__ in ("async_generator_asend", "async_generator_athrow"):
            # An async generator's step (an asynccontextmanager's exit, a
            # yield dependency's teardown): its generator is not an
            # attribute, only a referent.
            nxt = next((r for r in gc.get_referents(coro) if inspect.isasyncgen(r)), None)
        coro = nxt
    return ""


def _app_frames(frame) -> list:
    out = []
    while frame is not None:
        if _app.rel(frame.f_code.co_filename) is not None:
            out.append(frame)
        frame = frame.f_back
    return out


def _read(fn):
    """Another thread's state, read defensively: it may change meanwhile (a
    RuntimeError: read again). A read that fails is _Odd: never "empty"."""
    err = None
    for _ in range(3):
        try:
            return fn()
        except RuntimeError as e:
            err = e
            continue
        except Exception as e:
            err = e
            break
    raise _Odd(f"{type(err).__name__}: {err}"[:120])


def _loops():
    out = []
    for key, ref in _read(lambda: list(_collector.loops.items())):
        loop = ref()
        if loop is None:
            _collector.loops.pop(key, None)
        elif not loop.is_closed():
            out.append(loop)
    return out


def _tasks(loop):
    aio = sys.modules.get("asyncio")
    return _read(lambda: [t for t in aio.all_tasks(loop) if not t.done()]) if aio else []


def _scheduled(loop):
    # uvloop keeps no _scheduled: its handles are not seen (README).
    # The timers, and the callbacks ready and not run yet (call_soon).
    return _read(lambda: [h for h in list(getattr(loop, "_scheduled", [])) + list(getattr(loop, "_ready", []))
                          if not h.cancelled()])


def _rid_of_context(ctx):
    if ctx is None:
        return None
    return _read(lambda: ctx.get(_measure.REQUEST))


def _untraced_threads(me) -> list:
    """Before 3.12: threads running Python that keploy's tracer cannot
    reach (a thread a C extension made, or one started before the agent),
    with app code on their stack: their lines go to no record."""
    if not isinstance(_collector, _measure.Trace):
        return []
    out = []
    local = _collector.local
    known = threading._active
    for ident, fr in sys._current_frames().items():
        if ident == me or ident in _OURS:
            continue
        app = _app_frames(fr)
        if not app:
            continue
        t = known.get(ident)
        # An app frame whose tracer is not keploy's: one that ran before the
        # tracer, or under another tracer set on this thread since.
        if t is None or isinstance(t, threading._DummyThread) or ident in _UNTRACED or \
                any(f.f_trace is not local for f in app):
            out.append(t.name if t is not None else f"thread {ident}")
    return out


def _blind_threads(me) -> dict:
    """Before 3.12: the threads keploy's tracer cannot reach (started before
    the agent, by _thread.start_new_thread or by C) that are on a Python
    frame now: {ident: (innermost frame, its f_lasti)}. One that moved
    between START and END ran Python keploy could not see."""
    if not isinstance(_collector, _measure.Trace):
        return {}
    known = threading._active
    out = {}
    for ident, fr in sys._current_frames().items():
        if ident == me or ident in _OURS:
            continue
        t = known.get(ident)
        if t is None or isinstance(t, threading._DummyThread) or ident in _UNTRACED:
            out[ident] = (fr, fr.f_lasti, t.name if t is not None else f"thread {ident}")
    return out


def _snapshot() -> dict:
    """What START sees, for END to compare: each thread's app frames (the
    launcher's <module>, a main(), a poller's loop: none of them is work the
    test started), the dummy threads (one that comes up in the window is one
    keploy cannot trace), the asyncio tasks pending (before 3.12), and,
    before 3.12, the threads keploy cannot trace: running app code, and
    where each is."""
    me = threading.get_ident()
    # The tasks pending at START: before 3.12 a task's context cannot be
    # read, so a task created in the window counts as the test's own.
    tasks, odd = weakref.WeakSet(), ""
    if sys.version_info < (3, 12):
        try:
            for loop in _loops():
                for t in _tasks(loop):
                    tasks.add(t)
        except _Odd as e:
            odd = str(e)
    # The frame objects themselves, held for the window: compared by
    # identity, never by an id a new frame could take.
    frames = {ident: _app_frames(fr) for ident, fr in sys._current_frames().items()
              if ident != me and ident not in _OURS}
    return {"frames": {k: v for k, v in frames.items() if v}, "untraced": _untraced_threads(me),
            "tasks": tasks, "odd": odd, "blind": _blind_threads(me),
            "dummies": {t for t in threading._active.values() if isinstance(t, threading._DummyThread)},
            "t0": time.monotonic()}


def _blind_ran(snap, me) -> list:
    """The threads keploy cannot trace that ran Python in the window: moved
    since START, or came up since."""
    before = snap.get("blind") or {}
    out = []
    for ident, (fr, lasti, name) in _blind_threads(me).items():
        old = before.get(ident)
        if old is None or old[0] is not fr or old[1] != lasti:
            out.append(name)
    return out


def _greenlets(rid) -> list:
    """The greenlets request rid spawned (gevent's, eventlet's) still alive:
    parked, or not started yet."""
    out = []
    for g, r in _read(lambda: list(_hooks.greenlet_request.items())):
        if r != rid or g.dead:
            continue
        fr = g.gr_frame
        app = _app_frames(fr) if fr is not None else []
        if app:
            out.append(app[-1].f_code.co_name)
        else:
            run = getattr(g, "_run", None) or getattr(g, "run", None)
            out.append(getattr(run, "__name__", None) or "greenlet")
    return out


def _uvloop_handles(rid) -> list:
    """The app callbacks request rid scheduled on uvloop (call_soon,
    call_later, call_at) not run or cancelled yet."""
    out = []
    for key, (r, h, cb) in _read(lambda: list(_hooks.pending.items())):
        if r != rid:
            continue
        if h.cancelled():
            _hooks.pending.pop(key, None)
            continue
        out.append(getattr(cb, "__name__", "?"))
    return out


def _new_app_frames(ident, fr, snap) -> list:
    """The app frames on a thread's stack that were not there at START."""
    old = snap.get("frames", {}).get(ident, ())
    return [f for f in _app_frames(fr) if not any(f is g for g in old)]


_NONE = object()


def _own_tail(rid, snap) -> list:
    """The window's request's own work still running or waiting (found by
    state, its context): its threads alive running app code (a Timer
    included; an idle pool worker runs none), its executor jobs not done,
    its asyncio tasks pending with app code, its app callbacks scheduled
    and not run; and app code running on a thread whose work keploy cannot
    attribute. Work of another context is not this test's (its lines, when
    they run in a window, are that window's foreign work)."""
    why, odd = [], []
    if snap.get("odd"):
        odd.append(snap["odd"])
    me = threading.get_ident()
    threads, tasks, handles, jobs, unknown = [], [], [], 0, []
    by_ident = {t.ident: t for t in threading.enumerate()}
    for ident, fr in sys._current_frames().items():
        if ident == me or ident in _OURS:
            continue
        # Only app frames younger than START: work the test started is.
        if not _new_app_frames(ident, fr, snap):
            continue
        t = by_ident.get(ident)
        seen = _collector.thread_request.get(ident, _NONE)
        if seen is not _NONE:
            # It ran app code in the window: for whom it last did.
            if seen == rid:
                threads.append(t.name if t is not None else f"thread {ident}")
        elif t is None or not _entries.keyed(t):
            # Started before the agent, or by a C extension: whose work it
            # runs, keploy cannot tell.
            unknown.append(t.name if t is not None else f"thread {ident}")
        elif _entries.thread_request.get(t) == rid:
            threads.append(t.name)
    for t, r in _read(lambda: list(_entries.thread_request.items())):
        # Every thread the test started that is alive (in a library's code
        # too: it may call the app's later), but a pool's worker, whose jobs
        # are judged by their futures and contexts.
        if t.ident in by_ident and r == rid and t.is_alive() and t.name not in threads and \
                not _entries.pool_worker(t):
            threads.append(t.name)
    for fut, r in _read(lambda: list(_entries.future_request.items())):
        if r == rid and not fut.done():
            jobs += 1
    jobs += _read(lambda: _hooks.pool_pending(rid))
    for loop in _loops():
        for t in _tasks(loop):
            ctx = getattr(t, "_context", None)
            if ctx is None and hasattr(t, "get_context"):
                ctx = _read(t.get_context)
            if ctx is None:
                if t in snap.get("tasks", ()):
                    continue        # pending at START: not this test's
            elif _rid_of_context(ctx) != rid:
                continue
            app = _app_coroutine(_read(t.get_coro))
            if app:
                tasks.append(app)
        for h in _scheduled(loop):
            if _rid_of_context(getattr(h, "_context", None)) != rid:
                continue
            try:
                if _app_code(getattr(h, "_callback", None)):
                    handles.append(getattr(getattr(h, "_callback", None), "__name__", "?"))
            except _Odd as e:
                odd.append(str(e))
    greenlets = _greenlets(rid)
    handles += _uvloop_handles(rid)
    queued = _hooks.queued(rid)
    if threads:
        why.append(f"work the test started was still running at its end (threads: {', '.join(sorted(set(threads)))[:200]})")
    if greenlets:
        why.append(f"a greenlet the test started was still waiting at its end ({', '.join(sorted(set(greenlets)))[:200]})")
    if unknown:
        why.append(f"app code was running at the test's end on a thread keploy cannot attribute "
                   f"({', '.join(sorted(set(unknown)))[:200]})")
    if jobs:
        why.append(f"executor jobs the test submitted had not finished at its end ({jobs})")
    if tasks:
        why.append(f"an asyncio task the test created was still pending at its end ({', '.join(sorted(tasks))[:200]})")
    if handles:
        why.append(f"a callback the test scheduled had not run at its end ({', '.join(sorted(handles))[:200]})")
    if queued:
        why.append(f"work the test queued had not been taken at its end ({queued} item(s))")
    if odd:
        why.append(f"could not read the process's state: {odd[0]}")
    return why


def _emit(test_id: str) -> None:
    """Send the window's lines as test_id's record, with why it is partial.
    The test's request still being served (a yield dependency's teardown
    after the response, request_finished receivers, a greenlet parked on
    I/O) and its own work still running are waited for, up to TAIL_MAX; then
    the state is judged, the lines taken and the collector reset in one step
    (no line between them is lost), then the slow parts."""
    global _snap
    settled = _settle()
    snap = _snap or {"frames": {}, "untraced": [], "dummies": set(), "tasks": set(), "t0": time.monotonic()}
    rid = _collector.window_request
    deadline = time.monotonic() + TAIL_MAX
    while True:
        try:
            tail = _own_tail(rid, snap) if rid is not None else []
        except Exception as e:
            tail = [f"could not read the process's state: {type(e).__name__}: {e}"[:160]]
        if rid is not None and rid in _entries.live:
            # Its entry has not returned: the request is still being served.
            tail.insert(0, "the test's request was still being served at its end")
        if not tail or time.monotonic() >= deadline:
            break
        _sleep(SETTLE_STEP)
    if tail:
        settled = _settle() and settled
    untraced = _untraced_threads(threading.get_ident()) + _blind_ran(snap, threading.get_ident())
    # Before 3.12: a thread made outside threading (a C extension's) that
    # came up running Python in the window is one the tracer never reached.
    dummies = [t.name for t in list(threading._active.values())
               if isinstance(t, threading._DummyThread) and t not in snap["dummies"]
               and t.ident not in _OURS] if isinstance(_collector, _measure.Trace) else []
    # The time until the next START: app code that runs in it is outside
    # any window (its lines go in the next START's between record).
    hits, reqs, foreign, failure = _collector.swap()
    data = _collector.take(hits)
    foreign = sorted(foreign)
    requests = reqs
    _snap = None
    partial = list(_window_why)
    if _last_fork >= snap["t0"] and not any("forked" in w for w in partial):
        partial.append("a process forked during the test")
    if requests > 1:
        partial.append(f"another request was served during the test ({requests} requests)")
    if data and requests == 0:
        if _entries.seen_entry[0]:
            partial.append("app code ran in the test outside any request (no request came into this "
                           "process in the test: a poller, a scheduler, or a queue's consumer)")
        else:
            partial.append("app code ran in the test in a process that served no request keploy sees "
                           "(a process pool's worker running the test's work, or a server keploy "
                           "does not hook: see the README's servers)")
    if foreign:
        partial.append(f"app code that is not the test's ran during it ({', '.join(foreign)[:200]})")
    partial += tail
    if failure:
        partial.append(f"keploy's line callback failed in {failure[0]} ({failure[1]})")
    blind = sorted(set(snap["untraced"] + untraced + dummies))
    if blind:
        partial.append(f"a thread keploy cannot trace ran Python during the test ({', '.join(blind)[:200]})")
    hashes, why = _file_hashes(data)
    partial += why
    # requests: the request entries this process saw in the window (keploy
    # sums them over the app's processes: over 1 is a partial test).
    record = {"id": test_id, "executedLinesByFile": data, "pid": os.getpid(),
              "build": _index[1] if _index_ready.is_set() and _index else "", "fileHashes": hashes,
              "requests": requests}
    if partial:
        record["partial"] = "; ".join(partial)
    if not settled:
        record["windowCut"] = True
    _send(json.dumps(record, separators=(",", ":")).encode())
    log.debug(f"[{test_id}] sent {len(data)} file(s) / "
              f"{sum(len(v) for v in data.values())} line(s)"
              + (f"; partial: {record['partial']}" if partial else "")
              + ("; its lines were still coming (windowCut)" if not settled else ""))


def _send_between(lines) -> None:
    """The lines app code ran between the last test's END and this START:
    keploy's outside-tests bucket, never a test's."""
    if not lines:
        return
    hashes, why = _file_hashes(lines)
    rec = {"kind": "startup", "v": 1, "pid": os.getpid(), "build": _index[1] if _index_ready.is_set() and _index else "",
           "executedLinesByFile": lines, "fileHashes": hashes, "between": True}
    if why:
        rec["partial"] = "; ".join(why)
    try:
        _send(json.dumps(rec, separators=(",", ":")).encode())
    except OSError as e:
        log.debug(f"could not send the lines between tests: {e}")


def _send_startup(ident) -> None:
    """The lines this process ran before its first window (import time, the
    app's start), to each data socket (receiver) once: keploy credits them
    to no test."""
    if _startup is None:
        return
    hashes, why = _file_hashes(_startup)
    rec = {"kind": "startup", "v": 1, "pid": os.getpid(), "build": _build()[1],
           "executedLinesByFile": _startup, "fileHashes": hashes}
    if why:
        rec["partial"] = "; ".join(why)
    try:
        with _Socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(INDEX_WAIT)
            s.connect(DATA_SOCK)
            s.sendall(json.dumps(rec, separators=(",", ":")).encode())
        _startup_sent.add(ident)
    except OSError as e:
        log.debug(f"could not send the STARTUP record: {e}")


# ------------------------------------------------ per-connection handler
def _run(cmd: str) -> bool:
    """Run one command; True when taken. Raises _NotTaken (or anything)
    when it was not, with why."""
    global current_id, _table_busy, _startup, _snap
    action, sep, arg = cmd.partition(" ")
    if action == "TABLE" and not sep:
        if _collector is None:
            raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
        if not _index_ready.is_set():
            _start_build()
            raise _NotTaken("the build is still being read")
        _, build, why = _build()
        if not build:
            raise _NotTaken(f"no build table: {why}")
        with _table_lock:
            if _table_busy:
                raise _NotTaken("a build table is on its way already")
            _table_busy = True
        try:
            _spawn(_stream_table, build, _build()[0])
        except BaseException:
            with _table_lock:
                _table_busy = False
            raise
        return True
    if action not in ("START", "END"):
        raise _NotTaken(f"unknown command {action[:20]!r}")
    test_id = arg
    if not test_id:
        raise _NotTaken(f"{action} names no test")

    if _collector is not None:
        failure = _collector.take_failure()
        if failure:
            log.warning(f"keploy's line callback failed in {failure[0]} ({failure[1]}): "
                        "those locations are no longer measured")
    with lock:
        if action == "START":
            if _collector is None:
                _index_if_new()
                raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
            replaced = _collector.replaced()
            if replaced:
                current_id = None
                raise _NotTaken(replaced)
            log.debug(f"START {test_id}")
            if current_id is not None and current_id != test_id:
                log.warning(f"START {test_id} while {current_id} had no END: "
                            f"{current_id}'s lines are dropped")
            try:
                # First what the window starts from, then the sends: no
                # line run meanwhile is lost, and no send holds the window.
                # Entries of modules a finder could not see (patched now,
                # before this test's request comes).
                for name in _entries.ENTRIES:
                    mod = sys.modules.get(name)
                    if mod is not None:
                        _entries.patch_module(mod)
                _snap = _snapshot()
                between = None
                # Taken and replaced in one step: no line is lost between.
                hits = _collector.swap(open_window=True)[0]
                if _startup is None:
                    # The lines before the first window are the process's
                    # start, not another test's.
                    _startup = _collector.take(hits)
                    _start_build()
                else:
                    between = _collector.take(hits)
            except Exception as e:
                # Lines not reset would hold the tests before this one: the
                # START is not taken, and its END is ignored.
                current_id = None
                log.error(f"[{test_id}] could not reset the lines: {e}", exc_info=True)
                raise _NotTaken("could not reset the lines") from e
            _window_why.clear()
            current_id = test_id
            _index_if_new()
            if between:
                _send_between(between)
            return True

        if _collector is None:
            raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
        log.debug(f"END   {test_id}")
        if test_id != current_id:
            log.warning(f"END {test_id} does not match the test started "
                        f"({current_id}): no coverage sent")
            raise _NotTaken(f"no window of {test_id[:60]} is open")
        try:
            replaced = _collector.replaced()
            if replaced:
                raise _NotTaken(replaced)
            _emit(test_id)
        except _NotTaken:
            raise
        except Exception as e:
            log.error(f"[{test_id}] could not send the coverage: {e}", exc_info=True)
            raise _NotTaken("could not send the coverage") from e
        finally:
            current_id = None
        return True


_open_conns: set = set()               # connections being served (closed in a forked child)


def _handle(conn: socket.socket):
    """Read one command, run it, and answer it before the close."""
    _open_conns.add(conn)
    if conn.gettimeout() is None:
        conn.settimeout(60)
    fp = conn.makefile("r", errors="replace")
    try:
        try:
            raw = fp.readline(4096)
        except OSError as e:
            log.warning(f"Could not read a command: {e}")
            return
        if not raw:
            return                          # closed with no command
        cmd = raw.strip()
        reply = REPLY_IGNORED
        try:
            if _run(cmd):
                reply = REPLY_TAKEN
        except _NotTaken as e:
            reply = REPLY_IGNORED[:-1] + b" " + str(e).replace("\n", " ").encode("utf-8", "replace") + b"\n"
        except Exception as e:
            log.error(f"Handler error: {e}", exc_info=True)
        try:
            conn.sendall(reply)
        except OSError as e:
            log.warning(f"Could not answer {cmd!r}: {e}")
    finally:
        try:
            fp.close()
            conn.close()
        except Exception:
            pass
        _open_conns.discard(conn)


# ------------------------------------------------ control server
class _Unsafe(Exception):
    """The control dir is not one this process may trust."""


def _pidns() -> str:
    """This process's pid namespace, as its inode ("" when it cannot be
    read): pids repeat across containers that share /tmp, namespaces don't."""
    try:
        link = os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""
    return link[link.find("[") + 1:link.rfind("]")] if "[" in link else ""


def _make_dir(d: str) -> None:
    """The sockets' dir: made 1777 (as /tmp: an app's processes may run as
    several users) without a moment at another mode, by a rename into place;
    and, made or found, a real dir owned by root or this user, with the
    sticky bit, or refused (another user's dir could swap a socket for a
    link)."""
    import stat
    import tempfile
    if _ENV_DIR and d == _ENV_DIR:
        # The run's dir is keploy's to make: never made here.
        if not os.path.lexists(d):
            raise _Unsafe(f"{d} (KEPLOY_COVERAGE_CONTROL_DIR) does not exist")
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise _Unsafe(f"{d} is not a directory")
        if st.st_uid not in (0, os.geteuid()):
            raise _Unsafe(f"{d} is owned by uid {st.st_uid}, neither root nor this user")
        if (st.st_mode & 0o7777) != 0o1777:
            raise _Unsafe(f"{d} is not mode 1777 (world-writable with the sticky bit): "
                          f"{oct(st.st_mode & 0o7777)}")
        return
    if not os.path.lexists(d):
        tmp = tempfile.mkdtemp(prefix=".kcd-", dir=os.path.dirname(d) or ".")
        try:
            os.chmod(tmp, 0o1777)
            os.rename(tmp, d)
        except OSError:
            os.rmdir(tmp)          # another process made it first
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise _Unsafe(f"{d} is not a directory")
    if st.st_uid not in (0, os.geteuid()):
        raise _Unsafe(f"{d} is owned by uid {st.st_uid}, neither root nor this user")
    if not st.st_mode & stat.S_ISVTX or (st.st_mode & 0o777) != 0o777:
        raise _Unsafe(f"{d} is not world-writable with the sticky bit (mode {oct(st.st_mode & 0o7777)})")


def _live(path: str) -> bool:
    probe = _Socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(path)
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True                # too busy to accept: live
    finally:
        probe.close()


def _sock_path() -> str:
    """<pid namespace inode>-<pid>.sock, unique across containers that
    share the dir; <pid>-<random>.sock when the namespace cannot be read or
    a live socket has the name."""
    ns = _pidns()
    path = os.path.join(CONTROL_DIR, f"{ns}-{os.getpid()}.sock" if ns else "")
    if ns and not (os.path.lexists(path) and _live(path)):
        return path
    return os.path.join(CONTROL_DIR, f"{os.getpid()}-{os.urandom(4).hex()}.sock")


def _bind(path: str) -> socket.socket:
    """Bind and listen on this process's socket, then give it its name:
    bound at a temp name (.<name>.tmp, which keploy never reads), listening,
    then renamed to path (over a dead process's file of that name), so a
    *.sock file in the dir always listens, or is dead. Mode 0666 by a chmod
    of the temp file in the checked dir (the umask is the app's)."""
    d = os.path.dirname(path)
    _make_dir(d)
    tmp = os.path.join(d, "." + os.path.basename(path) + ".tmp")
    if os.path.lexists(tmp):
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
    srv = _Socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(tmp)
        # In the checked sticky dir no other user can swap the temp file:
        # chmod it, rather than set the process-wide umask from a thread.
        os.chmod(tmp, 0o666)
        srv.listen()
        os.rename(tmp, path)
    except BaseException:
        srv.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return srv


def _server(srv: socket.socket):
    try:
        while True:
            try:
                # The real socket class (socket.accept() would make the
                # patched one, a greenlet socket, under gevent).
                fd, _ = srv._accept()
                c = _Socket(srv.family, srv.type, srv.proto, fileno=fd)
            except OSError:
                return                      # closed: forked away
            _spawn(_handle, c)
    finally:
        srv.close()


def _at_exit() -> None:
    """At exit: stop measuring, and remove this process's socket file if it
    bound it and it is still the one it bound (a forked child that exits
    leaves its parent's), unless this process ran app lines no record
    carried (it never took a START, or ran some after its last one): then
    the file stays, keploy sees a socket come at the test's END, so the test
    is not verified, and removes it as stale before the next test."""
    c = _collector
    unsent = False
    if c is not None:
        unsent = c.count() > 0
        try:
            c.stop()
        except Exception:
            pass
    if unsent:
        return
    if os.getpid() == _owner_pid and _own_ident is not None and _ident(CONTROL_SOCK) == _own_ident:
        try:
            os.remove(CONTROL_SOCK)
        except OSError:
            pass


def _serve() -> bool:
    """Bind this process's socket and serve it on a thread; False when it
    cannot be."""
    global _srv, _owner_pid, _own_ident, CONTROL_SOCK
    try:
        path = _sock_path()
        try:
            srv = _bind(path)
        except OSError:
            # Another user's stale file of the name, and the like: once
            # more, under a name no one has.
            path = os.path.join(CONTROL_DIR, f"{os.getpid()}-{os.urandom(4).hex()}.sock")
            srv = _bind(path)
    except _Unsafe as e:
        _say_unmeasured(f"Keploy does not trust its control dir: {e}")
        return False
    except OSError as e:
        _say_unmeasured(f"Could not start the Keploy control server in {CONTROL_DIR}: {e}")
        return False
    _srv, CONTROL_SOCK = srv, path
    _owner_pid, _own_ident = os.getpid(), _ident(path)
    if not _ATEXIT[0]:
        _ATEXIT[0] = True
        atexit.register(_at_exit)
    _spawn(_server, srv)
    log.info(f"Keploy control server at {path}")
    return True


def _say_unmeasured(why: str) -> None:
    """A process keploy cannot reach is said in its log, and to keploy: an
    {"kind":"unmeasured"} record to each data socket that comes, so keploy
    knows a process of the app runs unmeasured."""
    log.error(f"{why}; this process (pid {os.getpid()}) is not measured")
    rec = json.dumps({"kind": "unmeasured", "v": 1, "pid": os.getpid(), "why": why[:300],
                      "dir": CONTROL_DIR}, separators=(",", ":")).encode()
    # At once, to the receiver there now (a test can START and END within a
    # second of it); then to each new one, from a thread.
    first = _ident(DATA_SOCK)
    if first is not None:
        try:
            _send(rec)
        except OSError:
            first = None

    def tell():
        sent = first
        while True:
            ident = _ident(DATA_SOCK)
            if ident is not None and ident != sent:
                try:
                    _send(rec)
                    sent = ident
                except OSError:
                    pass
            _sleep(0.1)

    _spawn(tell)


def _children_of_this_thread():
    try:
        with open(f"/proc/self/task/{threading.get_native_id()}/children") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def _exec_fork() -> bool:
    """Whether this fork is subprocess's (preexec_fn): the child execs at
    once, and any Python it becomes binds its own socket."""
    try:
        f = sys._getframe(2)
        return f.f_code.co_name == "_execute_child" and f.f_code.co_filename.endswith("subprocess.py")
    except ValueError:
        return False


def _forked_parent():
    """A fork in this process: one during a window makes it partial (the
    child's lines are not in this record, and its socket comes too late for
    this test's START)."""
    global _last_fork
    if _exec_fork():
        return
    _last_fork = time.monotonic()
    if current_id is not None:
        kids = _children_of_this_thread()
        who = f" (pid {kids[-1]})" if kids else ""
        _window_why.append(f"a process forked during the test{who}")


# uWSGI (its module, when this is one of its processes) and the app's own
# post_fork_hook it had (chained).
_UWSGI: list = [None, None]


def _uwsgi_module():
    """uWSGI's module, when this process is one of uWSGI's (its Python is
    uWSGI's embedded one: the module is built in), else None."""
    m = sys.modules.get("uwsgi")
    if m is not None:
        return m
    try:
        if "uwsgi" not in os.path.basename(os.readlink("/proc/self/exe")):
            return None
        import uwsgi as m      # noqa: F811 (only inside uWSGI)
        return m
    except Exception:
        return None


def _uwsgi_post_fork():
    """uWSGI's post_fork_hook, in each worker (and mule, spooler) it starts:
    the fork handling. Under --py-call-uwsgi-fork-hooks Python's at-fork
    handlers ran it already."""
    try:
        if not (_owner_pid == os.getpid() and _srv is not None):
            _uwsgi_worker()
    finally:
        prev = _UWSGI[1]
        if prev is not None:
            prev()


def _uwsgi_worker():
    """A process of uWSGI's that serves (a worker): its own socket and
    state, keeping the lines its master's import ran (its startup). With no
    threads (an older uWSGI without --enable-threads) keploy's control
    thread cannot run: refused, at once."""
    global _collector, _unmeasured
    uw = _UWSGI[0]
    if _collector is not None:
        _collector.pid = os.getpid()
    if not getattr(uw, "has_threads", 1):
        _unmeasured = ("uWSGI without threads (--enable-threads) is not supported yet: "
                       "keploy's control thread cannot run in its processes")
        _collector = None
        _say_unmeasured(_unmeasured)
        return
    _forked(keep_lines=True)


def _c_forked():
    """A process forked from C with none of Python's after-fork work done
    (uWSGI's workers; mod_wsgi's), found at its first app line. Alone on its
    one thread, it does that work now (PyOS_AfterFork_Child: the
    interpreter's locks and thread states, then the at-fork handlers, keploy's
    _forked among them, which binds its own socket): its socket comes while
    keploy may be inside a test (that test is then unverified), and the
    tests after are measured. With other threads already running, that work
    cannot be done safely: the process says it is unmeasured and binds
    nothing (a socket no thread can serve would hold every test)."""
    global _collector, _unmeasured
    if _UWSGI[0] is not None:
        # uWSGI's worker whose post_fork_hook was replaced (uwsgidecorators
        # imported after the agent): uWSGI's own fork work is done.
        _uwsgi_worker()
        return
    try:
        threads = len(os.listdir("/proc/self/task"))
    except OSError:
        threads = 0
    try:
        if threads == 1:
            import ctypes
            # As a fork Python made: its before-fork step (it takes the
            # interpreter's locks, which 3.13+ checks are held), then its
            # after-fork step in the child (which re-makes them).
            ctypes.pythonapi.PyOS_BeforeFork()
            ctypes.pythonapi.PyOS_AfterFork_Child()
            return
    except Exception as e:
        log.debug(f"keploy could not redo Python's after-fork work: {e!r}")
    _unmeasured = (f"this process (pid {os.getpid()}) was forked without Python's fork handlers "
                   + (f"while {threads} threads ran" if threads > 1 else "and they cannot be run now"))
    _collector = None
    _say_unmeasured(_unmeasured)


def _forked(keep_lines: bool = False):
    """A forked child (a prefork server's worker) is a process of its own:
    it re-makes every lock (one held at the fork has no owner here), closes
    its copies of its parent's listening socket and of the connections the
    parent was serving, starts a fresh window state (no window, no lines,
    its own STARTUP), reads the build again if the parent had not, and binds
    its own socket at once (a fork subprocess makes to exec binds none)."""
    global current_id, _startup, _data_ident, _srv, _owner_pid, _own_ident, _snap
    global lock, _index_lock, _table_lock, _table_busy, _index_ready, _last_fork
    if _collector is not None:
        _collector.pid = os.getpid()
    if _exec_fork():
        return
    lock, _index_lock, _table_lock, _table_busy = (_allocate_lock(), _allocate_lock(),
                                                   _allocate_lock(), False)
    # Always new: a fork between the build's _index and its set() left one
    # that is never set.
    _index_ready = _Flag()
    if _index is not None:
        _index_ready.set()
    # The fds themselves: a served connection's makefile holds a reference,
    # so socket.close() alone would leave the fd open, and the parent's
    # answer's close held back while this child lives.
    for sock in ([_srv] if _srv is not None else []) + list(_open_conns):
        try:
            os.close(sock.detach())
        except Exception:
            pass
    _open_conns.clear()
    _srv, _owner_pid, _own_ident = None, None, None
    current_id, _startup, _data_ident, _snap = None, None, None, None
    _startup_sent.clear()
    _window_why.clear()
    _last_fork = 0.0
    _OURS.clear()
    _UNTRACED.clear()
    if _collector is not None and not keep_lines:
        try:
            _collector.reset()
        except Exception:
            pass
    # A forkserver's child is the app's worker: it binds, at once (it never
    # execs, and one that lives only inside a window must leave its socket
    # file, so keploy sees it came).
    if _root_refused:
        return
    # The build, if the parent had not read it: at the first START, not in
    # every fork child (a prefork server's).
    # At once, in the fork itself: a child that lives only a moment leaves
    # its socket file (keploy sees it came), and no START can come before
    # the child's socket is there. A fork subprocess makes to exec is not
    # here (_exec_fork).
    _serve()


def _invoked_for_pythonpath() -> bool:
    """`python -m keploy_agent --pythonpath` only prints a path."""
    argv = list(getattr(sys, "orig_argv", None) or [])
    for i, a in enumerate(argv):
        if a == "-m" and argv[i + 1:i + 3] == ["keploy_agent", "--pythonpath"]:
            return True
        if a == "-mkeploy_agent" and argv[i + 1:i + 2] == ["--pythonpath"]:
            return True
    if argv:
        return False
    # 3.9 has no orig_argv: while -m imports the package, argv[0] is "-m".
    argv = getattr(sys, "argv", None) or []
    return argv[:2] == ["-m", "--pythonpath"]


# ------------------------------------------------ start
_ATEXIT = [False]


# The control socket is bound here, before this import returns, so it is
# there before the app can say it is ready: bound on a thread, the bind could
# come after the app's port, and the first test's START then found no one
# listening and its coverage was lost. Commands are served on a thread. A
# socket that cannot be bound is said, and the app runs on without coverage.
def _start():
    global _collector, _unmeasured
    main_interp = getattr(_thread, "_is_main_interpreter", None)
    if main_interp is not None and not main_interp():
        # A subinterpreter (3.12+): its own Python is not the app's process,
        # and isolated ones refuse the agent's daemon threads.
        return
    helper = _stdlib_helper()
    if helper and helper != "multiprocessing.forkserver":
        log.debug(f"{helper} (pid {os.getpid()}) serves no request: not measured")
        return
    if not _root_refused:
        os.environ.setdefault("KEPLOY_APP_SOURCE_DIR", APP_ROOT)
    os.environ.setdefault("KEPLOY_AGENT_FIRST_PID", str(os.getpid()))
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        # Other threads' frames are read with no lock that stops them: under
        # the GIL that is safe, without it not tested.
        _unmeasured = "a free-threaded Python (no GIL) is not supported yet"
        log.error(f"Keploy measures nothing in this process: {_unmeasured}")
        _serve()
        return
    if _root_refused:
        _unmeasured = _root_refused
        log.error(f"Keploy measures nothing in this process: {_root_refused}")
    else:
        try:
            _collector = _measure.start(_app)
        except _measure.Unavailable as e:
            _unmeasured = str(e)
            log.error(f"Keploy cannot measure this process: {e}")
    if helper:
        # The forkserver: its children are the app's workers.
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_forked, after_in_parent=_forked_parent)
        return
    uw = _uwsgi_module()
    master = False
    if uw is not None:
        try:
            master = uw.worker_id() == 0
        except Exception:
            master = True
        if master:
            # uWSGI's master (or its process before its first worker): it
            # forks its workers from C, maybe while one of keploy's threads
            # held the GIL (a worker that would never get it). So no thread
            # and no socket here; each worker binds its own at its start.
            _UWSGI[0] = uw
            prev = getattr(uw, "post_fork_hook", None)
            if prev is not None and prev is not _uwsgi_post_fork:
                _UWSGI[1] = prev
            uw.post_fork_hook = _uwsgi_post_fork
    if not master and not _serve():
        c, _collector = _collector, None
        if c is not None:
            c.stop()
        return
    if _FIRST and not master:
        # The first process reads the build now, off the app's path, so the
        # first START's INDEX is ready; the others at their first START.
        _start_build()
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_forked, after_in_parent=_forked_parent)
    if _collector is not None:
        _hooks.is_app = _is_app
        _collector.on_fork = _c_forked
        _entries._on_start.append(lambda rid: _collector is not None and _collector.request_started(rid))
        try:
            _entries.install()
        except Exception as e:
            # Never the app's import: with its entries unhooked keploy
            # cannot tell its tests' lines, so this process is unmeasured.
            _unmeasured = f"keploy could not hook the app's request entries ({type(e).__name__}: {e})"[:300]
            c, _collector = _collector, None
            try:
                c.stop()
            except Exception:
                pass
            _say_unmeasured(_unmeasured)
            return
        if isinstance(_collector, _measure.Trace):
            me = threading.get_ident()
            _UNTRACED.update(t.ident for t in threading.enumerate() if t.ident != me)
        untraced = getattr(_collector, "untraced_threads", 0)
        if untraced:
            log.warning(f"{untraced} thread(s) started before keploy_agent was "
                        "imported are not measured (Python < 3.12): import it first")
        log.info(f"Keploy agent ready ({_collector.name}, app root {APP_ROOT}, "
                 f"{'started by the PYTHONPATH hook' if BOOT else 'imported by the app'})")


if not _invoked_for_pythonpath():
    _start()
