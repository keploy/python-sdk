"""Rows for the ways apps are started: `python app.py` (the README's own
example: the agent imported by the app, the server run from its <module>),
a manage.py whose main() keeps a reloader loop on the main thread, and
`python -m pkg`; each with the explicit import and with the PYTHONPATH hook.
Each runs START, one HTTP request, END against the real process. Also the
request still being served at END (B2): a WSGI response whose close() runs
app code after the client has its answer."""

import http.client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "keploy_agent"

VIEWS = '''\
import time


def view():
    x = 1
    return x + 1


def finished():
    y = 2
    return y


class Body:
    """A body whose close() runs app code after the answer was sent (as
    Django's request_finished receivers do)."""

    def __init__(self, delay):
        self.delay = delay

    def __iter__(self):
        yield b"ok"

    def close(self):
        if self.delay:
            time.sleep(self.delay)
            finished()


def app(environ, start_response):
    view()
    delay = float(environ.get("QUERY_STRING") or 0)
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return Body(delay)
'''

SERVER = '''\
import os
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server, WSGIRequestHandler


class Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


class TS(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def serve(app, threaded):
    make_server("127.0.0.1", int(os.environ["PORT"]), app, server_class=TS if threaded else WSGIServer,
                handler_class=Quiet).serve_forever()
'''

# A reloader outside the app (as Django's autoreload is): the main thread
# loops in it under manage.py's <module> and main(), the server on a thread.
RELOADER = '''\
import os
import threading
import time


def run_with_reloader(fn):
    threading.Thread(target=fn, daemon=True, name="django-main-thread").start()
    while True:
        time.sleep(0.02)
        os.stat(__file__)
'''

IMPORT = "import keploy_agent  # noqa: F401  (the README's explicit import)\n"

LAUNCHERS = {
    # name: (files, argv after python)
    "app_py_single": ({"app.py": "{imp}from views import app\nfrom srv import serve\n\nserve(app, False)\n"},
                      ["app.py"]),
    "app_py_threaded": ({"app.py": "{imp}from views import app\nfrom srv import serve\n\nserve(app, True)\n"},
                        ["app.py"]),
    "manage_py": ({"manage.py": textwrap.dedent('''\
        {imp}import sys
        from reloader import run_with_reloader


        def main():
            from views import app
            from srv import serve
            run_with_reloader(lambda: serve(app, True))


        if __name__ == "__main__":
            main()
        ''')}, ["manage.py", "runserver"]),
    "python_m_pkg": ({"pkg/__init__.py": "",
                      "pkg/__main__.py": textwrap.dedent('''\
        {imp}from views import app
        from srv import serve


        def main():
            serve(app, True)


        main()
        ''')}, ["-m", "pkg"]),
}


def fake_data(path, recs):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(64)

    def one(c):
        data = b""
        while True:
            ch = c.recv(65536)
            if not ch:
                break
            data += ch
        c.close()
        try:
            recs.append(json.loads(data))
        except Exception:
            pass        # a table frame: not read here

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv


def cmd(sock, c):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(sock)
    s.sendall(c.encode() + b"\n")
    r = s.makefile().readline().strip()
    s.close()
    return r


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(port, q=""):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/?" + q if q else "/")
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def private_src(d):
    src = pathlib.Path(d) / "pkgsrc"
    shutil.copytree(SRC, src / "keploy_agent")
    init = src / "keploy_agent" / "__init__.py"
    text = init.read_text()
    line = 'DATA_SOCK = "/tmp/coverage_data.sock"'
    assert text.count(line) == 1
    init.write_text(text.replace(line, f"DATA_SOCK = {d + '/d.sock'!r}"))
    return src


