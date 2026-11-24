# Keploy Python Coverage Agent

The Keploy Python Coverage Agent is a lightweight, sidecar module designed to integrate with the Keploy integration testing platform (enterprise version). When imported into a Python application, it enables Keploy to capture and report code coverage on a per-test-case basis.

## Installation and Usage

Follow these steps to integrate the coverage agent into your Python project.

### Step 1: Install the Agent

Install the `keploy-agent` package from PyPI into your virtual environment:

```bash
pip install keploy-agent
```

If you are developing the agent, install it in editable mode from a checkout of this repository instead:

```bash
pip install -e /path/to/python-sdk
```

### Step 2: Integrate into Your Application

To enable coverage tracking, import the `keploy_agent` module at the **very top** of your application's main entry point file (e.g., `app.py`, `main.py`).

It is crucial that this is one of the first imports, as this ensures the agent is initialized before your application code begins to execute.

**Example `app.py`:**

```python
import keploy_agent  # <-- Add this line at the top
import os
from flask import Flask

app = Flask(__name__)

@app.get("/")
def hello():
    # This function will be tracked by the coverage agent
    # when its endpoint is hit during a Keploy test.
    return "Hello, World!"

if __name__ == "__main__":
    app.run()
```

#### Or: no change to the app

Instead of the import, put the agent's start-up hook first on the app's
`PYTHONPATH`. Python then starts the agent in the app's process at start-up:

```bash
PYTHONPATH="$(python -m keploy_agent --pythonpath)" uvicorn app.main:app --port 8000
```

`python -m keploy_agent --pythonpath` prints the hook's directory followed by
the `PYTHONPATH` already set, which is kept as it is; an app's own
`sitecustomize` still runs after the agent's.

