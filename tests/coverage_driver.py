"""Runs one coverage case against keploy_agent in its own process, with a fake
keploy agent on the data socket.

The agent measures the whole process (sys.monitoring, or settrace before
3.12) once imported, so each case runs here, in a child of the test. The
import binds the case's own control socket and sends to the case's own data
socket (control_driver.import_agent), never keploy's paths in /tmp.

argv[1] is the case as JSON: "sock" (the control socket), "data" (the data
socket the fake agent serves), "app" (the app root, on sys.path; the test
sets KEPLOY_APP_SOURCE_DIR to it) and "steps", each one of:
  {"cmd": "START s/t1"}       sent on the control socket; its answer is kept
  {"call": "mod:func"}        imports mod and calls func()
  {"thread": "mod:func"}      calls it on a new thread, and joins it
  {"rebind": true}            the fake agent binds its socket again (a new
                              receiver: a new inode)
  {"wait": "frames", "n": 2}  waits until n parts came whole
  {"wait": "records", "n": 3} waits until n records came

The fake agent takes what pkg/service/agent's receiver takes: a connection
that opens with "K" is a KTB1 frame (each part's stream checked on its last
frame as finishLocked checks it, then "OK"), any other is one JSON record.
Prints one JSON line: {"answers", "records", "parts", "errors"}.
"""

import importlib
import json
import os
import sys
import threading
import time

case = json.loads(sys.argv[1])
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, case["app"])

from fake_agent import FakeAgent, command  # noqa: E402

out = {"answers": [], "records": [], "parts": [], "errors": []}
lock = threading.Lock()


def answer(sock, cmd):
    """The answer to cmd, or "refused" when no one listens any more."""
    try:
        return command(sock, cmd)
    except ConnectionRefusedError:
        return "refused"


def sock_of(pid, seconds=10):
    """The socket file of process pid (<ns>-<pid>.sock), once it is there."""
    d = os.path.dirname(case["sock"])
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for n in os.listdir(d):
            if n.endswith(f"-{pid}.sock"):
                return os.path.join(d, n)
        time.sleep(0.01)
    return ""


def resolve(ref):
    mod, _, fn = ref.partition(":")
    return getattr(importlib.import_module(mod), fn)


def import_agent(sock, data):
    """Imports keploy_agent from its source with its two socket lines
    changed to name sock and data (as control_driver does)."""
    import importlib.util

    spec = importlib.util.find_spec("keploy_agent")
    src = spec.loader.get_source("keploy_agent")
    line = 'DATA_SOCK = "/tmp/coverage_data.sock"'
    if src.count(line) != 1:
        raise SystemExit(f"keploy_agent does not have the line {line!r} once")
    src = src.replace(line, f"DATA_SOCK = {data!r}")
    ka = importlib.util.module_from_spec(spec)
    sys.modules["keploy_agent"] = ka
    exec(compile(src, spec.origin, "exec"), ka.__dict__)
    return ka


PY = {}