def launch(launcher, mode, tests, query=""):
    """Runs the app under one launcher; returns {test id: record}, the
    answers and the app's log."""
    d = tempfile.mkdtemp(prefix="kl", dir="/tmp")
    try:
        src = private_src(d)
        app, lib = pathlib.Path(d) / "app", pathlib.Path(d) / "lib"
        app.mkdir()
        lib.mkdir()
        (app / "views.py").write_text(VIEWS)
        (lib / "srv.py").write_text(SERVER)
        (lib / "reloader.py").write_text(RELOADER)
        files, argv = LAUNCHERS[launcher]
        for name, text in files.items():
            (app / name).parent.mkdir(parents=True, exist_ok=True)
            (app / name).write_text(text.replace("{imp}", IMPORT if mode == "explicit" else ""))
        ctl = os.path.join(d, "ctl")
        os.mkdir(ctl)
        os.chmod(ctl, 0o1777)
        recs = []
        srv = fake_data(os.path.join(d, "d.sock"), recs)
        port = free_port()
        env = dict(os.environ, KEPLOY_COVERAGE_CONTROL_DIR=ctl, KEPLOY_APP_SOURCE_DIR=str(app), PORT=str(port))
        env.pop("KEPLOY_AGENT_FIRST_PID", None)
        path = [str(src), str(lib)]
        if mode == "hook":
            path.insert(0, str(src / "keploy_agent" / "_boot"))
        env["PYTHONPATH"] = os.pathsep.join(path)
        err = open(os.path.join(d, "err"), "w+")
        p = subprocess.Popen([sys.executable] + argv, cwd=str(app), env=env, stdout=err, stderr=err,
                             start_new_session=True)
        try:
            sock, deadline = None, time.monotonic() + 30
            while time.monotonic() < deadline and p.poll() is None:
                socks = [n for n in os.listdir(ctl) if n.endswith(".sock")]
                if socks:
                    try:
                        get(port)
                        sock = os.path.join(ctl, socks[0])
                        break
                    except OSError:
                        pass
                time.sleep(0.05)
            err.seek(0)
            assert sock, f"no socket/port; log: {err.read()[-1500:]}"
            answers = []
            for t in tests:
                answers.append(cmd(sock, f"START s/{t}"))
                get(port, query)
                answers.append(cmd(sock, f"END s/{t}"))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and sum(1 for r in recs if "id" in r) < len(tests):
                time.sleep(0.05)
            err.seek(0)
            return {r["id"]: r for r in recs if "id" in r}, answers, err.read()
        finally:
            os.killpg(p.pid, 9)
            p.wait()
            srv.close()
            err.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("mode", ["explicit", "hook"])
@pytest.mark.parametrize("launcher", sorted(LAUNCHERS))
def test_a_launchers_frames_are_no_tests_work(launcher, mode):
    got, answers, log = launch(launcher, mode, ["t1", "t2", "t3"])
    assert answers == ["ACK proto=2"] * 6, (answers, log[-1500:])
    assert sorted(got) == ["s/t1", "s/t2", "s/t3"], log[-1500:]
    for t, r in sorted(got.items()):
        assert "partial" not in r, (t, r.get("partial"), log[-1500:])
        lines = r["executedLinesByFile"]
        assert "views.py" in lines and 5 in lines["views.py"], (t, lines)


def test_a_request_finishing_after_its_answer_is_waited_for():
    # close() runs app code 20 ms after the client has its answer: END waits
    # for the entry to return, and the lines are the test's.
    got, answers, log = launch("app_py_threaded", "explicit", ["t1", "t2"], query="0.02")
    assert answers == ["ACK proto=2"] * 4, (answers, log[-1500:])
    for t, r in sorted(got.items()):
        assert "partial" not in r, (t, r.get("partial"))
        assert 10 in r["executedLinesByFile"]["views.py"], (t, r["executedLinesByFile"])


def test_a_request_still_served_past_the_wait_is_partial():
    got, answers, log = launch("app_py_threaded", "hook", ["t1"], query="0.3")
    assert answers == ["ACK proto=2"] * 2, (answers, log[-1500:])
    assert "the test's request was still being served at its end" in got["s/t1"].get("partial", ""), got
