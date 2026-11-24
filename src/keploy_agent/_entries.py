"""Where the app's requests come in, and how their context travels.

A request entry (a framework's or a server's) gives the request an id in the
REQUEST context variable for as long as it is served, and tells the
collector a request started. The threads, executor jobs and asyncio tasks
and callbacks a request starts copy its context, so their app lines are the
request's too. A line whose context is not the window's request is
another's (_measure.note).

Entries, framework first (they cover any server the framework runs on):
Starlette (FastAPI) Starlette.__call__ (http and websocket scopes), Flask
Flask.wsgi_app, Django WSGIHandler.__call__ and ASGIHandler.__call__; then
servers, for apps of no framework above: uvicorn RequestResponseCycle.run_asgi
(h11 and httptools: uvloop too), gunicorn's sync, gthread and async workers'
handle_request, werkzeug WSGIRequestHandler.run_wsgi, wsgiref
BaseHandler.run, waitress WSGITask.execute, gevent pywsgi
WSGIHandler.run_application. An entry inside another (a server's, then
Django's) is the same request.

Each is patched when its module is imported (a finder on sys.meta_path), or
at once when it already is.
"""

from __future__ import annotations

import concurrent.futures.thread as _cft
import contextvars
import functools
import importlib.abc
import importlib.machinery
import inspect
import itertools
import sys
import threading
import types
import weakref

from . import _hooks
from ._measure import REQUEST

# Inside an entry in this context: a nested entry is the same request.
_IN_ENTRY = contextvars.ContextVar("keploy_in_entry", default=False)
_ids = itertools.count(1)
_on_start = []          # the collector's request_started, set by the agent
# The requests being served now: an entry adds its id as it is entered and
# takes it out as it returns, however it returns (END waits for the test's).
live: set = set()
# The entries that could not be hooked: (module, class.method, error).
failed: list = []
# How often each hooked entry was called, outer or nested: {name: n} (the
# checks against the real servers read it).
fired: dict = {}


# A request entry ran in this process (a process with none, whose app code
# runs in a test, is a pool's worker or a server keploy does not hook).
seen_entry = [False]


def _started(rid) -> None:
    seen_entry[0] = True
    for f in _on_start:
        f(rid)


class _Body:
    """The body a WSGI framework's entry returned, when no server entry is
    around it (a server keploy does not hook: uWSGI, mod_wsgi...): its
    iteration and its close() (Flask's call_on_close, Django's
    request_finished, a streaming body) run as the request, and the request
    is served until close()."""

    def __init__(self, it, rid):
        self._it, self._rid, self._iter, self._closed = it, rid, None, False

    def __iter__(self):
        return self

    def __next__(self):
        t1, t2 = REQUEST.set(self._rid), _IN_ENTRY.set(True)
        try:
            if self._iter is None:
                self._iter = iter(self._it)
            return next(self._iter)
        finally:
            _IN_ENTRY.reset(t2)
            REQUEST.reset(t1)

    def __len__(self):
        return len(self._it)

    def close(self):
        if self._closed:
            return
        self._closed = True
        t1, t2 = REQUEST.set(self._rid), _IN_ENTRY.set(True)
        try:
            close = getattr(self._it, "close", None)
            if close is not None:
                close()
        finally:
            _IN_ENTRY.reset(t2)
            REQUEST.reset(t1)
            live.discard(self._rid)

    def __del__(self):
        # A server that never closes the body: the request is not served
        # for ever.
        live.discard(self._rid)


def _body(res, rid, args):
    """res as a _Body, or res itself (a file wrapper the server sends as a
    file, or no iterable)."""
    try:
        fw = args[1].get("wsgi.file_wrapper") if len(args) > 1 else None
        if isinstance(fw, type) and isinstance(res, fw):
            return res
        iter(res)
    except Exception:
        return res
    return _Body(res, rid)


def _sync_entry(fn, scope_ok=None, server=False, body=False):
    name = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}"

    @functools.wraps(fn)
    def entry(*a, **kw):
        fired[name] = fired.get(name, 0) + 1
        # A framework's entry inside a server's is the same request; a
        # server's entry is always a new one (a pipelined request is served
        # from inside the one before it).
        if (not server and _IN_ENTRY.get()) or (scope_ok is not None and not scope_ok(a)):
            return fn(*a, **kw)
        rid = next(_ids)
        t1, t2 = REQUEST.set(rid), _IN_ENTRY.set(True)
        live.add(rid)
        held = False
        try:
            _started(rid)
            res = fn(*a, **kw)
            if body:
                # A WSGI framework's entry with no server's around it.
                res = _body(res, rid, a)
                held = isinstance(res, _Body)
            return res
        finally:
            if not held:
                live.discard(rid)
            _IN_ENTRY.reset(t2)
            REQUEST.reset(t1)
    entry.__keploy_entry__ = True
    return entry


