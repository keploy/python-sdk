"""The lines the app ran since the last window began.

On Python 3.12 and later the collector is sys.monitoring's LINE event, on a
tool id of its own: the callback notes the line when its file is an app file
and returns DISABLE, so each code location costs one call until the next
window's start re-enables them all (restart_events). Every thread, the
event loop and thread pools included, is measured.

Before 3.12 it is sys.settrace (this thread) and threading.settrace (threads
started from now on): a call into an app file gets a line tracer, any other
call none. Threads already running when it starts are not traced.
"""

from __future__ import annotations

import contextvars
import sys
import threading
import weakref

_get_ident = threading.get_ident
_get_native_id = threading.get_native_id


def _thread_name(ident) -> str:
    t = threading._active.get(ident)
    return t.name if t is not None else f"thread {ident}"


def _task_name(task) -> str:
    try:
        coro = task.get_coro()
        return f"{task.get_name()} ({getattr(coro, '__qualname__', '?')})"
    except Exception:
        return "?"

from ._files import AppFiles


class Unavailable(Exception):
    """This process cannot be measured: why is the message."""


# The request whose work a line is: set at the app's request entry (a
# framework's or server's, see _entries), and carried into the threads,
# executor jobs and asyncio tasks and callbacks the request starts (each
# copies its starter's context).
REQUEST = contextvars.ContextVar("keploy_request", default=None)

# The id an app signal handler runs with: no request's (it interrupts
# whatever runs).
SIGNAL = "signal"

# The agent's own threads: app code they run (an app __getattr__ keploy's
# state reads call) is neither the test's nor noted.
OURS: set = set()
# At most this many names of foreign work are kept per window.
FOREIGN_MAX = 32


