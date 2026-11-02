"""keploy-pytest joins a pytest suite to keploy's Universal Mocker.

keploy records the calls each test's code makes (keploy mock record) and
replays them (keploy mock replay), so the suite runs with every dependency
answered from the recording. keploy judges every recorded test and lists the
ones it cannot replay, with a reason. This plugin is how a test tells keploy
where it begins and ends, and how keploy skips a test it set aside or fails a
test whose calls changed. It speaks the same scope protocol (v1) as go-sdk's
keploytest, @keploy/playwright and @keploy/jest; it decides nothing itself.

Installed, pytest loads it by itself (the pytest11 entry point "keploy").
Without keploy (KEPLOY_MOCK_AGENT unset or "off") it registers nothing.
Under keploy, with one test process (keploy's first release runs one test
process at a time, and refuses a second):
  - the process claims keploy's run when its session starts, before pytest
    collects (and so imports) any test module (POST /agent/instance), with
    keploy's project folder (KEPLOY_MOCK_PROJECT), which its test files are
    named from; the current folder only when keploy names none;
  - each test begins (POST /agent/scope/begin) once the fixtures of a wider
    scope it uses (session, package, module, class) are set up, before any
    of its own function-scoped fixtures; a test keploy set aside is skipped
    by pytest itself, with keploy's reason;
  - the test ends (POST /agent/scope/end) once its own function-scoped
    fixtures are torn down, before any fixture of a wider scope is, with the
    outcome of its setup and call; when keploy saw a call with no recorded
    answer, or a recorded call the test no longer made, the test errors at
    its teardown with keploy's text.
The fixtures' scopes are pytest's, so the calls a session or module fixture
makes are the test process's, outside every test, as pytest scopes them.
Any answer of the agent this plugin cannot trust fails the test it belongs
to; a claim that fails errors every test. A test ID begun a second time in
one process (a rerun) errors with the reason; none runs unjudged.
"""
import os

import pytest

from .agent import ACTION_FAIL, ACTION_SKIP, AgentClient, KeployError
from .join import ENV_TEST_PID, decide

# The outcomes keploy's end takes, from best to worst.
_RANK = {"passed": 0, "skipped": 1, "failed": 2}


def keploy_text(reason, fallback):
    """keploy's reason as the test shows it: once prefixed with "keploy: " (a
    judging reason from the agent already is; a skip reason, "<class>/<reason>:
    <detail>", is not), or fallback when the agent gave none, as go-sdk's
    keploytest shows it."""
    if not reason:
        return fallback
    return reason if reason.startswith("keploy: ") else "keploy: " + reason


def test_ref(item, dir):
    """A test's name on keploy's wire: keploy builds its ID as file::name.
    file is the test's file relative to dir, slash-separated; name is the
    test's pytest node ID within its file (Class::test_name[param])."""
    path = os.path.realpath(str(item.path))
    file = os.path.relpath(path, dir).replace(os.sep, "/")
    return {"name": item.nodeid.split("::", 1)[-1], "file": file}


def worst(a, b):
    """The worse of two outcomes; None is no outcome yet."""
    if a is None:
        return b
    return b if _RANK[b] > _RANK[a] else a


def outcome_of(report):
    """A phase's outcome as keploy's end takes it: pytest's own (an expected
    failure, xfail, is pytest's skipped; a strict unexpected pass its
    failed)."""
    return report.outcome if report.outcome in _RANK else "failed"


def xdist_controller(config):
    """Whether this process is pytest-xdist's controller, which runs no test
    (its workers do): xdist's own test (xdist.is_xdist_controller)."""
    return getattr(config, "workerinput", None) is None and config.getoption("dist", "no") != "no"


class _Open:
    """A test begun and not yet ended."""

    def __init__(self, ref):
        self.ref = ref
        # The worst outcome of its setup and call so far.
        self.outcome = None


# The open test of an item, and keploy's text when its end failed it.
_OPEN = pytest.StashKey()
_FAILED = pytest.StashKey()


class KeployScope:
    """The plugin of a process under keploy."""

    def __init__(self, joining):
        self.joining = joining
        self.client = None
        self.dir = ""
        # Why no test of this process can run judged: a refused
        # KEPLOY_MOCK_AGENT, or a claim that failed.
        self.error = None
        # Every test ID begun in this process: a second begin of one fails.
        self.begun = set()
        # Tests keploy set aside: (test ID, reason).
        self.set_aside = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        if xdist_controller(session.config):
            # The controller runs no test, and claims nothing: each worker
            # claims for itself.
            return
        j = self.joining
        if j.mode == "refused":
            self.error = j.refusal
            return
        client = AgentClient(j.url, j.token)
        d = os.path.realpath(j.project or os.getcwd())
        try:
            client.claim(".", d)
        except KeployError as e:
            self.error = str(e)
            return
        os.environ[ENV_TEST_PID] = str(os.getpid())
        self.client, self.dir = client, d

    @pytest.fixture(autouse=True)
    def _keploy(self, request):
        if self.error is not None:
            pytest.fail(self.error, pytrace=False)
        c = self.client
        if c is None:
            yield
            return
        item = request.node
        ref = test_ref(item, self.dir)
        tid = ref["file"] + "::" + ref["name"]
        if tid in self.begun:
            pytest.fail("keploy: %s in %s would begin a second time, and keploy names a test by its file and name: "
                        "run each test once (no reruns, such as pytest-rerunfailures' --reruns)" % (ref["name"], ref["file"]),
                        pytrace=False)
        self.begun.add(tid)
        try:
            b = c.begin(ref)
        except KeployError as e:
            pytest.fail(str(e), pytrace=False)
        if b.get("action") == ACTION_SKIP:
            # pytest's own skip: no fixture of the test's own runs, and the
            # reason is pytest's skip reason. No end follows.
            reason = keploy_text(b.get("reason"), "keploy: set aside (the agent gave no reason)")
            self.set_aside.append((tid, reason))
            pytest.skip(reason)
        st = _Open(ref)
        item.stash[_OPEN] = st
        yield
        # Every function-scoped fixture set up after this one is torn down;
        # none of a wider scope is yet.
        # pytest reported the test's setup (and call, when it ran) before
        # its teardown began: st.outcome is set.
        del item.stash[_OPEN]
        try:
            e = c.end(ref, st.outcome)
        except KeployError as err:
            item.stash[_FAILED] = str(err)
            return
        if e.get("action") == ACTION_FAIL:
            item.stash[_FAILED] = keploy_text(e.get("reason"), "keploy: the keploy agent failed this test and gave no reason")

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        rep = yield
        # The test is open through its setup and call reports: its end, in
        # its teardown, closes it, and sets the failure the teardown report
        # carries.
        st = item.stash.get(_OPEN, None)
        if st is not None:
            st.outcome = worst(st.outcome, outcome_of(rep))
        failure = item.stash.get(_FAILED, None)
        if failure is not None:
            del item.stash[_FAILED]
            if rep.failed:
                # The teardown failed already: keploy's text goes beside it.
                rep.sections.append(("keploy", failure))
            else:
                rep.outcome = "failed"
                rep.longrepr = failure
        return rep

    def pytest_terminal_summary(self, terminalreporter):
        if not self.set_aside:
            return
        terminalreporter.section("keploy: %d test(s) set aside, skipped" % len(self.set_aside))
        for tid, reason in self.set_aside:
            terminalreporter.line("%s: %s" % (tid, reason))



def pytest_configure(config):
    j = decide(os.environ, os.getpid())
    if j.mode == "inert":
        return
    config.pluginmanager.register(KeployScope(j), "keploy-scope")

