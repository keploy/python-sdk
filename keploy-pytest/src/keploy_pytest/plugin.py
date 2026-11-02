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
  - each test begins (POST /agent/scope/begin) once every fixture of a
    wider scope (session, package, module, and class for a test in a class)
    it reaches is set up, and before any of its own function-scoped
    fixtures, whoever declares them
    (the test module, a conftest.py, another plugin); a test keploy set aside
    is skipped by pytest itself, with keploy's reason;
  - the test ends (POST /agent/scope/end) once its own function-scoped
    fixtures are torn down, before any fixture of a wider scope is, with the
    outcome of its setup and call; when keploy saw a call with no recorded
    answer, or a recorded call the test no longer made, the test errors at
    its teardown with keploy's text.
The fixtures of a wider scope a test reaches are read from pytest's own
fixture graph as the test is set up: the test's fixture names, including
those a plugin adds once the test is collected (pytest-asyncio's event loop,
or its runner), and each fixture's arguments, through any fixture that
overrides another by its name. A class-scoped fixture is of a wider scope
only for a test in a class: for a test outside one, pytest sets it up and
tears it down with the test, as a function-scoped one, and its calls are the
test's. Three fixtures are set up by name from inside
a test, outside that graph, and are followed too: pytest's
doctest_namespace, for a doctest; pytest-django's test database
(django_db_setup), for the django_db marker and for live_server; and
pytest-asyncio's event loop (0.24 to 1.0) or event loop runner (1.1 and
later) of the loop scope of a fixture it runs, which it requests as it sets
the fixture up.
A fixture of a wider scope that is still set up only once a test has begun
(requested with request.getfixturevalue while the test runs, or one that
only a function fixture of the same name reaches, def db(db)) would have its
calls counted as that test's: that test fails, naming the fixture and the
fix, and the run's end lists it.
Any answer of the agent this plugin cannot trust fails the test it belongs
to; a claim that fails errors every test. A test ID begun a second time in
one process (a rerun) errors with the reason; none runs unjudged.
A test is judged through pytest's fixtures: an item that uses none (a
custom pytest.Item, such as a linter's) runs as pytest runs it, not judged
by keploy, and the run's end names each such item. The lists at the run's
end travel in pytest's reports, so pytest-xdist's controller prints them.
"""
import inspect
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

# pytest's session fixture whose names a doctest runs with: DoctestItem.setup
# requests it by name once the doctest's fixtures are set up.
_DOCTEST_NAMESPACE = "doctest_namespace"

# The scopes wider than the function, widest first.
_WIDER = ("session", "package", "module", "class")


def wider_scopes(item):
    """The scopes wider than the function for item, widest first. The class
    scope is one only for an item in a class (a pytest.Class, a unittest
    TestCase's included), pytest's own rule for that scope's node: outside a
    class, pytest sets a class-scoped fixture up for the item itself and
    tears it down with it, as a function-scoped one."""
    return _WIDER if item.getparent(pytest.Class) is not None else _WIDER[:-1]


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


def setup_rank(name2fixturedefs, name, wider):
    """Where a fixture name goes in the order pytest sets an item's fixtures
    up: those of a scope in wider (the item's wider_scopes) first, then this
    plugin's, then the rest (function-scoped ones, a class-scoped one outside
    a class, and names with no fixture, such as request). pytest's own order
    puts wider scopes first, and within the function scope a root conftest's
    or another plugin's autouse fixture before this plugin's."""
    if name == _FIXTURE:
        return 1
    defs = name2fixturedefs.get(name)
    # The fixture the item uses is the last of its definitions (the one
    # nearest the test), as in pytest's own sort by scope.
    return 0 if defs and defs[-1].scope in wider else 2


def django_db_setup_inside(item, names):
    """Whether pytest-django sets its test database up from inside this
    item's window, from an autouse function fixture of its own: for the
    django_db marker (_django_db_marker requests _django_db_helper) and for
    a test that uses live_server (_live_server_helper requests
    transactional_db). Both reach the session fixture django_db_setup.
    names is the item's fixture names."""
    return item.get_closest_marker("django_db") is not None or "live_server" in names


def by_name(item, django):
    """The fixtures of a wider scope that pytest or a plugin sets up by name
    from inside this item's window, outside pytest's fixture graph: pytest's
    doctest_namespace for a doctest, and, when pytest-django is loaded
    (django), its django_db_setup for an item django_db_setup_inside names."""
    out = []
    if isinstance(item, pytest.DoctestItem):
        out.append(_DOCTEST_NAMESPACE)
    if django and django_db_setup_inside(item, item.fixturenames):
        out.append(_DJANGO_DB_SETUP)
    return out


def is_async(func):
    """Whether a fixture's function is a coroutine or an async generator
    function."""
    return inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)


def asyncio_plugin(config):
    """pytest-asyncio's plugin (the module pytest_asyncio.plugin), when it is
    registered, else None."""
    for p in config.pluginmanager.get_plugins():
        if getattr(p, "__name__", None) == "pytest_asyncio.plugin":
            return p
    return None


def asyncio_strict(config):
    """Whether pytest-asyncio runs in strict mode, read as pytest-asyncio
    reads it: its --asyncio-mode option, else its asyncio_mode setting."""
    mode = config.getoption("asyncio_mode", None)
    if mode is None:
        mode = config.getini("asyncio_mode")
    return mode == "strict"


