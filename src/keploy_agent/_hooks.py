"""Where a request's work is handed on in ways threads, executors and asyncio
do not cover, each patched as its module is imported (_entries' finder):

- greenlets: gevent.spawn, spawn_later and pools (Group.start); eventlet's
  spawn, spawn_n, spawn_after (GreenPool too). A greenlet starts with an
  empty context, so one a request spawns is given a context of its own that
  holds only the request's id, and is kept ({greenlet: rid}) for END;
- uvloop's Loop.call_soon, call_soon_threadsafe, call_later and call_at:
  uvloop keeps its handles in C, so an app callback a request schedules is
  kept until it runs or is cancelled;
- queues (queue's and asyncio's Queue, LifoQueue, PriorityQueue): an item a
  request puts is kept until it is taken; the consumer that takes it then
  runs with that request's id, until it takes the next item;
- signal.signal: an app handler runs with no request's id (SIGNAL), as it
  interrupts whatever was running.

is_app(fn) is the agent's test of app code (set at start)."""

from __future__ import annotations

import contextvars
import functools
import itertools
import weakref

from ._measure import REQUEST, SIGNAL


def is_app(fn) -> bool:          # replaced by the agent at start
    return True


# ------------------------------------------------------------- greenlets
# Each greenlet a request spawned: {greenlet: rid}.
greenlet_request = weakref.WeakKeyDictionary()


def give_context(g, rid) -> None:
    """g, spawned by request rid and not started yet: kept, and run in a
    context of its own that holds only rid."""
    if rid is None or g is None:
        return
    try:
        greenlet_request[g] = rid
    except TypeError:
        pass
    try:
        if g.gr_frame is None and not g.dead and g.gr_context is None:
            ctx = contextvars.Context()
            ctx.run(REQUEST.set, rid)
            g.gr_context = ctx
    except Exception:
        pass


def _spawner(fn):
    @functools.wraps(fn)
    def spawn(*args, **kwargs):
        g = fn(*args, **kwargs)
        give_context(g, REQUEST.get())
        return g
    spawn.__keploy__ = True
    return spawn


def _patch_spawners(mod, names) -> None:
    for name in names:
        fn = getattr(mod, name, None)
        if fn is not None and not getattr(fn, "__keploy__", False):
            setattr(mod, name, _spawner(fn))


def patch_gevent(mod) -> None:
    # gevent's Greenlet is a C type: its own spawn and start cannot be
    # patched (a Greenlet(...).start() of the app's is not kept).
    _patch_spawners(mod, ("spawn", "spawn_later"))


def patch_gevent_pool(mod) -> None:
    group = getattr(mod, "Group", None)
    orig = getattr(group, "start", None)
    if orig is None or getattr(orig, "__keploy__", False):
        return

    @functools.wraps(orig)
    def start(self, greenlet, *args, **kwargs):
        give_context(greenlet, REQUEST.get())
        return orig(self, greenlet, *args, **kwargs)
    start.__keploy__ = True
    group.start = start


def patch_eventlet(mod) -> None:
    # eventlet and eventlet.greenthread: GreenPool calls the module's
    # functions, so it is covered too.
    _patch_spawners(mod, ("spawn", "spawn_n", "spawn_after"))


# ------------------------------------------------------------- uvloop
# An app callback a request scheduled on uvloop, not run or cancelled yet:
# {key: (rid, handle, callback)}.
pending: dict = {}
_keys = itertools.count()
_MAX_PENDING = 65536
_PLAIN = (type(len), type([].append), type(object.__init__), type("".__add__))


def _prune() -> None:
    for k, (_, h, _) in list(pending.items()):
        try:
            if h.cancelled():
                pending.pop(k, None)
        except Exception:
            pending.pop(k, None)
    while len(pending) > _MAX_PENDING:
        pending.pop(next(iter(pending)), None)


def _scheduler(orig, at):
    """orig (a uvloop scheduling method) whose callback is args[at]."""
    @functools.wraps(orig)
    def schedule(self, *args, context=None):
        try:
            rid = context.get(REQUEST) if context is not None else REQUEST.get()
            cb = args[at] if rid is not None and len(args) > at else None
            if cb is None or type(cb) in _PLAIN or not is_app(cb):
                return orig(self, *args, context=context)
        except Exception:
            return orig(self, *args, context=context)
        key = next(_keys)

        def run(*a):
            pending.pop(key, None)
            return cb(*a)
        h = orig(self, *args[:at], run, *args[at + 1:], context=context)
        pending[key] = (rid, h, cb)
        if len(pending) > 1024 and key % 1024 == 0:
            _prune()
        return h
    schedule.__keploy__ = True
    return schedule


def patch_uvloop(mod) -> None:
    loop = getattr(mod, "Loop", None)
    if loop is None:
        return
    for name, at in (("call_soon", 0), ("call_soon_threadsafe", 0), ("call_later", 1), ("call_at", 1)):
        orig = getattr(loop, name, None)
        if orig is not None and not getattr(orig, "__keploy__", False):
            setattr(loop, name, _scheduler(orig, at))