def main():
    fake = FakeAgent(case["data"], out, lock)
    if case.get("pre_py"):
        # Code run before the agent's import (another tool's patch).
        PY.update(out=out, resolve=resolve)
        exec(case["pre_py"], PY)
    if case.get("pre_thread"):
        # A thread running app code from before the agent's import (before
        # 3.12 its tracer never reaches it).
        fn = resolve(case["pre_thread"])

        def pre():
            fn()

        threading.Thread(target=pre, name="pre", daemon=True).start()
        time.sleep(0.05)
    ka = import_agent(None, case["data"])
    # This process's own socket, <pid>.sock in KEPLOY_COVERAGE_CONTROL_DIR.
    case["sock"] = ka.CONTROL_SOCK
    out["sock"] = ka.CONTROL_SOCK
    out["producer"] = ka._collector.name if ka._collector else None
    out["driver_pid"] = os.getpid()
    def run_step(step):
        global CHILD, POOL
        if "cmd" in step:
            out["answers"].append(answer(case["sock"], step["cmd"]))
        elif "py" in step:
            # A row's own code, with the agent (ka), resolve, out and
            # as_request (fn served as one request) at hand.
            PY.update(ka=ka, resolve=resolve, out=out, as_request=ka._entries.as_request)
            exec(step["py"], PY)
        elif "call" in step:
            # As a server would: one request, the app's function its work.
            ka._entries.as_request(resolve(step["call"]))()
        elif step.get("pool_start"):
            from concurrent.futures import ThreadPoolExecutor
            SHARED.append(ThreadPoolExecutor(max_workers=1, thread_name_prefix="shared"))
            SHARED[0].submit(lambda: None).result()
        elif "pool_job" in step:
            # Submitted from outside any request (a scheduler's tick).
            SHARED[0].submit(resolve(step["pool_job"])).result()
        elif "call_pool" in step:
            # The test's request hands a job to the shared pool.
            fn = resolve(step["call_pool"])
            ka._entries.as_request(lambda: SHARED[0].submit(fn).result())()
        elif "wsgiref_request" in step:
            # A real server keploy hooks (wsgiref, in the stdlib): one HTTP
            # request, the app's function its work.
            import http.client
            from wsgiref.simple_server import make_server, WSGIRequestHandler

            if not WSGI:
                fn = resolve(step["wsgiref_request"])

                def app(environ, start_response):
                    fn()
                    start_response("200 OK", [("Content-Type", "text/plain")])
                    return [b"ok"]

                class Quiet(WSGIRequestHandler):
                    def log_message(self, *a):
                        pass

                srv = make_server("127.0.0.1", 0, app, handler_class=Quiet)
                threading.Thread(target=srv.serve_forever, name="wsgiref", daemon=True).start()
                WSGI.append(srv)
            c = http.client.HTTPConnection("127.0.0.1", WSGI[0].server_port, timeout=10)
            c.request("GET", "/")
            out.setdefault("wsgi_status", []).append(c.getresponse().status)
            c.close()
        elif "call_pool_nowait" in step:
            fn = resolve(step["call_pool_nowait"])
            ka._entries.as_request(lambda: SHARED[0].submit(fn))()
        elif "plain_call" in step:
            resolve(step["plain_call"])()
        elif "thread" in step:
            def in_request(fn=resolve(step["thread"])):
                t = threading.Thread(target=fn)
                t.start()
                t.join()
            ka._entries.as_request(in_request)()
        elif step.get("stub") == "lines_keep_coming":
            # Each read of the count finds a new line: END never settles.
            n = iter(range(10**9))
            ka._collector.count = lambda: next(n)
        elif step.get("stub") == "lines_settle":
            del ka._collector.count
        elif step.get("fork"):
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                deadline = time.monotonic() + 10
                while not ka._srv and time.monotonic() < deadline:
                    time.sleep(0.01)
                os.write(w, json.dumps({"collector": ka._collector is not None,
                                        "sock": ka.CONTROL_SOCK, "pid": os.getpid(),
                                        "current": ka.current_id}).encode())
                os._exit(0)
            os.close(w)
            data = b""
            while True:
                chunk = os.read(r, 4096)
                if not chunk:
                    break
                data += chunk
            os.waitpid(pid, 0)
            out["forked"] = json.loads(data)
        elif step.get("cfork_worker"):
            # A worker forked from C, as uWSGI's master and mod_wsgi fork
            # theirs: libc's fork() through ctypes (PyDLL: the GIL held), so
            # none of Python's at-fork handlers runs.
            import ctypes
            libc = ctypes.PyDLL(None)
            pid = libc.fork()
            if pid == 0:
                b = resolve("shop.orders:b")
                while True:
                    b()
                    time.sleep(0.002)
            workers.append(pid)
            path = sock_of(pid)
            out.setdefault("worker_socks", []).append(path)
        elif step.get("fork_worker") == "start":
            # A prefork worker: a live child that runs app code (b) all the
            # time, as a worker serving requests does.
            pid = os.fork()
            if pid == 0:
                b = resolve("shop.orders:b")
                while True:
                    b()
                    time.sleep(0.002)
            workers.append(pid)
            path = sock_of(pid)
            out.setdefault("worker_socks", []).append(path)
        elif "cmd_all" in step:
            # A command to every socket in the dir, as keploy sends it.
            d = os.path.dirname(case["sock"])
            # As keploy's agent does: every *.sock, its name never parsed;
            # one that refuses the connection is a dead process's (removed).
            for name in sorted(os.listdir(d)):
                if not name.endswith(".sock"):
                    continue
                a = answer(os.path.join(d, name), step["cmd_all"])
                if a == "refused":
                    os.remove(os.path.join(d, name))
                    continue
                out.setdefault("all_answers", []).append([name, a])
        elif step.get("fork_exec"):
            # subprocess with preexec_fn: Python's at-fork hooks run, then
            # the child execs at once.
            import subprocess
            p = subprocess.Popen(["sleep", "1"], preexec_fn=lambda: None)
            time.sleep(0.3)
            out["exec_child_sock"] = any(f"-{p.pid}." in n or n.startswith(f"{p.pid}-")
                                         for n in os.listdir(os.path.dirname(case["sock"])))
            p.wait()
        elif "spawn_thread" in step:
            # A thread started now that runs app code for `seconds`.
            fn = resolve(step["spawn_thread"])
            until = time.monotonic() + step.get("seconds", 0.3)

            def loop(fn=fn, until=until):
                while time.monotonic() < until:
                    fn()
                    time.sleep(0.0005)

            t = threading.Thread(target=loop, name=step.get("name", "worker"), daemon=True)
            t.start()
            out.setdefault("threads", []).append(t.name)
        elif "idle_pool" in step and not step.get("_inside"):
            ka._entries.as_request(lambda st=dict(step, _inside=True): run_step(st))()
        elif "idle_pool" in step:
            # A pool whose thread is made in the window, runs app code once
            # and then waits idle: never the test's lost work.
            from concurrent.futures import ThreadPoolExecutor
            POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pool")
            POOL.submit(resolve(step["idle_pool"])).result()
        elif "wait_s" in step:
            time.sleep(step["wait_s"])
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif step.get("threads_now"):
            out.setdefault("threads_now", []).append(sorted(t.name for t in threading.enumerate()))
        elif step.get("fork_locked"):
            # A fork while a command holds the window lock and the build
            # holds its own (and the build not read yet): the child must
            # still answer.
            held = threading.Event()
            release = threading.Event()

            def hold(lk):
                with lk:
                    held.set()
                    release.wait(10)

            ka._index, ka._index_ready = None, threading.Event()
            for lk in (ka.lock, ka._index_lock):
                held.clear()
                threading.Thread(target=hold, args=(lk,), daemon=True).start()
                held.wait(5)
            pid = os.fork()
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            release.set()
            workers.append(pid)
            path = sock_of(pid)
            t0 = time.monotonic()
            c = answer(path, "START s/locked") if path else "no socket"
            out["locked_child"] = [c, round(time.monotonic() - t0, 2)]
        elif step.get("bind_order"):
            # The final name appears only once the socket listens.
            import socket as socketmod
            final = os.path.join(os.path.dirname(case["sock"]), "probe-bind.sock")
            seen = []
            real_listen = socketmod.socket.listen

            def listen(self, *a):
                seen.append(os.path.exists(final))
                return real_listen(self, *a)

            socketmod.socket.listen = listen
            # An app's own, strict umask: never changed by keploy, and the
            # socket 0666 all the same.
            os.umask(0o077)
            mask = 0o077
            try:
                srv = ka._bind(final)
            finally:
                socketmod.socket.listen = real_listen
            after = os.umask(mask)
            os.umask(after)
            out["umask_kept"] = mask == after
            out["sock_mode"] = oct(os.stat(final).st_mode & 0o777)
            c = socketmod.socket(socketmod.AF_UNIX)
            c.connect(final)
            c.close()
            srv.close()
            out["bind_order"] = {"final_at_listen": seen, "temp_left": [
                n for n in os.listdir(os.path.dirname(final)) if n.endswith(".tmp")]}
        elif step.get("fork_quick"):
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
        elif "exec_python" in step:
            # A Python process the test runs (manage.py, a script): it binds
            # its own socket, runs app code, and exits before the END.
            import subprocess
            mod, _, fn = step["exec_python"].partition(":")
            d = os.path.dirname(case["sock"])
            before = set(os.listdir(d))
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(
                [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
                 case["app"]]))
            env.pop("KEPLOY_AGENT_FIRST_PID", None)
            subprocess.run([sys.executable, "-c", f"import keploy_agent, {mod}; {mod}.{fn}()"],
                           env=env, check=True, timeout=60)
            out["exec_left"] = sorted(set(os.listdir(d)) - before)
        elif "edit_app" in step:
            # The file's bytes change under the running process (an editor,
            # a checkout).
            path = os.path.join(case["app"], step["edit_app"])
            with open(path, "a") as f:
                f.write("\n# edited\n")
        elif "io_thread" in step and not step.get("_inside"):
            ka._entries.as_request(lambda st=dict(step, _inside=True): run_step(st))()
        elif "io_thread" in step:
            # A thread of the test blocked in I/O inside app code at its end.
            import socket as socketmod
            a, b = socketmod.socketpair()
            PAIRS.append((a, b))
            # Its target is not app code (a library's worker calls into the
            # app): only its stack tells it runs the test's work.
            fn = resolve(step["io_thread"])
            t = threading.Thread(target=lambda: fn(a), name="io", daemon=True)
            t.start()
        elif step.get("io_release"):
            for a, b in PAIRS:
                b.sendall(b"x")
            PAIRS.clear()
        elif "timer" in step and not step.get("_inside"):
            ka._entries.as_request(lambda st=dict(step, _inside=True): run_step(st))()
        elif "timer" in step:
            t = threading.Timer(step.get("delay", 1.0), resolve(step["timer"]))
            t.name = "timer"
            t.daemon = True
            t.start()
        elif step.get("loop_start"):
            import asyncio
            LOOP.append(asyncio.new_event_loop())
            threading.Thread(target=LOOP[0].run_forever, name="loop", daemon=True).start()
        elif "loop_run" in step:
            import asyncio
            asyncio.run_coroutine_threadsafe(resolve(step["loop_run"])(), LOOP[0]).result(10)
        elif "loop_task" in step and not step.get("_inside"):
            ka._entries.as_request(lambda st=dict(step, _inside=True): run_step(st))()
        elif "loop_task" in step:
            import asyncio
            asyncio.run_coroutine_threadsafe(resolve(step["loop_task"])(), LOOP[0])
            time.sleep(0.05)
        elif "loop_call_later" in step and not step.get("_inside"):
            ka._entries.as_request(lambda st=dict(step, _inside=True): run_step(st))()
        elif "loop_call_later" in step:
            LOOP[0].call_soon_threadsafe(LOOP[0].call_later, step.get("delay", 1.0),
                                         resolve(step["loop_call_later"]))
            time.sleep(0.05)
        elif "bg_thread" in step:
            # A poller: an app loop running since before the test, ticking
            # now and then (it may be idle in the gap between tests).
            t = threading.Thread(target=resolve(step["bg_thread"]), name="poller", daemon=True)
            t.start()
        elif step.get("fork_in_flight"):
            # A command half sent: its handler waits in readline, its
            # connection open, when the process forks (a gunicorn master
            # forking a worker while it answers keploy).
            import socket as socketmod
            c = socketmod.socket(socketmod.AF_UNIX)
            c.connect(case["sock"])
            c.sendall(b"STA")
            deadline = time.monotonic() + 10
            while not ka._open_conns and time.monotonic() < deadline:
                time.sleep(0.01)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.write(w, json.dumps({"collector": ka._collector is not None,
                                        "current": ka.current_id,
                                        "conns": len(ka._open_conns)}).encode())
                os.close(w)
                time.sleep(30)
                os._exit(0)
            os.close(w)
            data = b""
            while True:
                chunk = os.read(r, 4096)
                if not chunk:
                    break
                data += chunk
            out["forked_in_flight"] = json.loads(data)
            workers.append(pid)
            c.sendall(b"RT s/t9\n")
            c.settimeout(5)
            reply, eof = b"", False
            try:
                while True:
                    chunk = c.recv(256)
                    if not chunk:
                        eof = True
                        break
                    reply += chunk
            except socketmod.timeout:
                pass
            c.close()
            out["in_flight_answer"] = [reply.decode(), eof]
        elif step.get("touch_app"):
            # The bytes stay; the mtimes move past any clock (keploy's time
            # freeze sets the app's clock back to the recording's).
            for d, _, fs in os.walk(case["app"]):
                for f in fs:
                    if f.endswith(".py"):
                        t = time.time() + 86400
                        os.utime(os.path.join(d, f), (t, t))
        elif step.get("stub") == "rel_fails":
            # The file rule raises for files not seen yet: the callback must
            # swallow it, and keploy says so later.
            def boom(filename):
                raise RuntimeError("rel failed")
            REAL_COMPUTE.append(ka._app._compute)
            ka._app._compute = boom
            mod = step.get("module", "late")
            os.makedirs(os.path.join(case["app"], mod), exist_ok=True)
            with open(os.path.join(case["app"], mod, "__init__.py"), "w") as f:
                f.write("def f():\n    return 1\n")
            out["late"] = importlib.import_module(mod).f()
        elif step.get("stub") == "rel_ok":
            del ka._app._compute
        elif step.get("fork_worker") == "stop":
            for pid in workers:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            workers.clear()
        elif step.get("fork_exit"):
            # A forked child that exits as a recycled worker does: sys.exit,
            # which runs the atexit handlers it inherited.
            pid = os.fork()
            if pid == 0:
                CHILD = True
                sys.stdout = open(os.devnull, "w")
                sys.exit(0)
            os.waitpid(pid, 0)
            out["sock_after_child_exit"] = os.path.exists(case["sock"])
        elif step.get("settrace_other"):
            # A debugger or profiler started after the agent (before 3.12 it
            # replaces keploy's tracer).
            def other(frame, event, arg):
                return None
            threading.settrace(other)
            sys.settrace(other)
        elif "frame_answer" in step:
            fake.frame_answer = step["frame_answer"].encode()
        elif "max_payload" in step:
            sys.modules["keploy_agent._table"].MAX_PAYLOAD = step["max_payload"]
        elif step.get("rebind"):
            time.sleep(0.2)   # the INDEX of the socket before has gone
            fake.bind()
        elif step.get("wait") == "table_idle":
            deadline = time.monotonic() + 30
            while ka._table_busy and time.monotonic() < deadline:
                time.sleep(0.02)
        elif "wait" in step:
            key = {"frames": "parts", "records": "records"}[step["wait"]]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with lock:
                    if len(out[key]) >= step["n"]:
                        break
                time.sleep(0.02)

    for step in case["steps"]:
        run_step(step)
    time.sleep(0.2)


CHILD = False
workers = []
PAIRS = []
LOOP = []
SHARED = []
WSGI = []
REAL_COMPUTE = []

try:
    main()
except SystemExit:
    if not CHILD:
        out["errors"].append("SystemExit")
except BaseException as e:
    out["errors"].append(repr(e))
finally:
    if not CHILD:
        for pid in workers:
            os.kill(pid, 9)
        with lock:
            sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
# A forked child (fork_exit) leaves here and exits as Python does, atexit
# handlers and all.
sys.exit(0)