class _Collector:
    name = ""

    def __init__(self, files: AppFiles):
        self.files = files
        self.hits: set[tuple[str, int]] = set()
        # The requests that entered the app in this window: the first (the
        # window's: keploy's replay sends one per test) and how many. Only
        # while a window is open: a process that never gets a START keeps
        # nothing per request.
        self.first = None
        self.count_requests = 0
        # The pid that measures: a process forked from C (uWSGI's, mod_wsgi's
        # workers), with no at-fork handler run, is another (on_fork).
        import os as _os
        self._getpid = _os.getpid
        self.pid = _os.getpid()
        self.on_fork = None
        # The thread the cyclic garbage collector runs on now (finalizers it
        # calls run in whatever context is current: no request's work).
        self.gc_ident = None
        import gc as _gc
        _gc.callbacks.append(self._gc)

    def _gc(self, phase, info) -> None:
        self.gc_ident = _get_ident() if phase == "start" else None
        # Who ran app code in the window that was not the window's
        # request's: {who: True}.
        self.foreign: dict = {}
        self.thread_request: dict = {}
        # A test's window is open: whose lines are noted only then (between
        # windows they go to the between bucket, whoever ran them).
        self.open = False
        # The asyncio event loops app code ran on, by id.
        self.loops: dict = {}
        # None: no error yet; (file, error): one to say; False: said.
        self._failed = None
        # The first callback error of this window, for its record (reset()
        # clears it; the log's slot above is apart).
        self.window_failure = None

    def reset(self, open_window: bool = False) -> None:
        self.swap(open_window)

    def swap(self, open_window: bool = False):
        """The window's state, taken and replaced in one step: a line run
        meanwhile is in the old set or the new one, never lost. Returns
        (hits, requests, foreign, failure)."""
        old = (self.hits, self.count_requests, self.foreign, self.window_failure)
        self.hits = set()
        self.first, self.count_requests = None, 0
        self.foreign = {}
        self.thread_request = {}
        self.window_failure = None
        self.open = open_window
        return old

    @property
    def window_request(self):
        return self.first

    def request_started(self, rid) -> None:
        if not self.open:
            return
        if self.first is None:
            self.first = rid
        self.count_requests += 1

    def note(self) -> None:
        """Whose this app line is (called at a location's first hit in the
        window): the window's request's, or something else's (no request: a
        scheduler, a poller, a task from before the window; another request:
        a probe, a Timer or callback the last test left), named by its
        asyncio task or thread. Nothing is noted while no window is open."""
        if self.on_fork is not None and self._getpid() != self.pid:
            # Forked from C: this process's own state and socket, first.
            self.pid = self._getpid()
            self.on_fork()
        # The event loops app code runs on, window or not (END looks there
        # for the test's callbacks and tasks); by reference: a new loop at a
        # dead one's address too.
        aio = sys.modules.get("asyncio")
        loop = aio._get_running_loop() if aio is not None else None
        if loop is not None:
            r = self.loops.get(id(loop))
            if r is None or r() is not loop:
                self.loops[id(loop)] = weakref.ref(loop)
        if not self.open:
            return
        me = _get_ident()
        if self.gc_ident == me:
            # A finalizer (__del__, a weakref callback) the collector ran.
            self.foreign.setdefault("the garbage collector's finalizers", True)
            return
        rid = REQUEST.get()
        if rid is SIGNAL:
            self.foreign.setdefault("a signal handler", True)
            return
        # The request each thread last ran app code for in the window: a pool
        # thread's job's, which its start context is not.
        self.thread_request[me] = rid
        if rid is not None and rid == self.first:
            return
        who = None
        if loop is not None:
            task = aio.current_task(loop)
            if task is not None:
                who = "task " + _task_name(task)
        if who is None:
            who = "thread " + _thread_name(_get_ident())
        if who in self.foreign or len(self.foreign) < FOREIGN_MAX:
            self.foreign[who] = True

    def count(self) -> int:
        return len(self.hits)

    def stop(self) -> None:
        """Stop measuring."""
        try:
            import gc as _gc
            _gc.callbacks.remove(self._gc)
        except ValueError:
            pass

    def replaced(self) -> str:
        """Why keploy's measuring was replaced by another tool, or ""."""
        return ""

    def failed(self, code, e: Exception) -> None:
        """A callback's error, kept to be said later (take_failure), outside
        the callback: logging here could fail again in the app's frame (at
        the recursion limit) and raise there."""
        try:
            f = (code.co_filename, type(e).__name__)
        except Exception:
            f = ("?", "?")
        if self.window_failure is None:
            self.window_failure = f
        if self._failed is None or self._failed is False:
            self._failed = f

    def take_failure(self):
        """The first callback error not said yet, as (file, error), or None."""
        f = self._failed
        if not f:
            return None
        self._failed = False
        return f

    def take(self, hits=None) -> dict[str, list[int]]:
        """The lines noted since the last reset (or the hits given), by
        file, sorted."""
        by_file: dict[str, list[int]] = {}
        for f, ln in set(self.hits if hits is None else hits):
            by_file.setdefault(f, []).append(ln)
        for v in by_file.values():
            v.sort()
        return by_file


class Monitoring(_Collector):
    """sys.monitoring (3.12+)."""

    name = "sys.monitoring"

    def __init__(self, files: AppFiles):
        super().__init__(files)
        mon = sys.monitoring
        self.tool = None
        # 3 and 4 first: coverage.py's sysmon core takes COVERAGE_ID (1)
        # and fails when it is taken, and it may start after this agent.
        for tool in (3, 4, mon.COVERAGE_ID):
            try:
                mon.use_tool_id(tool, "keploy")
            except ValueError:
                continue
            self.tool = tool
            break
        if self.tool is None:
            raise Unavailable("sys.monitoring tool ids 1, 3 and 4 are all in use "
                              "(another coverage tool or profiler runs in the app)")
        rel = files.rel
        disable = mon.DISABLE

        # Line 0 is no line (an empty module's entry): it is in no table.
        # An error here would be raised in the app's own frame: it is
        # swallowed, said once, and the location measured no more.
        def on_line(code, line, _rel=rel, _disable=disable, _ours=OURS, _gi=_get_ident):
            try:
                if line > 0:
                    f = _rel(code.co_filename)
                    if f is not None:
                        if _gi() in _ours:
                            # keploy's own thread: not the app's line, and
                            # the location stays live for the app.
                            return None
                        self.hits.add((f, line))
                        self.note()
            except Exception as e:
                self.failed(code, e)
            return _disable

        mon.register_callback(self.tool, mon.events.LINE, on_line)
        mon.set_events(self.tool, mon.events.LINE)

    def swap(self, open_window: bool = False):
        old = super().swap(open_window)
        # Every location a past window disabled runs its callback once more.
        sys.monitoring.restart_events()
        return old

    def stop(self) -> None:
        super().stop()
        sys.monitoring.set_events(self.tool, 0)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(self.tool)


