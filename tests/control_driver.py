"""Runs one control-socket case against keploy_agent in its own process.

keploy_agent sets a global tracer and starts a non-daemon thread when it is
imported, so each case runs here, in a child of the test, and ends with
os._exit. The import binds the agent's real control socket path
(/tmp/coverage_control.sock): do not run these rows while keploy is running
on the same machine.

argv[1] is the case as JSON:
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


SOCK = "/tmp/coverage_control.sock"


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
    import keploy_agent as ka

    emitted = []

    def emit_ok(test_id):
        emitted.append(test_id)

    def emit_fails(test_id):
        emitted.append(test_id)
        raise ConnectionRefusedError(111, "no data socket")

    class FailingTracer:
        def results(self):
            raise RuntimeError("counts not reset")

    def run_raises(cmd):
        raise ValueError("run failed")

    ka._emit = emit_ok
    got = []
    errors = []
    for step in case["steps"]:
        stub = step.get("stub")
        if stub == "reset_fails":
            ka._tracer = FailingTracer()
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
    # A socket file left by an app that ended: the import replaces it.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.remove(SOCK)
    except FileNotFoundError:
        pass
    stale.bind(SOCK)
    stale.close()
    held = []
    start = threading.Thread.start
    threading.Thread.start = lambda self: held.append(self)
    try:
        import keploy_agent as ka
    finally:
        threading.Thread.start = start
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(ka.CONTROL_SOCK)
    except OSError as e:
        out["connect_error"] = repr(e)
        return
    out["held"] = [t.name for t in held]
    for t in held:
        t.start()
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
        import keploy_agent as ka
    except BaseException as e:
        out["import_error"] = repr(e)
        return
    out["closed"] = len(closed)
    out["srv"] = hasattr(ka, "_srv")
    out["threads"] = [t.name for t in threading.enumerate()]


try:
    {"answers": answers,
     "bind_before_ready": bind_before_ready,
     "bind_fails": bind_fails}[case["mode"]]()
finally:
    finish()