def _async_entry(fn, scope_ok=None, server=False):
    name = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}"

    @functools.wraps(fn)
    async def entry(*a, **kw):
        fired[name] = fired.get(name, 0) + 1
        if (not server and _IN_ENTRY.get()) or (scope_ok is not None and not scope_ok(a)):
            return await fn(*a, **kw)
        rid = next(_ids)
        t1, t2 = REQUEST.set(rid), _IN_ENTRY.set(True)
        live.add(rid)
        try:
            _started(rid)
            return await fn(*a, **kw)
        finally:
            live.discard(rid)
            _IN_ENTRY.reset(t2)
            REQUEST.reset(t1)
    entry.__keploy_entry__ = True
    return entry


def _http_scope(args) -> bool:
    # (self, scope, receive, send): lifespan is no request.
    try:
        return args[1].get("type") in ("http", "websocket")
    except Exception:
        return True


# The frameworks' entries: one inside a server's entry is the same request.
FRAMEWORKS = ("starlette.applications", "flask.app", "django.core.handlers.wsgi",
              "django.core.handlers.asgi")
WSGI_FRAMEWORKS = ("flask.app", "django.core.handlers.wsgi")

# module -> [(class, method, scope_ok)]
ENTRIES = {
    "starlette.applications": [("Starlette", "__call__", _http_scope)],
    "flask.app": [("Flask", "wsgi_app", None)],
    "django.core.handlers.wsgi": [("WSGIHandler", "__call__", None)],
    "django.core.handlers.asgi": [("ASGIHandler", "__call__", _http_scope)],
    "uvicorn.protocols.http.h11_impl": [("RequestResponseCycle", "run_asgi", None)],
    "uvicorn.protocols.http.httptools_impl": [("RequestResponseCycle", "run_asgi", None)],
    "gunicorn.workers.sync": [("SyncWorker", "handle_request", None)],
    "gunicorn.workers.gthread": [("ThreadWorker", "handle_request", None)],
    "gunicorn.workers.base_async": [("AsyncWorker", "handle_request", None)],
    "werkzeug.serving": [("WSGIRequestHandler", "run_wsgi", None)],
    "wsgiref.handlers": [("BaseHandler", "run", None)],
    "waitress.task": [("WSGITask", "execute", None)],
    # run_application, not handle_one_request: the latter is called once
    # more per connection to find it closed, which is no request.
    "gevent.pywsgi": [("WSGIHandler", "run_application", None)],
}
# Modules patched for how work is handed on (_hooks), not for an entry.
for _name in _hooks.PATCHES:
    ENTRIES.setdefault(_name, [])


PATCHES = _hooks.PATCHES


def patch_module(mod) -> None:
    """Hook a module's entries. One that cannot be hooked is kept in failed
    (its requests are then not seen: their tests are partial, never wrong)."""
    extra = PATCHES.get(mod.__name__)
    if extra is not None:
        try:
            extra(mod)
        except Exception as e:
            if len(failed) < 16:
                failed.append((mod.__name__, extra.__name__, f"{type(e).__name__}: {e}"[:120]))
    for cls_name, meth, scope_ok in ENTRIES.get(mod.__name__, ()):
        try:
            cls = getattr(mod, cls_name, None)
            fn = getattr(cls, meth, None) if cls is not None else None
            if fn is None or getattr(fn, "__keploy_entry__", False):
                continue
            if inspect.iscoroutinefunction(fn):
                setattr(cls, meth, _async_entry(fn, scope_ok, server=mod.__name__ not in FRAMEWORKS))
            else:
                setattr(cls, meth, _sync_entry(fn, scope_ok, server=mod.__name__ not in FRAMEWORKS,
                                               body=mod.__name__ in WSGI_FRAMEWORKS))
        except Exception as e:
            if len(failed) < 16:
                failed.append((mod.__name__, f"{cls_name}.{meth}", f"{type(e).__name__}: {e}"[:120]))


class _Loader(importlib.abc.Loader):
    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        try:
            patch_module(module)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.loader, name)


_finding = threading.local()


class _Finder(importlib.abc.MetaPathFinder):
    """Patches an entry's module as it is imported. It asks the other
    finders on sys.meta_path, in their order (none is skipped: ddtrace's,
    OpenTelemetry's, a zip importer), for the spec, and wraps its loader; a
    finder that asks the others in turn (and so this one) gets None here."""

    def find_spec(self, name, path, target=None):
        if name not in ENTRIES:
            return None
        busy = getattr(_finding, "names", None)
        if busy is None:
            busy = _finding.names = set()
        if name in busy:
            return None
        busy.add(name)
        try:
            return self._find(name, path, target)
        finally:
            busy.discard(name)

    def _find(self, name, path, target):
        # The finders after this one, in their order (a zip, editable or
        # frozen importer included): the spec the import would have found.
        spec = None
        for f in list(sys.meta_path):
            if f is self or isinstance(f, _Finder):
                continue
            find = getattr(f, "find_spec", None)
            if find is None:
                continue
            try:
                spec = find(name, path, target)
            except Exception:
                return None
            if spec is not None:
                break
        if spec is None or spec.loader is None or isinstance(spec.loader, _Loader):
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install() -> None:
    """Hook the entries (now, for modules already imported), and make the
    threads and executor jobs the app starts carry their starter's
    context."""
    for name in ENTRIES:
        mod = sys.modules.get(name)
        if mod is not None:
            patch_module(mod)
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    _patch_threads()
    _patch_executor()
    try:
        import signal
        _hooks.patch_signal(signal)
    except Exception as e:
        failed.append(("signal", "signal", f"{type(e).__name__}: {e}"[:120]))


