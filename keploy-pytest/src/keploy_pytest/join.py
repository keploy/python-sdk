"""Whether this process reports to keploy: the same rule go-sdk's keploytest
follows. keploy mock record and keploy mock replay export KEPLOY_MOCK_AGENT
(the agent's URL), KEPLOY_MOCK_AGENT_TOKEN and KEPLOY_MOCK_PROJECT (the
project's directory) into the test command. Unset or "off", the package is
inert and every test runs as it would without keploy.
"""
import json
import os
import urllib.parse

ENV_AGENT = "KEPLOY_MOCK_AGENT"
ENV_TOKEN = "KEPLOY_MOCK_AGENT_TOKEN"
ENV_PROJECT = "KEPLOY_MOCK_PROJECT"
# The claimer's pid, exported once a test process has claimed the run, so the
# processes its tests start (the service under test, a helper) know they
# belong to it and do not claim the run themselves.
ENV_TEST_PID = "KEPLOY_TEST_PID"


class Inert:
    mode = "inert"


class Refused:
    mode = "refused"

    def __init__(self, refusal):
        self.refusal = refusal


class Active:
    mode = "active"

    def __init__(self, url, token, project):
        self.url, self.token, self.project = url, token, project


def decide(env, pid, runs_inside=None):
    """Inert, Refused or Active, from the environment of process pid."""
    if runs_inside is None:
        runs_inside = runs_inside_proc
    v = (env.get(ENV_AGENT) or "").strip()
    if v == "" or v.lower() == "off":
        return Inert()
    try:
        u = urllib.parse.urlsplit(v)
        host = u.netloc
        u.port  # a port that is not a number is no URL
    except ValueError:
        u, host = None, ""
    if u is None or u.scheme not in ("http", "https") or host == "":
        return Refused("keploy: %s=%s is neither off nor the http URL of a keploy agent; set it to off to run without keploy" % (ENV_AGENT, json.dumps(v)))
    tp = (env.get(ENV_TEST_PID) or "").strip()
    if tp != str(pid):
        # Another process joined keploy's run. This one is a member only when
        # it runs inside that process; a marker naming any other process was
        # inherited from elsewhere, and this process claims like any test
        # process (the agent refuses a second live one).
        try:
            n = int(tp)
        except ValueError:  # no marker ("") or no pid
            n = 0
        if n > 0 and runs_inside(n):
            return Inert()
    return Active(v.rstrip("/"), env.get(ENV_TOKEN) or "", (env.get(ENV_PROJECT) or "").strip())


def runs_inside_proc(pid):
    """Whether this process runs inside process pid (pid is an ancestor), read
    from /proc. Without /proc (a system other than Linux) it cannot tell, and
    takes the marker at its word."""
    if not os.path.exists("/proc/self/stat"):
        return True
    p = os.getppid()
    # Bounded: a walk that cycles (a pid reused while it is read) ends.
    for _ in range(128):
        if p == pid:
            return True
        p = _proc_parent(p)
        if p is None:  # past the first process (its parent, 0, has no entry), or gone
            return False
    return False


def _proc_parent(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            stat = f.read()
    except OSError:
        return None
    # The command name, in parentheses, may hold spaces and parentheses: the
    # fields after it start at the last ')'.
    rest = stat[stat.rindex(b")") + 2:].split(b" ")
    try:
        return int(rest[1])
    except (IndexError, ValueError):
        return None