def asyncio_loop(fd, request, plugin):
    """The fixture pytest-asyncio (plugin, None when it is not loaded)
    requests by name as it sets fd up, outside fd's arguments, or None.
    request is the test's request. It is pytest-asyncio's own rule:
      - 0.24 to 1.0 put a wrapper in place of each async fixture they run
        when they collect it, and keep those fixtures in a set of their own
        (_HOLDER). As the wrapper sets such a fixture up, it requests the
        event loop that pytest-asyncio's own
        _get_event_loop_fixture_id_for_async_fixture names, from the loop
        scope it resolved for the fixture as it wrapped it: on 0.24 to 0.26,
        event_loop for the function loop scope, else the loop of the test's
        collector of that scope (_session_event_loop,
        <module>::<event_loop>...); on 1.0, _<loop scope>_event_loop. That
        set and that function, called with the fixture's own function as the
        wrapper calls it, are read here. Any other fixture (a sync one
        marked with pytest_asyncio.fixture included) is run as it is, and
        requests no loop.
      - 1.1 and later request, for a fixture marked with
        pytest_asyncio.fixture (an async or a sync one), and, outside strict
        mode, for any coroutine or async generator function, the event loop
        runner of the fixture's loop scope, _<loop scope>_scoped_runner: the
        loop scope pytest_asyncio.fixture gave the fixture, else the
        asyncio_default_fixture_loop_scope setting, else the fixture's
        scope. Each runner requests the session fixture event_loop_policy
        (an autouse one), and on 1.4 _asyncio_loop_factory too: the walk
        follows the runner's arguments as any fixture's.
      - 0.23 gives an async fixture its loop as an argument when it
        collects it, inside pytest's graph, and has no runner: the runner's
        name names no fixture there, and the walk passes it by."""
    if plugin is None:
        return None
    loop_id = getattr(plugin, "_get_event_loop_fixture_id_for_async_fixture", None)
    if loop_id is not None:
        return loop_id(request, fd.func.__wrapped__) if fd in plugin._HOLDER else None
    if not getattr(fd.func, "_force_asyncio_fixture", False) and (asyncio_strict(request.config) or not is_async(fd.func)):
        return None
    try:
        default = request.config.getini("asyncio_default_fixture_loop_scope")
    except ValueError:
        default = None
    return "_%s_scoped_runner" % (getattr(fd.func, "_loop_scope", None) or default or fd.scope)


def wider_reached(names, defs_of, requested, wider):
    """The fixtures of a scope in wider (the item's wider_scopes) that pytest
    sets up for these fixture names, widest scope first, read from pytest's
    own graph:
    each name's nearest definition (defs_of(name)[-1], the one pytest gives a
    test), each fixture's arguments, and, where a fixture requests its own
    name, the definition it overrides (pytest's own chain). A fixture also
    reaches the fixture requested(fixturedef) names, when not None: one a
    plugin requests by name as it sets the fixture up. A wider definition
    reached only as the one an override overrides (a session db under
    def db(db)) is not returned: requesting its name gives the override."""
    found = {}
    seen = set()
    todo = [(n, ()) for n in names]
    i = 0
    while i < len(todo):
        name, chain = todo[i]
        i += 1
        defs = defs_of(name)
        depth = chain.count(name) + 1
        if not defs or depth > len(defs):
            continue
        fd = defs[-depth]
        if id(fd) in seen:
            continue
        seen.add(id(fd))
        if depth == 1 and fd.scope in wider:
            found[name] = wider.index(fd.scope)
        args = list(fd.argnames)
        extra = requested(fd)
        if extra is not None:
            args.append(extra)
        todo.extend((a, chain + (name,)) for a in args)
    return sorted(found, key=found.get)


def defs_lookup(item):
    """defs_of for wider_reached: a fixture name's definitions visible to
    item, as pytest itself looks them up for the item (pytest 8.0 looks them
    up by the item's parent's node ID, later pytest by the item)."""
    info = item._fixtureinfo
    fm = item.session._fixturemanager
    node = item if "node" in inspect.signature(fm.getfixturedefs).parameters else item.parent.nodeid

    def defs_of(name):
        defs = info.name2fixturedefs.get(name)
        return defs if defs is not None else fm.getfixturedefs(name, node)

    return defs_of


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

    def __init__(self, ref, scopes):
        self.ref = ref
        # The scopes wider than the function for its item (wider_scopes).
        self.scopes = scopes
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
        # pytest-asyncio's plugin, when it is loaded.
        self.asyncio = None
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
        self.asyncio = asyncio_plugin(session.config)
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
            # pytest sets the item's fixtures up in this list's order
            # (FixtureRequest._fillfixtures): _keploy goes right after every
            # fixture of a wider scope, before every function-scoped one.
            wider = wider_scopes(item)
            info.names_closure.sort(key=lambda n: setup_rank(info.name2fixturedefs, n, wider))
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        st = self.window
        if st is not None and fixturedef.scope in st.scopes:
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
        # Every fixture of a wider scope the test reaches is set up before it
        # begins, the ones a plugin added to its names since pytest sorted
        # them included: their calls belong to the test process.
        names = list(item.fixturenames) + by_name(item, self.django)
        wider = wider_scopes(item)
        for name in wider_reached(names, defs_lookup(item), lambda fd: asyncio_loop(fd, request, self.asyncio), wider):
            request.getfixturevalue(name)
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
        st = _Open(ref, wider)
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