# Each started thread's request: {thread: rid}.
thread_request = weakref.WeakKeyDictionary()
# Each executor job's request: {future: rid}.
future_request = weakref.WeakKeyDictionary()


def keyed(t) -> bool:
    """Whether a thread was started under the agent (it has a request entry,
    maybe None). An unhashable one never is: its work is unattributed."""
    try:
        return t in thread_request
    except TypeError:
        return False


def _carry(rid, inside, fn, /, *args, **kwargs):
    """fn run with keploy's two variables set as its starter had them, in
    whatever context it runs in (the thread's own, the worker's), and reset
    after: nothing else of the starter's context is carried."""
    t1, t2 = REQUEST.set(rid), _IN_ENTRY.set(inside)
    try:
        return fn(*args, **kwargs)
    finally:
        _IN_ENTRY.reset(t2)
        REQUEST.reset(t1)


def _wrap_run(thread, rid) -> None:
    """thread.run, run with keploy's request id set (in the thread's own
    context, reset after), keeping its shape: a method bound to the thread,
    with a __func__ that takes the thread (sentry-sdk's patch calls
    getattr(run, "__func__", run)(thread))."""
    cur = thread.run
    func = getattr(cur, "__func__", None)
    if func is not None and getattr(cur, "__self__", None) is thread:
        def run(self, *args, **kwargs):
            t = REQUEST.set(rid)
            try:
                return func(self, *args, **kwargs)
            finally:
                REQUEST.reset(t)
                _unbind(self, bound)
    else:
        def run(self, *args, **kwargs):
            t = REQUEST.set(rid)
            try:
                return cur(*args, **kwargs)
            finally:
                REQUEST.reset(t)
                _unbind(self, bound)
    functools.update_wrapper(run, func or cur)
    bound = types.MethodType(run, thread)
    thread.run = bound


def _unbind(thread, bound) -> None:
    # The bound method on the thread is a cycle (thread -> run -> thread):
    # undone as the thread ends (sentry-sdk's #434).
    try:
        if thread.__dict__.get("run") is bound:
            del thread.__dict__["run"]
    except Exception:
        pass


def pool_worker(t) -> bool:
    """A pool's worker thread, whose jobs are tracked by their futures or
    contexts (not by the thread): concurrent.futures', anyio's."""
    target = getattr(t, "_target", None)
    if target is _cft._worker:
        return True
    cls = type(t)
    return cls.__name__ == "WorkerThread" and cls.__module__.startswith("anyio")


def _patch_threads() -> None:
    if getattr(threading.Thread.start, "__keploy__", False):
        return
    orig = threading.Thread.start

    @functools.wraps(orig)
    def start(self):
        # The request id only: a thread a request starts is not inside its
        # entry (a server entry there is a request of its own).
        rid = REQUEST.get()
        try:
            thread_request[self] = rid
        except TypeError:
            # An unhashable Thread subclass runs unkeyed: its app lines are
            # then foreign (the test partial), never the test's by mistake.
            return orig(self)
        if rid is not None:
            try:
                _wrap_run(self, rid)
            except Exception:
                pass
        return orig(self)

    start.__keploy__ = True
    threading.Thread.start = start


def _other_interpreter(ex) -> bool:
    ipe = getattr(sys.modules.get("concurrent.futures.interpreter"), "InterpreterPoolExecutor", None)
    return ipe is not None and isinstance(ex, ipe)


def _patch_executor() -> None:
    cls = _cft.ThreadPoolExecutor
    if getattr(cls.submit, "__keploy__", False):
        return
    orig = cls.submit

    @functools.wraps(orig)
    def submit(self, fn, /, *args, **kwargs):
        # Another interpreter's jobs (3.14 InterpreterPoolExecutor) are
        # pickled there, and measured on their own: left alone.
        if type(self) is not cls and _other_interpreter(self):
            return orig(self, fn, *args, **kwargs)
        # Always carried, None too: a worker a request started runs in that
        # request's values, which a job from elsewhere must not inherit.
        rid, inside = REQUEST.get(), _IN_ENTRY.get()
        fut = orig(self, _carry, rid, inside, fn, *args, **kwargs)
        try:
            future_request[fut] = rid
        except TypeError:
            pass
        return fut

    submit.__keploy__ = True
    cls.submit = submit


def as_request(fn):
    """fn served as one request (the rows' stand-in for a server)."""
    return _sync_entry(fn)
