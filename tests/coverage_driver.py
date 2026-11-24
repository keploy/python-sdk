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


def resolve(ref):
    mod, _, fn = ref.partition(":")
    return getattr(importlib.import_module(mod), fn)


def import_agent(sock, data):
    """Imports keploy_agent from its source with its two socket lines
    changed to name sock and data (as control_driver does)."""
    import importlib.util

    spec = importlib.util.find_spec("keploy_agent")
    src = spec.loader.get_source("keploy_agent")
    for line, path, name in (('CONTROL_SOCK = "/tmp/coverage_control.sock"', sock, "CONTROL_SOCK"),
                             ('DATA_SOCK = "/tmp/coverage_data.sock"', data, "DATA_SOCK")):
        if src.count(line) != 1:
            raise SystemExit(f"keploy_agent does not have the line {line!r} once")
        src = src.replace(line, f"{name} = {path!r}")
    ka = importlib.util.module_from_spec(spec)
    sys.modules["keploy_agent"] = ka
    exec(compile(src, spec.origin, "exec"), ka.__dict__)
    return ka


def main():
    fake = FakeAgent(case["data"], out, lock)
    ka = import_agent(case["sock"], case["data"])
    out["producer"] = ka._collector.name if ka._collector else None
    out["driver_pid"] = os.getpid()
    for step in case["steps"]:
        if "cmd" in step:
            out["answers"].append(answer(case["sock"], step["cmd"]))
        elif "call" in step:
            resolve(step["call"])()
        elif "thread" in step:
            t = threading.Thread(target=resolve(step["thread"]))
            t.start()
            t.join()
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
                os.write(w, json.dumps({"collector": ka._collector is not None,
                                        "why": ka._unmeasured}).encode())
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
        elif step.get("fork_worker") == "start":
            # A prefork worker: a live child that serves (here: sleeps).
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            workers.append(pid)
        elif "fork_yielders" in step:
            # Children of this process that ask for its socket at once, each
            # on its own connection, and never bind it.
            pipes = []
            for _ in range(step["fork_yielders"]):
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(r)
                    a = answer(case["sock"], f"YIELD {os.getpid()}")
                    os.write(w, a.encode())
                    os.close(w)
                    time.sleep(60)
                    os._exit(0)
                os.close(w)
                workers.append(pid)
                pipes.append(r)
            got = []
            for r in pipes:
                data = b""
                while True:
                    chunk = os.read(r, 4096)
                    if not chunk:
                        break
                    data += chunk
                got.append(data.decode())
            out["yields"] = sorted(got)
        elif "wait_s" in step:
            time.sleep(step["wait_s"])
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
            tool = getattr(ka._collector, "tool", None)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                if sys.version_info >= (3, 12):
                    tracer = sys.monitoring.get_tool(tool) is not None
                else:
                    tracer = sys.gettrace() is not None or (
                        getattr(threading, "gettrace", lambda: threading._trace_hook)() is not None)
                os.write(w, json.dumps({"collector": ka._collector is not None, "tracer": tracer,
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
        elif "handover_wait" in step:
            ka.HANDOVER_WAIT = step["handover_wait"]
        elif step.get("ancestors"):
            chain = {5: 1, 1: 0}
            out["ancestors"] = [sorted(ka._ancestors(start=5, read_ppid=chain.get)),
                                sorted(ka._ancestors(start=1, read_ppid=chain.get))]
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
            ka._app._compute = boom
            os.makedirs(os.path.join(case["app"], "late"), exist_ok=True)
            with open(os.path.join(case["app"], "late", "__init__.py"), "w") as f:
                f.write("def f():\n    return 1\n")
            import late
            out["late"] = late.f()
        elif step.get("bind_race"):
            # An owner that answers YIELD ACK and, before this process
            # replaces its file, another process wins the path: that one's
            # socket must stay.
            import socket as socketmod
            path = case["sock"] + ".race"
            owner = socketmod.socket(socketmod.AF_UNIX)
            owner.bind(path)
            owner.listen()
            winner = []

            def serve_owner():
                c, _ = owner.accept()
                c.recv(256)
                os.remove(path)
                w = socketmod.socket(socketmod.AF_UNIX)
                w.bind(path)
                w.listen()
                winner.append((w, ka._ident(path)))
                c.sendall(b"ACK proto=2\n")
                c.close()

            threading.Thread(target=serve_owner, daemon=True).start()
            ka._ancestors = lambda: {os.getpid()}
            try:
                ka._bind(path)
                out["bind_race"] = "bound"
            except ka._Served:
                out["bind_race"] = "served"
            out["winner_kept"] = bool(winner) and ka._ident(path) == winner[0][1]
        elif step.get("bind_lock"):
            # path.lock held by another: the bind waits for it.
            import fcntl
            path = case["sock"] + ".locked"
            fd = os.open(path + ".lock", os.O_RDONLY | os.O_CREAT, 0o666)
            fcntl.flock(fd, fcntl.LOCK_EX)
            threading.Timer(0.5, os.close, args=(fd,)).start()
            t0 = time.monotonic()
            srv = ka._bind(path)
            out["bind_waited"] = round(time.monotonic() - t0, 2)
            srv.close()
        elif step.get("fork_worker") == "stop":
            for pid in workers:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            workers.clear()
        elif step.get("fork_exit"):
            # A forked child that exits as a recycled worker does: sys.exit,
            # which runs the atexit handlers it inherited.
            global CHILD
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
    time.sleep(0.2)


CHILD = False
workers = []

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