class Trace(_Collector):
    """sys.settrace / threading.settrace (before 3.12)."""

    name = "settrace"

    def __init__(self, files: AppFiles):
        super().__init__(files)
        if sys.gettrace() is not None:
            raise Unavailable("another tracer (a debugger or a coverage tool) is "
                              "set in the app, and before Python 3.12 keploy would "
                              "replace it")
        rel = files.rel

        # The local tracer is set only on app frames, so its file is an app
        # file and rel() a cached dict hit. A location is noted (whose it is)
        # at its first hit in the window, as sys.monitoring does. An error in
        # either would be raised in the app's own frame: it is swallowed,
        # said once, and the frame traced no more.
        def local(frame, event, arg):
            try:
                if event == "line":
                    ln = frame.f_lineno
                    if ln > 0:
                        key = (rel(frame.f_code.co_filename), ln)
                        hits = self.hits
                        if key not in hits:
                            hits.add(key)
                            self.note()
                return local
            except Exception as e:
                self.failed(frame.f_code, e)
                return None

        def call(frame, event, arg, _rel=rel):
            try:
                if event == "call" and _rel(frame.f_code.co_filename) is not None:
                    return local
            except Exception as e:
                self.failed(frame.f_code, e)
            return None

        self._call = call
        self.local = local
        # Another tracer set on a running thread from Python (sys.settrace:
        # a debugger, a profiler) takes that thread from keploy's: said
        # (replaced()). One set from C (PyEval_SetTrace) is not seen.
        self.tracer_set = ""
        settrace = self._settrace = sys.settrace

        def settrace_hook(fn):
            if fn is not call and not self.tracer_set:
                self.tracer_set = f"thread {_thread_name(_get_ident())}"
            return settrace(fn)
        settrace_hook.__keploy__ = True
        settrace(call)
        threading.settrace(call)
        sys.settrace = settrace_hook
        # The importing thread's app frames (an app.py that imports the
        # agent, a manage.py's main()) were running before the tracer: each
        # gets keploy's line tracer, as pdb does, so their lines are measured
        # and the thread is no untraced one.
        fr = sys._getframe(1)
        while fr is not None:
            try:
                if rel(fr.f_code.co_filename) is not None:
                    fr.f_trace = local
            except Exception:
                pass
            fr = fr.f_back
        others = [t for t in threading.enumerate()
                  if t is not threading.current_thread() and t.is_alive()]
        self.untraced_threads = len(others)

    def replaced(self) -> str:
        """Why keploy's tracer is no longer the one new threads get; "" while
        it is. Before 3.12 a debugger, profiler or coverage tool started
        after the agent replaces it without a word. (The agent's own command
        threads are raw OS threads, never traced, so only the hook new
        threads get is read.)"""
        gt = getattr(threading, "gettrace", None)
        cur = gt() if gt is not None else getattr(threading, "_trace_hook", None)
        if cur is not self._call:
            return "another tracer replaced keploy's for the app's threads"
        if self.tracer_set:
            return (f"another tracer was set on {self.tracer_set} (sys.settrace): "
                    "its lines are no longer measured")
        return ""

    def stop(self) -> None:
        super().stop()
        if getattr(sys.settrace, "__keploy__", False):
            sys.settrace = self._settrace
        if sys.gettrace() is self._call:
            self._settrace(None)
        threading.settrace(None)


def start(files: AppFiles) -> _Collector:
    """Start measuring: sys.monitoring on 3.12+, else settrace. Raises
    Unavailable when neither can run here."""
    if sys.version_info >= (3, 12):
        return Monitoring(files)
    return Trace(files)
