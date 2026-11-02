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
    of its own function-scoped fixtures, whoever declares them (the test
    module, a conftest.py, another plugin); a test keploy set aside is
    skipped by pytest itself, with keploy's reason;
  - the test ends (POST /agent/scope/end) once its own function-scoped
    fixtures are torn down, before any fixture of a wider scope is, with the
    outcome of its setup and call; when keploy saw a call with no recorded
    answer, or a recorded call the test no longer made, the test errors at
    its teardown with keploy's text.
A fixture of a wider scope (session, package, module, class) that is set
up only once a test has begun (requested with request.getfixturevalue while
the test runs, or through a function fixture of the same name) would have
its calls counted as that test's: that test fails, naming the fixture and the
fix, and the run's end lists it. pytest-django's test database
(django_db_setup), which pytest-django sets up from inside a test for the
django_db marker and for live_server, is set up before the test begins.
Any answer of the agent this plugin cannot trust fails the test it belongs
to; a claim that fails errors every test. A test ID begun a second time in
one process (a rerun) errors with the reason; none runs unjudged.
A test is judged through pytest's fixtures: an item that uses none (a
custom pytest.Item, such as a linter's) runs as pytest runs it, not judged
by keploy, and the run's end names each such item. The lists at the run's
end travel in pytest's reports, so pytest-xdist's controller prints them.
"""
import os

import pytest

from .agent import ACTION_FAIL, ACTION_SKIP, AgentClient, KeployError
from .join import ENV_TEST_PID, decide

# The outcomes keploy's end takes, from best to worst.
_RANK = {"passed": 0, "skipped": 1, "failed": 2}

# The name of the fixture that begins and ends each test.
_FIXTURE = "_keploy"

# pytest-django's session fixture that creates and migrates the test
# database.
_DJANGO_DB_SETUP = "django_db_setup"


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


def setup_rank(name2fixturedefs, name):
    """Where a fixture name goes in the order pytest sets an item's fixtures
    up: those of a wider scope than the function first, then this plugin's,
    then the rest (function-scoped ones, and names with no fixture, such as
    request). pytest's own order puts wider scopes first, and within the
    function scope a root conftest's or another plugin's autouse fixture
    before this plugin's."""
    if name == _FIXTURE:
        return 1
    defs = name2fixturedefs.get(name)
    # The fixture the item uses is the last of its definitions (the one
    # nearest the test), as in pytest's own sort by scope.
    return 0 if defs and defs[-1].scope != "function" else 2


def django_db_setup_inside(item, names):
    """Whether pytest-django sets its test database up from inside this
    item's window, from an autouse function fixture of its own: for the
    django_db marker (_django_db_marker requests _django_db_helper) and for
    a test that uses live_server (_live_server_helper requests
    transactional_db). Both reach the session fixture django_db_setup.
    names is the item's fixture names."""
    return item.get_closest_marker("django_db") is not None or "live_server" in names


def has_pytest_django(config):
    """Whether pytest-django's plugin is registered, under whatever name."""
    return any(getattr(p, "__name__", None) == "pytest_django.plugin" for p in config.pluginmanager.get_plugins())


def wider_text(name, wider):
    """The failure of a test that set fixtures of a wider scope up while it
    ran: wider is (fixture, scope) pairs."""
    return ("keploy: %s set up a fixture of a wider scope while it ran: %s. That fixture's calls would count as this "
            "test's, and would move to whichever test sets it up first in another run. Have it set up before the test "
            "begins: name it in the test's arguments or a fixture's, or in an autouse fixture of its scope in "
            "conftest.py; not with request.getfixturevalue while the test runs, and not through a function fixture of "
            "the same name (rename one of them)" % (name, ", ".join("%s (%s scope)" % w for w in wider)))


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
        # The fixtures of a wider scope set up while it was open: (name,
        # scope).
        self.wider = []


# The open test of an item, and keploy's text when its end failed it.
_OPEN = pytest.StashKey()
_FAILED = pytest.StashKey()
# A line for a list at the run's end, as (report attribute, line): the
# report of the item's current phase carries it, so that pytest-xdist's
# controller, which sees its workers' reports, prints it too.
_NOTE = pytest.StashKey()

# The report attributes of _NOTE, in the order the run's end lists them.
_LISTS = ("keploy_wider", "keploy_set_aside", "keploy_unjudged")


