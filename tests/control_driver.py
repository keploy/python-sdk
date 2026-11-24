"""Runs one control-socket case against keploy_agent in its own process.

keploy_agent sets a global tracer and starts a non-daemon thread when it is
imported, so each case runs here, in a child of the test, and ends with
os._exit. The import binds the agent's control socket: here at the case's own
path (import_agent), never /tmp/coverage_control.sock, which a
Keploy-instrumented app on the same machine may be serving.

argv[1] is the case as JSON, with "sock", the control socket path the import
binds, and one of:
  {"mode": "answers", "steps": [...]}
      Imports keploy_agent, then runs each step on a socketpair through
      keploy_agent._handle. A step is one of:
        {"send": "START t1\\n"}   the bytes sent (surrogateescape); the
                                  answer is recorded
        {"send": null}            the peer closes with nothing sent
        {"silent": true}          nothing sent, the read times out
        {"stub": NAME}            reset_fails, emit_fails, emit_ok, run_raises
  {"mode": "bind_before_ready"}
      Leaves a stale socket file at the path, holds every thread the import
      starts, connects to the control socket as soon as the import returns,
      then lets the threads run and sends START.
  {"mode": "bind_fails"}
      Imports keploy_agent with every socket's listen failing.
Prints one JSON line on stdout with what it saw.
"""

import json
import os
import socket
import sys
import threading

case = json.loads(sys.argv[1])
out = {}


# The dir of the control sockets (KEPLOY_COVERAGE_CONTROL_DIR, set by the
# test): this process binds <its pid>.sock there.
SOCK_DIR = case["sock_dir"]
_ns = os.readlink("/proc/self/ns/pid")
SOCK = os.path.join(SOCK_DIR, f"{_ns[_ns.find('[') + 1:_ns.rfind(']')]}-{os.getpid()}.sock")

# The line of keploy_agent that names its data socket.
DATA_LINE = 'DATA_SOCK = "/tmp/coverage_data.sock"'
# The data socket: never /tmp/coverage_data.sock, a keploy agent's on a
# machine that runs one. No one listens here unless a case starts a receiver.
DATA = case.get("data") or os.path.join(os.path.dirname(SOCK_DIR), "data.sock")


def import_agent():
    """Imports keploy_agent as an app does, from its source with DATA_LINE
    alone changed to name DATA, so the import binds SOCK (in SOCK_DIR, by
    its environment) and sends to DATA."""
    import importlib.util

    spec = importlib.util.find_spec("keploy_agent")
    src = spec.loader.get_source("keploy_agent")
    if src.count(DATA_LINE) != 1:
        raise SystemExit(f"keploy_agent does not have the line {DATA_LINE!r} once")
    src = src.replace(DATA_LINE, f"DATA_SOCK = {DATA!r}")
    ka = importlib.util.module_from_spec(spec)
    sys.modules["keploy_agent"] = ka
    exec(compile(src, spec.origin, "exec"), ka.__dict__)
    out["sock"] = ka.CONTROL_SOCK
    return ka


def finish():
    srv = getattr(sys.modules.get("keploy_agent"), "_srv", None)
    if srv is not None:
        srv.close()
        try:
            os.remove(SOCK)
        except OSError:
            pass
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def answers():
    ka = import_agent()

    emitted = []

    def emit_ok(test_id):
        emitted.append(test_id)

    def emit_fails(test_id):
        emitted.append(test_id)
        raise ConnectionRefusedError(111, "no data socket")

    def reset_fails():
        raise RuntimeError("lines not reset")

    def run_raises(cmd):
        raise ValueError("run failed")

    ka._emit = emit_ok
    got = []
    errors = []
    for step in case["steps"]:
        stub = step.get("stub")
        if stub == "reset_fails":
            ka._collector.swap = reset_fails
        elif stub == "emit_fails":
            ka._emit = emit_fails
        elif stub == "emit_ok":
            ka._emit = emit_ok
        elif stub == "run_raises":
            ka._run = run_raises
        elif stub:
            raise SystemExit(f"unknown stub {stub}")
        if stub:
            continue
        peer, conn = socket.socketpair()
        if step.get("silent"):
            conn.settimeout(0.05)
        elif step["send"] is None:
            peer.shutdown(socket.SHUT_WR)
        else:
            peer.sendall(step["send"].encode("utf-8", "surrogateescape"))
        try:
            ka._handle(conn)
        except BaseException as e:
            errors.append(repr(e))
        reply = b""
        while True:
            chunk = peer.recv(256)
            if not chunk:
                break
            reply += chunk
        peer.close()
        got.append(reply.decode())
    out["answers"] = got
    out["errors"] = errors
    out["emitted"] = emitted
    out["current_id"] = ka.current_id


def bind_before_ready():
    # A socket file left by an app that ended: the import replaces it. The
    # dir is made as the agent makes it (1777).
    os.makedirs(SOCK_DIR, exist_ok=True)
    os.chmod(SOCK_DIR, 0o1777)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.remove(SOCK)
    except FileNotFoundError:
        pass
    stale.bind(SOCK)
    stale.close()
    # The agent's threads are real OS threads (_thread, never gevent's):
    # hold every one the import starts.
    import _thread
    held = []
    start = _thread.start_new_thread
    _thread.start_new_thread = lambda fn, args, *kw: held.append((fn, args))
    try:
        ka = import_agent()
    finally:
        _thread.start_new_thread = start
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(ka.CONTROL_SOCK)
    except OSError as e:
        out["connect_error"] = repr(e)
        return
    out["held"] = [fn.__name__ for fn, _ in held]
    # The agent saved the holder as its thread starter: give it the real one.
    ka._start_new_thread = start
    for fn, args in held:
        start(fn, args)
    c.settimeout(10)
    c.sendall(b"START t1\n")
    reply = b""
    while True:
        chunk = c.recv(256)
        if not chunk:
            break
        reply += chunk
    c.close()
    out["answer"] = reply.decode()


def bind_fails():
    closed = []
    close = socket.socket.close

    def listen(self, *args):
        raise OSError(98, "Address already in use")

    def record_close(self):
        if self.family == socket.AF_UNIX:
            closed.append(True)
        close(self)

    socket.socket.listen = listen
    socket.socket.close = record_close
    try:
        ka = import_agent()
    except BaseException as e:
        out["import_error"] = repr(e)
        return
    out["closed"] = len(closed)
    out["srv"] = getattr(ka, "_srv", None) is not None
    out["threads"] = [t.name for t in threading.enumerate()]
    out["server_thread"] = getattr(ka, "_srv", None) is not None


try:
    {"answers": answers,
     "bind_before_ready": bind_before_ready,
     "bind_fails": bind_fails}[case["mode"]]()
finally:
    finish()
