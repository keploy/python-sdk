"""A fake keploy agent for the rows: it answers the scope protocol's three
calls as the rows script them, and logs every call in the order it came,
beside the marks the rows' test files post (POST /mark {"at": ...}), so that a
row reads where each call fell among the suite's own steps."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest_plugins = ["pytester"]


def ok(**extra):
    return 200, json.dumps(dict({"v": 1, "status": "ok"}, **extra))


class FakeAgent:
    def __init__(self):
        self.calls = []  # (path, body, headers)
        self.lock = threading.Lock()
        # path -> fn(body, n) -> (status, text): n counts that path's calls.
        self.script = {
            "/agent/instance": lambda body, n: (200, json.dumps({"v": 1, "instance": "run", "timeFile": ""})),
            "/agent/scope/begin": lambda body, n: ok(),
            "/agent/scope/end": lambda body, n: ok(),
        }
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw) if raw else None
                with agent.lock:
                    n = sum(1 for c in agent.calls if c[0] == self.path)
                    agent.calls.append((self.path, body, dict(self.headers)))
                if self.path == "/mark":
                    status, text = 200, "{}"
                else:
                    status, text = agent.script[self.path](body, n)
                data = text.encode()
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("location", "/elsewhere")
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def log(self):
        """The calls as short lines: claim KEY, begin FILE::NAME, end
        FILE::NAME OUTCOME, or the mark's label."""
        out = []
        for path, body, _ in self.calls:
            if path == "/mark":
                out.append(body["at"])
            elif path == "/agent/instance":
                out.append("claim " + body["key"])
            elif path == "/agent/scope/begin":
                out.append("begin %s::%s" % (body["file"], body["name"]))
            elif path == "/agent/scope/end":
                out.append("end %s::%s %s" % (body["file"], body["name"], body["outcome"]))
        return out


# The helper the rows' test files import to post a mark.
MARK = '''
import json, os, urllib.request

def mark(at):
    req = urllib.request.Request(os.environ["MARK_URL"] + "/mark", data=json.dumps({"at": at}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req).read()
'''


@pytest.fixture
def agent(monkeypatch):
    a = FakeAgent()
    for k in ("KEPLOY_MOCK_AGENT", "KEPLOY_MOCK_AGENT_TOKEN", "KEPLOY_MOCK_PROJECT", "KEPLOY_TEST_PID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MARK_URL", a.url)
    yield a
    a.close()


@pytest.fixture
def suite(pytester, agent, monkeypatch):
    """A pytester project joined to the fake agent: KEPLOY_MOCK_AGENT names
    it, KEPLOY_MOCK_PROJECT the project, and markhelp.py posts marks."""
    monkeypatch.setenv("KEPLOY_MOCK_AGENT", agent.url)
    monkeypatch.setenv("KEPLOY_MOCK_PROJECT", str(pytester.path))
    pytester.makepyfile(markhelp=MARK)
    return pytester