class KeployScope:
    """The plugin of a process under keploy."""

    def __init__(self, joining):
        self.joining = joining
        self.client = None
        self.dir = ""
        # Whether pytest-django is loaded.
        self.django = False
        # The open test, while one is.
        self.window = None
        # Why no test of this process can run judged: a refused
        # KEPLOY_MOCK_AGENT, or a claim that failed.
        self.error = None
        # Every test ID begun in this process: a second begin of one fails.
        self.begun = set()
        # The lines of the lists at the run's end, by report attribute.
        self.lists = {a: [] for a in _LISTS}

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        self.django = has_pytest_django(session.config)
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

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            # It sets up no fixture, so _keploy never runs for it.
            item.stash[_NOTE] = ("keploy_unjudged", item.nodeid)
        else:
            names = info.names_closure
            early = ()
            if self.django and _DJANGO_DB_SETUP not in names and django_db_setup_inside(item, names):
                # pytest sets each name of the list up, by
                # request.getfixturevalue, so the test database is set up
                # before _keploy, as a test that requests db has it.
                names.append(_DJANGO_DB_SETUP)
                early = (_DJANGO_DB_SETUP,)
            # pytest sets the item's fixtures up in this list's order
            # (FixtureRequest._fillfixtures): _keploy goes right after every
            # fixture of a wider scope, before every function-scoped one.
            names.sort(key=lambda n: 0 if n in early else setup_rank(info.name2fixturedefs, n))
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        st = self.window
        if st is not None and fixturedef.scope != "function":
            # Set up only now, inside a test: its calls would be the test's.
            # A fixture that overrides another by its name is listed once.
            w = (fixturedef.argname, fixturedef.scope)
            if w not in st.wider:
                st.wider.append(w)
        return (yield)

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
            item.stash[_NOTE] = ("keploy_set_aside", "%s: %s" % (tid, reason))
            pytest.skip(reason)
        st = _Open(ref)
        item.stash[_OPEN] = st
        self.window = st
        yield
        # Every function-scoped fixture set up after this one is torn down;
        # none of a wider scope is yet.
        # pytest reported the test's setup (and call, when it ran) before
        # its teardown began: st.outcome is set.
        del item.stash[_OPEN]
        self.window = None
        failures = []
        if st.wider:
            # The test fails: keploy is told so.
            st.outcome = "failed"
            failures.append(wider_text(ref["name"], st.wider))
            item.stash[_NOTE] = ("keploy_wider", "%s: %s" % (tid, ", ".join("%s (%s scope)" % w for w in st.wider)))
        try:
            e = c.end(ref, st.outcome)
        except KeployError as err:
            failures.append(str(err))
        else:
            if e.get("action") == ACTION_FAIL:
                failures.append(keploy_text(e.get("reason"), "keploy: the keploy agent failed this test and gave no reason"))
        if failures:
            item.stash[_FAILED] = "\n".join(failures)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        rep = yield
        note = item.stash.get(_NOTE, None)
        if note is not None:
            del item.stash[_NOTE]
            setattr(rep, note[0], note[1])
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

    def pytest_runtest_logreport(self, report):
        # Every report of this process's tests, or of a pytest-xdist
        # controller's workers.
        for a in _LISTS:
            line = getattr(report, a, None)
            if line is not None:
                self.lists[a].append(line)

    def pytest_terminal_summary(self, terminalreporter):
        wider, set_aside, unjudged = (self.lists[a] for a in _LISTS)
        if wider:
            terminalreporter.section("keploy: %d test(s) failed: a fixture of a wider scope was set up inside them" % len(wider))
            terminalreporter.line("each fixture's calls would count as the test's; set it up before the test begins "
                                  "(each test's error says how):")
            for line in wider:
                terminalreporter.line(line)
        if set_aside:
            terminalreporter.section("keploy: %d test(s) set aside, skipped" % len(set_aside))
            for line in set_aside:
                terminalreporter.line(line)
        if unjudged:
            terminalreporter.section("keploy: %d item(s) not judged by keploy" % len(unjudged))
            terminalreporter.line("they use no pytest fixtures, so keploy-pytest cannot begin or end them; "
                                  "their calls are the test process's:")
            for nodeid in unjudged:
                terminalreporter.line(nodeid)



def pytest_configure(config):
    j = decide(os.environ, os.getpid())
    if j.mode == "inert":
        return
    config.pluginmanager.register(KeployScope(j), "keploy-scope")