Every Python process of the app is measured (with the PYTHONPATH hook; with
an explicit import, the processes that import it). Each binds its own control
socket in `/tmp/coverage_control.d/` (`<pid namespace>-<pid>.sock`, or
`<pid>-<random>.sock` when that name is taken; the dir is made `1777`, the
socket `0666`, and a dir that is not a real, sticky dir owned by root or the
app's user is refused), keploy sends each test's START and END to every one
of them, and merges their records: a reloader and its server child (Django
`runserver`, `uvicorn --reload`, Flask's debug reloader), prefork workers
(gunicorn: a forked worker binds its own socket), the workers of
multiprocessing's forkserver, and any other Python process the app starts.
multiprocessing's resource tracker and semaphore tracker never start the
agent, and a fork that execs at once (`subprocess` with `preexec_fn`) binds
no socket.

Each record names the process (`pid`), its build, and the hash of every file
it ran, so keploy can tell lines of other bytes than its build table's.

### What is measured

- The app's own `.py` files under the app root (`KEPLOY_APP_SOURCE_DIR`, else
  the working directory): not virtual environments, `site-packages`, hidden
  directories, `tests/`, `test_*.py`, `*_test.py` or `conftest.py`. Leave more
  out with `KEPLOY_COVERAGE_EXCLUDE` (comma-separated globs of app-root-relative
  paths, such as `scripts/*,migrations/*`).
- Python 3.12 and later: `sys.monitoring`, which measures every thread at the
  cost of about one call per line a test runs. Python 3.9 to 3.11:
  `sys.settrace`, which measures threads started after the agent: import it
  first.
- Keploy asks the agent once per build for every app file's coverable lines,
  so its coverage report shows the lines no test ran too.
- A test's lines are the app lines run between keploy's START and END for it.
  Lines that run when a module is imported (`import`, `def` and `class` lines,
  module and class-body code) run before the first test unless the import
  itself happens during a test, so no test covers them and the report counts
  them as not run.
- Lines that run when a process starts (imports, the app's own start) come
  in its STARTUP record (sent to each keploy receiver), which keploy credits
  to no test.

#### Which lines are the test's

Every request that comes into the app gets an id, at its entry: Starlette
(FastAPI), Flask and Django (WSGI and ASGI) whatever server runs them, and
the servers uvicorn (h11, httptools; asyncio's loop or uvloop), gunicorn
(sync, gthread, gevent, eventlet), werkzeug, wsgiref, waitress and gevent's
pywsgi for apps of no framework above. The work a request hands on carries
its id: the threads it starts, `concurrent.futures` and
`multiprocessing.pool.ThreadPool` jobs, asyncio tasks and callbacks (uvloop's
too), greenlets it spawns (`gevent.spawn`, `spawn_later`, gevent's pools and
groups; eventlet's `spawn`, `spawn_n`, `spawn_after` and `GreenPool`), and
the items it puts on a `queue` or `asyncio` queue (the consumer that takes
one runs as its request until it takes the next). keploy carries only its
own id, nothing else of the request's context. keploy's replay sends one
request per test: a line is the test's when its id is the first request's of
the test's window. Each record says how many requests its process saw in the
window (`requests`); keploy sums them over the app's processes.

A record that may hold lines that are not the test's own, or lack some of
its own, says so (`partial`, with the reason); keploy then reports the test
as not measured rather than count its lines:

| What | `partial` says |
|---|---|
| A second request in the test (a health probe, another client, a pipelined request) | `another request was served during the test (N requests)` |
| App code in a process that never had a request keploy sees: a process pool's worker running the test's work, or a server or framework keploy does not hook | `app code ran in the test in a process that served no request keploy sees ...` |
| App code in a process that serves requests, in a test with none in it (a poller, a scheduler, a queue's consumer) | `app code ran in the test outside any request ...` |
| App code in the test with no request's id or another request's: a scheduler, a poller, a background thread or task, a job on a shared pool, a Timer or callback another test left, a signal handler, a finalizer the garbage collector ran | `app code that is not the test's ran during it (<task or thread>, a signal handler, the garbage collector's finalizers)` |
| The test's request still being served 50 ms after its END: its entry has not returned (a yield dependency's teardown after the response, `request_finished` receivers, a streaming body, a greenlet parked on I/O), or, under a server keploy does not hook, its WSGI body is not closed | `the test's request was still being served at its end` |
| The test's own work still waiting 50 ms after its END: a thread it started that is alive (in a library's code too; a pool's worker is judged by its jobs), an executor or ThreadPool job, an asyncio task with app code, an app callback (asyncio's or uvloop's), a greenlet it spawned, an item it queued that no one took | `work the test started was still running at its end (threads: ...)`, `executor jobs the test submitted had not finished at its end (N)`, `an asyncio task the test created was still pending at its end (...)`, `a callback the test scheduled had not run at its end (...)`, `a greenlet the test started was still waiting at its end (...)`, `work the test queued had not been taken at its end (N item(s))` |
| Before 3.12: a thread keploy's tracer cannot reach (started before the agent, by `_thread` or by C) on app code at START or END, or whose stack moved between them | `a thread keploy cannot trace ran Python during the test (...)` |
| A fork during the test (a fork `subprocess` makes to exec, `preexec_fn`, is none) | `a process forked during the test (pid N)` |
| A file edited after the process started (before its build was read: the process may run the bytes from before), or whose bytes changed after the build was read, or that cannot be read | `<path> was edited after this process started` / `<path> changed after this process's build was read` / `<path> could not be read: ...` |
| A state read that fails, an object of the app's that cannot be read (a proxy that raises), an error in keploy's line callback | `could not read the process's state: ...` / `keploy's line callback failed in ...` |

Before 3.12 a tracer the app sets on a running thread with `sys.settrace`
(a debugger, a profiler) takes that thread from keploy's: the END and every
START after it are not taken (`IGNORED`, with why), as when another tracer
replaces keploy's for new threads.

The test's own work after its response (a FastAPI yield dependency's
teardown, sync or async; Django's `request_finished`; gunicorn's
`post_request`) runs inside the request's entry, so END waits for the entry
to return, up to 50 ms, before the record is made. A thread's app frames
that were on its stack at START (the `<module>` of `python app.py`, a
`manage.py`'s `main()`, `python -m pkg`'s `__main__`, a poller's loop) are
not the test's work. Lines that run between two tests go to keploy's
outside-tests bucket (a `startup` record with `"between": true`), never to a
test. A Python process that ran app code no record carried (one started and
ended inside a test) keeps its socket file when it exits, so keploy sees a
process come at the test's END and does not verify the test. A test whose
new lines keep coming for 0.1 s after its END is reported as cut short
(`windowCut`).

Servers and frameworks keploy does not hook, whose tests are all partial
(the app code runs with no request entry keploy knows): aiohttp, tornado,
sanic, Twisted and grpcio servers; and a WSGI or ASGI app of no framework
above (plain WSGI, falcon, bottle, Litestar...) on a server keploy does not
hook: uWSGI, mod_wsgi, granian, hypercorn, daphne, bjoern, meinheld,
CherryPy. falcon and bottle on a hooked server are measured through the
server's entry. FastAPI, Flask and Django on a server keploy does not hook
are measured through the framework's entry when all of the app's code runs
inside it: app code outside it (a WSGI or ASGI middleware of the app's in
`wsgi.py` or `asgi.py`, `app.wsgi_app = Middleware(app.wsgi_app)`, a `Flask`
subclass whose `wsgi_app` runs lines before `super()`) has no request's id,
so every test is partial. A WSGI framework's body under such a server
(Flask's `call_on_close`, Django's `request_finished`, a streaming body)
runs as the request, which is served until the server closes the body.

uWSGI (tested with 2.0.28: Flask and Django; plain WSGI is partial, above).
uWSGI forks its workers from C. keploy starts no thread and binds no
socket in uWSGI's master (a fork while one of its threads held the GIL
would leave the worker stuck on it); each worker (and mule or spooler)
binds its own socket as uWSGI starts it (`uwsgi.post_fork_hook`, chained
with the app's; with `--py-call-uwsgi-fork-hooks`, Python's own fork
handlers), keeping the lines its master's import ran as its start. A
worker whose hook was replaced (`uwsgidecorators` imported after the agent)
binds at its first app line instead: keploy does not verify the test it
comes in. With `--threads N`, on 3.12+ each request is measured on its
thread; before 3.12 keploy's tracer cannot reach uWSGI's request threads,
so the tests are partial. A uWSGI without threads (older versions without
`--enable-threads`) cannot run keploy's control thread: its processes say
so in an `unmeasured` record and bind nothing. mod_wsgi starts Python in
each Apache child or daemon process after its fork (and runs Python's
fork handlers itself), so each is a process of its own, measured like any
other; the PYTHONPATH hook reaches it only when Apache's environment
carries `PYTHONPATH` (use the explicit import). mod_wsgi has no row yet.

What the agent cannot observe, and so cannot say, each because no Python
object or call shows it:

- before 3.12, a thread keploy's tracer cannot reach (started before the
  agent, by `_thread.start_new_thread`, or by C) that runs app code and
  is back at the same waiting point by the test's END: before 3.12 Python
  has no way to put a tracer on another thread that already runs (3.12's
  `sys.monitoring` measures every thread), so keploy sees such a thread
  only at START and END (import the agent first, or use the PYTHONPATH
  hook, so no such thread is the app's);
- before 3.12, a tracer set from C (`PyEval_SetTrace`: coverage.py's C
  tracer started on a thread) rather than through `sys.settrace`: it is no
  Python call;
- app code a C extension calls back on a thread of its own, on 3.12+, in a
  context the extension chose: whose work it is follows that context, and
  only the extension knows where it came from;
- work handed to something that is not a Python object of this process: a
  C library's or another process's queue (`queue.SimpleQueue`, a C type;
  `multiprocessing` queues; a broker such as Celery's or RQ's; another
  service): when it runs, it runs elsewhere or later, outside the window or
  in another test's, where its lines are another's;
- a greenlet started directly rather than spawned (gevent's
  `Greenlet(...).start()` or `Greenlet.spawn` on the class: gevent's
  Greenlet is a C type; eventlet's `GreenThread`; a raw
  `greenlet.greenlet`): finding it would take a walk of the whole heap at
  every END. Its lines run with no request's id, another's when they run
  in the test (partial), in no test when they run after its END;
- a file replaced after the process imported it by a copy that keeps an
  older mtime (`cp -p`, `rsync -t`, `tar x`): keploy knows a file changed
  by its stat, and only hashing every file at its import would show it;
- a Python process of the app started without the PYTHONPATH hook (an
  entry that does not import `keploy_agent`, `python -I`, `-E` or `-S`, or
  `subprocess` with an `env=` that drops `PYTHONPATH`): it runs no keploy
  code, so it is not measured and keploy is not told.

What keploy sees but cannot measure: a process that cannot bind its socket
says so in its log and in an `unmeasured` record to keploy (with the dir it
tried, sent at once to the receiver there and to each new one; keploy then
does not trust any test from then on); a free-threaded Python (no GIL) is
refused, and says so. With `KEPLOY_COVERAGE_CONTROL_DIR` (keploy's run dir,
which keploy makes, mode 1777) the agent binds only there, and refuses a dir
that is not a real, sticky, 1777 dir owned by root or the app's user.

The wire changed in 0.2.0: use a keploy whose agent sends its commands to
`/tmp/coverage_control.d/` (older keploy finds no `/tmp/coverage_control.sock`
and reports that no command reached a coverage SDK).

### Step 3: Run with Keploy

Now, you can run your application tests using the Keploy CLI. The agent will automatically connect with Keploy.

```bash
sudo -E keploy-enterprise test -c "python3 app.py" --language python --dedup
```

Now you will see `dedupData.yaml` getting created. 

Run `sudo -E keploy-enterprise dedup` to get the tests which are duplicate in `duplicates.yaml` file

In order to remove the duplicate tests, run the following command:

```bash
sudo -E keploy-enterprise dedup --rm
```