# ------------------------------------------------------------- queues
# The queues that hold an item a request put: their items' requests are in
# the queue's own _keploy_items ({id(item): [(rid, item)]}).
queues = weakref.WeakSet()
# The consumer runs a queued item's request now (set by a take).
_QUEUED = contextvars.ContextVar("keploy_queued", default=False)


def _putter(orig):
    @functools.wraps(orig)
    def _put(self, item, *a, **kw):
        rid = REQUEST.get()
        if rid is not None:
            try:
                reg = self.__dict__.setdefault("_keploy_items", {})
                reg.setdefault(id(item), []).append((rid, item))
                queues.add(self)
            except Exception:
                pass
        return orig(self, item, *a, **kw)
    _put.__keploy__ = True
    return _put


def _getter(orig):
    @functools.wraps(orig)
    def _get(self, *a, **kw):
        item = orig(self, *a, **kw)
        rid = None
        try:
            reg = self.__dict__.get("_keploy_items")
            got = reg.get(id(item)) if reg else None
            if got:
                for i, (r, it) in enumerate(got):
                    if it is item:
                        rid = r
                        del got[i]
                        break
                if not got:
                    reg.pop(id(item), None)
            # The taker's work from now on is the item's request's, until
            # its next take (a consumer's loop).
            if rid is not None:
                REQUEST.set(rid)
                _QUEUED.set(True)
            elif _QUEUED.get():
                REQUEST.set(None)
                _QUEUED.set(False)
        except Exception:
            pass
        return item
    _get.__keploy__ = True
    return _get


def _patch_queues(mod) -> None:
    for name in ("Queue", "LifoQueue", "PriorityQueue"):
        cls = getattr(mod, name, None)
        if cls is None:
            continue
        for meth, wrap in (("_put", _putter), ("_get", _getter)):
            orig = cls.__dict__.get(meth)
            if orig is not None and not getattr(orig, "__keploy__", False):
                setattr(cls, meth, wrap(orig))


patch_queue = _patch_queues
patch_asyncio_queues = _patch_queues


def queued(rid) -> int:
    """Items request rid put that no one has taken yet."""
    n = 0
    for q in list(queues):
        reg = q.__dict__.get("_keploy_items") or {}
        if not reg:
            queues.discard(q)
            continue
        n += sum(1 for v in list(reg.values()) for r, _ in v if r == rid)
    return n


# ------------------------------------------------------------- signals
def patch_signal(mod) -> None:
    orig_signal, orig_get = mod.signal, mod.getsignal
    if getattr(orig_signal, "__keploy__", False):
        return

    @functools.wraps(orig_signal)
    def signal(signalnum, handler):
        h = handler
        if callable(handler) and is_app(handler):
            @functools.wraps(handler)
            def h(signum, frame):
                # It interrupts whatever runs (a test's request too): its
                # lines are no request's.
                t = REQUEST.set(SIGNAL)
                try:
                    return handler(signum, frame)
                finally:
                    REQUEST.reset(t)
            h.__keploy_signal__ = handler
        prev = orig_signal(signalnum, h)
        return getattr(prev, "__keploy_signal__", prev)

    @functools.wraps(orig_get)
    def getsignal(signalnum):
        h = orig_get(signalnum)
        return getattr(h, "__keploy_signal__", h)

    signal.__keploy__ = True
    mod.signal, mod.getsignal = signal, getsignal


# ------------------------------------------------------------- ThreadPool
# multiprocessing.pool.ThreadPool's results a request's jobs give:
# {result: rid}. Its worker threads run in their own start's context, so a
# job carries the request's id as a concurrent.futures job does.
pool_results = weakref.WeakKeyDictionary()


def _carry_rid(rid, fn, *args, **kwargs):
    t = REQUEST.set(rid)
    try:
        return fn(*args, **kwargs)
    finally:
        REQUEST.reset(t)


def patch_mp_pool(mod) -> None:
    tp = getattr(mod, "ThreadPool", None)
    if tp is None:
        return
    for name in ("apply_async", "_map_async", "imap", "imap_unordered"):
        orig = getattr(tp, name, None)
        if orig is None or getattr(orig, "__keploy__", False):
            continue

        def make(orig):
            @functools.wraps(orig)
            def submit(self, func, *args, **kwargs):
                rid = REQUEST.get()
                if rid is None:
                    return orig(self, func, *args, **kwargs)
                res = orig(self, functools.partial(_carry_rid, rid, func), *args, **kwargs)
                try:
                    pool_results[res] = rid
                except TypeError:
                    pass
                return res
            submit.__keploy__ = True
            return submit
        setattr(tp, name, make(orig))


def pool_pending(rid) -> int:
    n = 0
    for res, r in list(pool_results.items()):
        ready = getattr(res, "ready", None)
        if r == rid and ready is not None and not ready():
            n += 1
    return n


PATCHES = {
    "multiprocessing.pool": patch_mp_pool,
    "gevent": patch_gevent,
    "gevent.pool": patch_gevent_pool,
    "eventlet": patch_eventlet,
    "eventlet.greenthread": patch_eventlet,
    "uvloop": patch_uvloop,
    "queue": patch_queue,
    "asyncio.queues": patch_asyncio_queues,
}
