"""Unit rows of the wire client (agent.py) and the joining rule (join.py)."""
import errno
import json
import os
import re
import socket
import subprocess
import sys
import threading

import pytest

from keploy_pytest.agent import MAX_ANSWER, AgentClient, KeployError, quoted
from keploy_pytest.join import Active, decide, runs_inside_proc
from keploy_pytest.plugin import keploy_text, outcome_of, worst, xdist_controller


def error_of(fn, *args):
    with pytest.raises(KeployError) as e:
        fn(*args)
    return e.value


@pytest.fixture
def raw_agent():
    """An agent that answers one call with the given bytes as they are, then
    closes the connection: an answer no well-behaved server sends."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threads = []

    def answer(data):
        def serve():
            conn, _ = listener.accept()
            with conn:
                got = b""
                while b"\r\n\r\n" not in got:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    got += chunk
                head, _, body = got.partition(b"\r\n\r\n")
                n = int(re.search(rb"(?i)\r\ncontent-length: *([0-9]+)", head).group(1))
                while len(body) < n:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    body += chunk
                conn.sendall(data)
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        threads.append(t)
        return "http://127.0.0.1:%d" % listener.getsockname()[1]

    yield answer
    # Ends a thread still waiting for its call: accept fails once the
    # listener is shut.
    listener.shutdown(socket.SHUT_RDWR)
    listener.close()
    for t in threads:
        t.join()


def test_answers_that_cannot_be_trusted(agent):
    c = AgentClient(agent.url, "")
    cases = [
        ("[1]", 'keploy: the keploy agent\'s answer to /agent/scope/begin cannot be read (not an object): "[1]"'),
        ("not json", "keploy: the keploy agent's answer to /agent/scope/begin cannot be read ("),
        ('{"v": true, "status": "ok"}', "keploy: the keploy agent answered /agent/scope/begin in scope protocol vtrue ("),
        ('{"v": "1", "status": "ok"}', 'keploy: the keploy agent answered /agent/scope/begin in scope protocol v"1" ('),
        ('{"v": null}', "keploy: the keploy agent answered /agent/scope/begin with "),
    ]
    for text, want in cases:
        agent.script["/agent/scope/begin"] = lambda body, n, text=text: (200, text)
        e = error_of(c.begin, {"name": "t", "file": "f.py"})
        assert str(e).startswith(want), (text, str(e))
        assert e.status == 200


def test_error_bodies(agent):
    c = AgentClient(agent.url, "")
    agent.script["/agent/scope/end"] = lambda body, n: (409, json.dumps({"error": "not_open", "message": "no test is open", "remedy": ""}))
    e = error_of(c.end, {"name": "t", "file": "f.py"}, "passed")
    assert (str(e), e.status, e.code) == ("keploy: no test is open", 409, "not_open")
    # A code or remedy that is no string is not the protocol's: neither is
    # quoted.
    agent.script["/agent/scope/end"] = lambda body, n: (409, json.dumps({"error": 7, "message": "odd code", "remedy": 5}))
    e = error_of(c.end, {"name": "t", "file": "f.py"}, "passed")
    assert (str(e), e.code) == ("keploy: odd code", "")
    agent.script["/agent/scope/end"] = lambda body, n: (400, json.dumps({"message": ""}))
    e = error_of(c.end, {"name": "t", "file": "f.py"}, "passed")
    assert str(e) == 'keploy: the keploy agent answered 400 Bad Request to /agent/scope/end: "{\\"message\\": \\"\\"}"'
    # A 404 with the protocol's error body is the agent's refusal, not a
    # missing route.
    agent.script["/agent/instance"] = lambda body, n: (404, json.dumps({"error": "no_run", "message": "no run"}))
    e = error_of(c.claim, ".", "/p")
    assert (str(e), e.status, e.code) == ("keploy: no run", 404, "no_run")
    # Only a plain 404 names the switch.
    agent.script["/agent/instance"] = lambda body, n: (503, "busy")
    e = error_of(c.claim, ".", "/p")
    assert (str(e), e.status) == ('keploy: the keploy agent answered 503 Service Unavailable to /agent/instance: "busy"', 503)
    # The begin and end bodies.
    agent.script["/agent/scope/end"] = lambda body, n: (200, '{"v": 1, "status": "ok"}')
    c.begin({"name": "t", "file": "f.py"})
    c.end({"name": "t", "file": "f.py"}, "skipped")
    assert [b for p, b, _ in agent.calls[-2:]] == [{"name": "t", "file": "f.py", "canSkip": True},
                                                  {"name": "t", "file": "f.py", "outcome": "skipped"}]
    assert "Authorization" not in agent.calls[-1][2]


def test_status_2xx_bounds(agent):
    c = AgentClient(agent.url, "")
    agent.script["/agent/scope/begin"] = lambda body, n: (299, '{"v": 1, "status": "ok"}')
    assert c.begin({"name": "t", "file": "f.py"})["status"] == "ok"
    agent.script["/agent/scope/begin"] = lambda body, n: (300, '{"v": 1, "status": "ok"}')
    assert error_of(c.begin, {"name": "t", "file": "f.py"}).status == 300
    # A redirect is refused, not followed (a 303 would be followed as a GET
    # of its location, which this fake answers 501).
    agent.script["/agent/scope/begin"] = lambda body, n: (303, '{"v": 1, "status": "ok"}')
    e = error_of(c.begin, {"name": "t", "file": "f.py"})
    assert (str(e), e.status) == ('keploy: the keploy agent answered 303 See Other to /agent/scope/begin: "{\\"v\\": 1, \\"status\\": \\"ok\\"}"', 303)
    agent.script["/agent/scope/begin"] = lambda body, n: (199, '{"v": 1, "status": "ok"}')
    # 1xx is no final answer: http.client reads no body after it, and the
    # status is what the error names.
    e = error_of(c.begin, {"name": "t", "file": "f.py"})
    assert (str(e), e.status) == ('keploy: the keploy agent answered 199  to /agent/scope/begin: ""', 199)


def test_bounded_answer(agent):
    c = AgentClient(agent.url, "")
    big = json.dumps({"v": 1, "status": "ok", "pad": "x" * MAX_ANSWER})
    agent.script["/agent/scope/begin"] = lambda body, n: (200, big)
    e = error_of(c.begin, {"name": "t", "file": "f.py"})
    assert str(e) == "keploy: cannot read the keploy agent's answer to /agent/scope/begin: the answer is larger than %d bytes" % MAX_ANSWER
    exact = json.dumps({"v": 1, "status": "ok", "pad": ""})
    exact = exact[:-2] + "x" * (MAX_ANSWER - len(exact)) + exact[-2:]
    assert len(exact) == MAX_ANSWER
    agent.script["/agent/scope/begin"] = lambda body, n: (200, exact)
    assert c.begin({"name": "t", "file": "f.py"})["status"] == "ok"


def test_bounded_read(raw_agent):
    # An answer that says it is longer than the bound is read only up to
    # the bound, not to the end it names: past the bound the client stops
    # and says so (this one ends early, which a client reading on would
    # name instead).
    url = raw_agent(b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n" % (2 * MAX_ANSWER) + b"x" * (MAX_ANSWER + 1))
    e = error_of(AgentClient(url, "").begin, {"name": "t", "file": "f.py"})
    assert str(e) == "keploy: cannot read the keploy agent's answer to /agent/scope/begin: the answer is larger than %d bytes" % MAX_ANSWER


def test_unreachable_agent():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    e = error_of(AgentClient("http://127.0.0.1:%d" % port, "").begin, {"name": "t", "file": "f.py"})
    # The cause as the system names it, without urllib's wrapping.
    refused = ConnectionRefusedError(errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED))
    assert str(e) == "keploy: cannot reach the keploy agent at http://127.0.0.1:%d: %s" % (port, refused)
    assert e.status == 0


def test_quoted():
    assert quoted("  a  ") == '"a"'
    assert quoted("x" * 512) == json.dumps("x" * 512)
    assert quoted("x" * 513) == json.dumps("x" * 512) + "..."


def test_decide():
    never = lambda pid: False  # noqa: E731
    always = lambda pid: True  # noqa: E731
    assert decide({}, 10).mode == "inert"
    assert decide({"KEPLOY_MOCK_AGENT": "  "}, 10).mode == "inert"
    assert decide({"KEPLOY_MOCK_AGENT": "Off"}, 10).mode == "inert"
    for bad in ("localhost:1", "ftp://h:1", "http://", "http://h:port", "not a url"):
        j = decide({"KEPLOY_MOCK_AGENT": bad}, 10)
        assert j.mode == "refused", bad
        assert j.refusal == "keploy: KEPLOY_MOCK_AGENT=%s is neither off nor the http URL of a keploy agent; set it to off to run without keploy" % json.dumps(bad)
    j = decide({"KEPLOY_MOCK_AGENT": " https://h:1// ", "KEPLOY_MOCK_AGENT_TOKEN": "t", "KEPLOY_MOCK_PROJECT": " /p "}, 10)
    assert isinstance(j, Active) and (j.url, j.token, j.project) == ("https://h:1", "t", "/p")
    env = {"KEPLOY_MOCK_AGENT": "http://h:1"}
    assert decide(dict(env, KEPLOY_TEST_PID="7"), 10, always).mode == "inert"
    assert decide(dict(env, KEPLOY_TEST_PID="7"), 10, never).mode == "active"
    # The marker naming this process, or no process, is no membership.
    assert decide(dict(env, KEPLOY_TEST_PID="10"), 10, always).mode == "active"
    assert decide(dict(env, KEPLOY_TEST_PID="0"), 10, always).mode == "active"
    assert decide(dict(env, KEPLOY_TEST_PID="-3"), 10, always).mode == "active"
    assert decide(dict(env, KEPLOY_TEST_PID="x"), 10, always).mode == "active"
    assert decide(dict(env, KEPLOY_TEST_PID=" 7 "), 10, always).mode == "inert"


def test_runs_inside_proc(monkeypatch):
    assert runs_inside_proc(os.getppid())
    assert runs_inside_proc(1) is (os.getpid() != 1)
    assert not runs_inside_proc(os.getpid())
    assert not runs_inside_proc(2 ** 22 + 7)
    # A grandchild, under a shell that stays its parent, runs inside this
    # process: read through the shell's /proc entry.
    code = "import sys; from keploy_pytest.join import runs_inside_proc as r; print(r(int(sys.argv[1])), r(2 ** 22 + 7))"
    out = subprocess.run(["sh", "-c", '"$0" -c "$1" "$2"; true', sys.executable, code, str(os.getpid())],
                         check=True, capture_output=True, text=True).stdout
    assert out == "True False\n"
    # A command name may hold ") " and what looks like the fields after it:
    # the parent is read after the last ')'. The grandchild runs under a
    # process named so.
    code = ("import subprocess, sys\n"
            "open('/proc/self/comm', 'w').write('k) S 1 1')\n"
            "subprocess.run([sys.executable, '-c', 'import sys; from keploy_pytest.join import runs_inside_proc as r; "
            "print(r(int(sys.argv[1])))', sys.argv[1]], check=True)\n")
    out = subprocess.run([sys.executable, "-c", code, str(os.getpid())], check=True, capture_output=True, text=True).stdout
    assert out == "True\n"
    # Without /proc it cannot tell, and takes the marker at its word.
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/proc/self/stat" else real(p))
    assert runs_inside_proc(2 ** 22 + 7)


def test_texts_and_outcomes():
    assert keploy_text(None, "fb") == "fb"
    assert keploy_text("", "fb") == "fb"
    assert keploy_text("keploy: x", "fb") == "keploy: x"
    assert keploy_text("quarantined/flaky: x", "fb") == "keploy: quarantined/flaky: x"
    assert worst(None, "passed") == "passed"
    assert worst("passed", "skipped") == "skipped"
    assert worst("skipped", "passed") == "skipped"
    assert worst("failed", "skipped") == "failed"
    assert worst("skipped", "failed") == "failed"

    class Rep:
        def __init__(self, outcome):
            self.outcome = outcome
    assert [outcome_of(Rep(o)) for o in ("passed", "failed", "skipped", "rerun")] == ["passed", "failed", "skipped", "failed"]


def test_xdist_controller_is_xdists_rule():
    # xdist.is_xdist_controller: not a worker, and distributing. A worker is
    # never the controller, whatever its dist option says.
    import xdist

    class Config:
        def __init__(self, dist, worker):
            self.option = type("Option", (), {"dist": dist})()
            if worker:
                self.workerinput = {}

        def getoption(self, name, default):
            return getattr(self.option, name, default)

    class Session:
        def __init__(self, config):
            self.config = config
    for dist in ("no", "load"):
        for worker in (False, True):
            cfg = Config(dist, worker)
            assert xdist_controller(cfg) is xdist.is_xdist_controller(Session(cfg)), (dist, worker)
    assert [xdist_controller(Config(d, w)) for d, w in (("no", False), ("load", False), ("no", True), ("load", True))] == \
        [False, True, False, False]
