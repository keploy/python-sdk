"""The client of keploy's agent: scope protocol v1, the same wire go-sdk's
keploytest, @keploy/playwright and @keploy/jest speak (POST /agent/instance,
/agent/scope/begin, /agent/scope/end).

Every answer of the agent carries the protocol version as "v"; an answer
without it, or with another version, is an error, so this package on an old
agent (or the reverse) fails loudly instead of running unjudged. Every way an
answer can fail to be trusted is an error. Nothing is retried, and no clock
decides anything: there is no timeout, as the agent answers once the work it
owes is done.
"""
import http.client
import json
import urllib.error
import urllib.request

PROTOCOL_VERSION = 1

PATH_INSTANCE = "/agent/instance"
PATH_BEGIN = "/agent/scope/begin"
PATH_END = "/agent/scope/end"

ACTION_SKIP = "skip"
ACTION_FAIL = "fail"

# Bounds what is read of one answer, and how much of one an error quotes.
MAX_ANSWER = 1 << 20
MAX_QUOTED = 512


class KeployError(Exception):
    """An answer this package cannot trust, or the agent's own refusal.

    status is the HTTP status of a non-2xx answer (0 when there was none);
    code is the scope protocol's error code of a refusal ("not_open", ...).
    """

    def __init__(self, message, status=0, code=""):
        super().__init__(message)
        self.status = status
        self.code = code


def quoted(text):
    s = text.strip()
    if len(s) > MAX_QUOTED:
        return json.dumps(s[:MAX_QUOTED]) + "..."
    return json.dumps(s)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect is an answer like any other non-2xx: following it would send
    # the call again, or to somewhere else.
    def redirect_request(self, *args, **kwargs):
        return None


# No proxy from the environment (the agent is keploy's own, on this machine),
# and no redirect.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _read_bounded(r):
    data = r.read(MAX_ANSWER + 1)
    if len(data) > MAX_ANSWER:
        raise ValueError("the answer is larger than %d bytes" % MAX_ANSWER)
    return data.decode("utf-8", "replace")


class AgentClient:
    """Talks to one keploy agent."""

    def __init__(self, url, token):
        self.url = url
        self.token = token

    def post(self, path, body):
        """Sends one call and reads its answer: the answer's object."""
        target = self.url.rstrip("/") + path
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = "Bearer " + self.token
        req = urllib.request.Request(target, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            res = _OPENER.open(req)
        except urllib.error.HTTPError as e:
            # A non-2xx answer: urllib raises it, with the answer to read.
            res = e
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise KeployError("keploy: cannot reach the keploy agent at %s: %s" % (self.url, _cause(e))) from e
        status = res.status
        reason = getattr(res, "reason", "") or ""
        try:
            with res:
                data = _read_bounded(res)
        except (http.client.HTTPException, OSError, ValueError) as e:
            raise KeployError("keploy: cannot read the keploy agent's answer to %s: %s" % (path, _cause(e)), status) from e
        if status < 200 or status > 299:
            try:
                e = json.loads(data)
            except ValueError:
                e = None  # not the protocol's error body
            if isinstance(e, dict) and isinstance(e.get("message"), str) and e["message"] != "":
                code = e["error"] if isinstance(e.get("error"), str) else ""
                remedy = " (%s)" % e["remedy"] if isinstance(e.get("remedy"), str) and e["remedy"] != "" else ""
                raise KeployError("keploy: %s%s" % (e["message"], remedy), status, code)
            raise KeployError("keploy: the keploy agent answered %d %s to %s: %s" % (status, reason, path, quoted(data)), status)
        try:
            a = json.loads(data)
        except ValueError as e:
            raise KeployError("keploy: the keploy agent's answer to %s cannot be read (%s): %s" % (path, e, quoted(data)), status) from e
        if not isinstance(a, dict):
            raise KeployError("keploy: the keploy agent's answer to %s cannot be read (not an object): %s" % (path, quoted(data)), status)
        v = a.get("v")
        if v is None:
            raise KeployError(
                "keploy: the keploy agent answered %s with %s, which names no scope protocol version; "
                "this keploy-pytest needs a keploy agent that speaks scope protocol v%d: upgrade keploy"
                % (path, quoted(data), PROTOCOL_VERSION), status)
        # JSON's true is Python's True, which equals 1: only a number is a version.
        if isinstance(v, bool) or v != PROTOCOL_VERSION:
            raise KeployError(
                "keploy: the keploy agent answered %s in scope protocol v%s (%s); "
                "this keploy-pytest speaks scope protocol v%d: use a keploy and a keploy-pytest of the same release"
                % (path, json.dumps(v), quoted(data), PROTOCOL_VERSION), status)
        return a

    def claim(self, key, dir):
        """Claims keploy's run for this test process."""
        try:
            a = self.post(PATH_INSTANCE, {"key": key, "dir": dir})
        except KeployError as e:
            if e.status == 404 and e.code == "":
                # keploy speaks scope protocol v1 only under its proven
                # switch. With it off (keploy's default) the agent has no
                # claim route and answers the mux's plain 404, as a keploy
                # from before the protocol does. The claim is this package's
                # first call, so it is the one that names the switch.
                raise KeployError(
                    "keploy: this keploy agent answered 404 to POST %s, so this pytest process cannot claim keploy's run: "
                    "keploy speaks keploy-pytest's wire, scope protocol v1, only when it runs with KEPLOY_PROVEN=1 "
                    "(KEPLOY_PROVEN=1 keploy mock record|replay -c \"python -m pytest\"); "
                    "a keploy run without KEPLOY_PROVEN=1, or one from before scope protocol v1, answers 404 there"
                    % PATH_INSTANCE, 404) from e
            raise
        if not a.get("instance"):
            raise KeployError("keploy: the keploy agent answered the claim of this test process without an instance")
        # a["timeFile"] names the run's clock when keploy moves it. Python
        # reads the clock through libc, which keploy's preload (inherited
        # from the test command) moves: a pytest process needs no launcher
        # to follow it.
        return a

    def begin(self, test):
        """Asks the agent to open a test (a dict of name and file)."""
        a = self.post(PATH_BEGIN, dict(test, canSkip=True))
        if a.get("status") != "ok":
            raise KeployError("keploy: the keploy agent answered the begin of %s with status %s" % (test["name"], json.dumps(_text(a.get("status")))))
        if a.get("action") and a["action"] != ACTION_SKIP:
            raise KeployError("keploy: the keploy agent answered the begin of %s with the unknown action %s" % (test["name"], json.dumps(a["action"])))
        return a

    def end(self, test, outcome):
        """Tells the agent the test ended, with its outcome."""
        a = self.post(PATH_END, dict(test, outcome=outcome))
        if a.get("status") != "ok":
            raise KeployError("keploy: the keploy agent answered the end of %s with status %s" % (test["name"], json.dumps(_text(a.get("status")))))
        if a.get("action") and a["action"] != ACTION_FAIL:
            raise KeployError("keploy: the keploy agent answered the end of %s with the unknown action %s" % (test["name"], json.dumps(a["action"])))
        return a


def _text(v):
    return "" if v is None else v


def _cause(e):
    r = getattr(e, "reason", None)
    if isinstance(e, urllib.error.URLError) and r is not None:
        return str(r)
    return str(e)
