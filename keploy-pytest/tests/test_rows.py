"""The rows: real pytest runs (pytester, each in a process of its own) of
small suites under a fake keploy agent, each read by what a user reads (the
outcomes, the report's text) and by the calls the agent got, in order."""
import json
import os

import pytest

from conftest import ok

ORDER = '''
import pytest
from markhelp import mark

mark("import")

@pytest.fixture(scope="session")
def db():
    mark("session setup")
    yield
    mark("session teardown")

@pytest.fixture(scope="module")
def tenant():
    mark("module setup")
    yield
    mark("module teardown")

@pytest.fixture
def cart():
    mark("function setup")
    yield
    mark("function teardown")

class TestCart:
    @pytest.mark.parametrize("item", ["book"])
    def test_add(self, db, tenant, cart, item):
        mark("test body")

def test_second(tenant):
    mark("second body")
'''


def run(pytester, *args):
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs", *args)


def test_inert_without_keploy(pytester, agent, monkeypatch):
    pytester.makepyfile(markhelp="")
    pytester.makepyfile(test_a="def test_a():\n    pass\n")
    for value in (None, "off", " OFF "):
        if value is None:
            monkeypatch.delenv("KEPLOY_MOCK_AGENT", raising=False)
        else:
            monkeypatch.setenv("KEPLOY_MOCK_AGENT", value)
        r = run(pytester)
        r.assert_outcomes(passed=1)
        assert agent.calls == []
        # pytest's header lists the installed plugins and the rootdir (whose
        # path may say keploy): no other line may.
        assert [line for line in r.outlines if "keploy" in line and not line.startswith(("plugins: ", "rootdir: "))] == []


def test_claim_begin_end_order(suite, agent):
    suite.makepyfile(test_order=ORDER)
    r = run(suite)
    r.assert_outcomes(passed=2)
    assert agent.log() == [
        "claim .",
        "import",
        "session setup",
        "module setup",
        "begin test_order.py::TestCart::test_add[book]",
        "function setup",
        "test body",
        "function teardown",
        "end test_order.py::TestCart::test_add[book] passed",
        "begin test_order.py::test_second",
        "second body",
        "end test_order.py::test_second passed",
        "module teardown",
        "session teardown",
    ]
    claim = agent.calls[0][1]
    assert claim == {"key": ".", "dir": os.path.realpath(str(suite.path))}
    assert "set aside" not in r.stdout.str()
    assert "not judged" not in r.stdout.str()
    assert "wider scope" not in r.stdout.str()


def autouse(label, scope="function"):
    """A file's text that declares an autouse fixture marking its setup and
    teardown."""
    return """
import pytest
from markhelp import mark

@pytest.fixture(autouse=True, scope=%r)
def %s():
    mark("%s setup")
    yield
    mark("%s teardown")
""" % (scope, label.replace(" ", "_"), label, label)


BODY = "from markhelp import mark\n\ndef test_a():\n    mark('body')\n"


def test_function_fixtures_of_a_conftest_or_another_plugin_are_inside_the_window(suite, agent):
    # A root conftest.py, a conftest.py beside the tests and another plugin
    # (-p) each declare an autouse function-scoped fixture, and the root
    # conftest a session-scoped one: pytest registers them all before this
    # plugin, and sets their fixtures up in that order within a scope.
    suite.makeconftest(autouse("root conftest") + autouse("root session", scope="session").replace("import pytest\nfrom markhelp import mark\n", ""))
    suite.makepyfile(myplug=autouse("plugin"))
    (suite.path / "tests").mkdir()
    (suite.path / "tests" / "conftest.py").write_text(autouse("tests conftest"))
    (suite.path / "tests" / "test_a.py").write_text(BODY)
    r = run(suite, "-p", "myplug", "tests")
    r.assert_outcomes(passed=1)
    log = agent.log()
    assert log[:2] == ["claim .", "root session setup"], log
    assert log[2] == "begin tests/test_a.py::test_a", log
    assert sorted(log[3:6]) == ["plugin setup", "root conftest setup", "tests conftest setup"], log
    assert log[6] == "body", log
    assert sorted(log[7:10]) == ["plugin teardown", "root conftest teardown", "tests conftest teardown"], log
    assert log[10:] == ["end tests/test_a.py::test_a passed", "root session teardown"], log


def test_a_session_fixture_overridden_by_a_function_fixture_is_inside_the_window(suite, agent):
    # The test module overrides the root conftest's session-scoped db with a
    # function-scoped one: the test uses the nearer, function-scoped db, so
    # it is set up after the begin and torn down before the end.
    suite.makeconftest("""
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def db():
    mark("session db setup")
    yield
    mark("session db teardown")
""")
    suite.makepyfile(test_a="""
import pytest
from markhelp import mark

@pytest.fixture
def db():
    mark("function db setup")
    yield
    mark("function db teardown")

def test_a(db):
    mark("body")
""")
    r = run(suite)
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "function db setup", "body", "function db teardown",
                           "end test_a.py::test_a passed"]


WIDER = """
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def db():
    mark("session db setup")
    yield
    mark("session db teardown")

@pytest.fixture(scope="module")
def tenant():
    mark("module tenant setup")
    yield
    mark("module tenant teardown")
"""


def test_a_wider_fixture_set_up_inside_a_test_fails_that_test_and_is_listed(suite, agent):
    # A session or module fixture first requested by a test's body is set
    # up after the begin: its calls would be the test's, so the test fails
    # with the fixture's name and the fix. Once set up, it is cached: the
    # next test that requests it sets nothing up.
    suite.makeconftest(WIDER)
    suite.makepyfile(test_a="""
from markhelp import mark

def test_a(request):
    request.getfixturevalue("db")
    mark("a body")

def test_b(request):
    request.getfixturevalue("db")
    mark("b body")

def test_c(request):
    request.getfixturevalue("tenant")
    mark("c body")
""")
    r = run(suite)
    r.assert_outcomes(passed=3, errors=2)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "session db setup", "a body", "end test_a.py::test_a failed",
                           "begin test_a.py::test_b", "b body", "end test_a.py::test_b passed",
                           "begin test_a.py::test_c", "module tenant setup", "c body", "end test_a.py::test_c failed",
                           "module tenant teardown", "session db teardown"]
    r.stdout.fnmatch_lines([
        "*ERROR at teardown of test_a*",
        "keploy: test_a set up a fixture of a wider scope while it ran: db (session scope). That fixture's calls would "
        "count as this test's, and would move to whichever test sets it up first in another run. Have it set up before "
        "the test begins: name it in the test's arguments or a fixture's, or in an autouse fixture of its scope in "
        "conftest.py; not with request.getfixturevalue while the test runs, and not through a function fixture of the "
        "same name (rename one of them)",
        "*ERROR at teardown of test_c*",
        "keploy: test_c set up a fixture of a wider scope while it ran: tenant (module scope).*",
        "*= keploy: 2 test(s) failed: a fixture of a wider scope was set up inside them =*",
        "each fixture's calls would count as the test's; set it up before the test begins (each test's error says how):",
        "test_a.py::test_a: db (session scope)",
        "test_a.py::test_c: tenant (module scope)",
    ])
    # keploy's own failure of the same test goes beside it.
    agent.calls.clear()
    agent.script["/agent/scope/end"] = lambda body, n: ok(action="fail", reason="keploy: GET /price/pen had no recorded answer")
    r = run(suite, "test_a.py::test_a")
    r.assert_outcomes(passed=1, errors=1)
    r.stdout.fnmatch_lines(["keploy: test_a set up a fixture of a wider scope while it ran: db (session scope).*",
                            "keploy: GET /price/pen had no recorded answer"])


def test_a_function_fixture_that_requests_the_wider_one_it_overrides_fails_the_test(suite, agent):
    # def db(db): the test's function-scoped db sets the conftest's session
    # db up from inside the test.
    suite.makeconftest(WIDER)
    suite.makepyfile(test_a="""
import pytest
from markhelp import mark

@pytest.fixture
def db(db):
    mark("function db setup")
    yield
    mark("function db teardown")

def test_a(db):
    mark("body")
""")
    r = run(suite)
    r.assert_outcomes(passed=1, errors=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "session db setup", "function db setup", "body",
                           "function db teardown", "end test_a.py::test_a failed", "session db teardown"]
    r.stdout.fnmatch_lines(["keploy: test_a set up a fixture of a wider scope while it ran: db (session scope).*"
                            "(rename one of them)"])


def test_class_and_package_fixtures_are_outside_the_window(suite, agent):
    pkg = suite.path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "conftest.py").write_text(autouse("package", scope="package"))
    (pkg / "test_a.py").write_text("""
import pytest
from markhelp import mark

class TestA:
    @pytest.fixture(scope="class")
    def cls_fix(self):
        mark("class setup")
        yield
        mark("class teardown")

    def test_a(self, cls_fix):
        mark("body")
""")
    r = run(suite)
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "package setup", "class setup", "begin pkg/test_a.py::TestA::test_a", "body",
                           "end pkg/test_a.py::TestA::test_a passed", "class teardown", "package teardown"]


def test_a_class_or_package_fixture_set_up_inside_a_test_fails_that_test_and_is_listed(suite, agent):
    pkg = suite.path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "conftest.py").write_text("""
import pytest
from markhelp import mark

@pytest.fixture(scope="package")
def pfix():
    mark("package pfix setup")
""")
    (pkg / "test_a.py").write_text("""
import pytest
from markhelp import mark

class TestA:
    @pytest.fixture(scope="class")
    def cfix(self):
        mark("class cfix setup")

    def test_c(self, request):
        request.getfixturevalue("cfix")

def test_p(request):
    request.getfixturevalue("pfix")
""")
    r = run(suite)
    r.assert_outcomes(passed=2, errors=2)
    assert agent.log() == ["claim .", "begin pkg/test_a.py::TestA::test_c", "class cfix setup",
                           "end pkg/test_a.py::TestA::test_c failed", "begin pkg/test_a.py::test_p", "package pfix setup",
                           "end pkg/test_a.py::test_p failed"]
    r.stdout.fnmatch_lines(["keploy: TestA::test_c set up a fixture of a wider scope while it ran: cfix (class scope).*",
                            "keploy: test_p set up a fixture of a wider scope while it ran: pfix (package scope).*",
                            "*= keploy: 2 test(s) failed: a fixture of a wider scope was set up inside them =*",
                            "each fixture's calls*",
                            "pkg/test_a.py::TestA::test_c: cfix (class scope)",
                            "pkg/test_a.py::test_p: pfix (package scope)"])


def test_a_wider_fixture_that_overrides_one_of_its_name_is_listed_once(suite, agent):
    # A session db over the conftest's session db, first requested by the
    # test's body: both are set up inside the test, under one name and scope.
    suite.makeconftest(WIDER)
    suite.makepyfile(test_a="""
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def db(db):
    mark("module's session db setup")
    yield

def test_a(request):
    request.getfixturevalue("db")
""")
    r = run(suite)
    r.assert_outcomes(passed=1, errors=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "session db setup", "module's session db setup",
                           "end test_a.py::test_a failed", "session db teardown"]
    line = [x for x in r.outlines if x.startswith("keploy: test_a set up a fixture of a wider scope")]
    assert len(line) == 1 and line[0].count("db (session scope)") == 1, r.outlines


def test_wider_fixtures_a_plugin_adds_to_a_tests_names_are_set_up_before_it_begins(suite, agent):
    # A plugin's pytest_runtest_setup adds a fixture name to the test's
    # names after keploy-pytest ordered them, as pytest-asyncio adds its event
    # loop runner. Every fixture of a wider scope that name reaches, through
    # its arguments and the conftest fixture it overrides, is set up before
    # the test begins, widest scope first; the function-scoped ones after.
    suite.makeconftest("""
import pytest
from markhelp import mark

def pytest_runtest_setup(item):
    item.fixturenames.append("added")

@pytest.fixture(scope="session")
def sess():
    mark("session sess setup")

@pytest.fixture
def added(sess):
    mark("conftest added setup")
""")
    pkg = suite.path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "conftest.py").write_text("""
import pytest
from markhelp import mark

@pytest.fixture(scope="package")
def pkgf():
    mark("package pkgf setup")
""")
    (pkg / "test_a.py").write_text("""
import pytest
from markhelp import mark

@pytest.fixture(scope="module")
def modf():
    mark("module modf setup")

class TestA:
    @pytest.fixture(scope="class")
    def clsf(self):
        mark("class clsf setup")

    @pytest.fixture
    def added(self, added, modf, pkgf, clsf):
        mark("class added setup")

    def test_a(self):
        mark("body")
""")
    r = run(suite)
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "session sess setup", "package pkgf setup", "module modf setup", "class clsf setup",
                           "begin pkg/test_a.py::TestA::test_a", "conftest added setup", "class added setup", "body",
                           "end pkg/test_a.py::TestA::test_a passed"]


def test_a_doctest_begins_once_doctest_namespace_is_set_up(suite, agent):
    # pytest's doctest runner requests the session fixture doctest_namespace
    # by name once the doctest's own fixtures are set up.
    suite.makeconftest("""
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def doctest_namespace(doctest_namespace):
    mark("doctest_namespace setup")
    doctest_namespace["three"] = 3
    return doctest_namespace
""")
    suite.makepyfile(mod='''
def add(a, b):
    """
    >>> add(1, 2) == three
    True
    """
    return a + b
''')
    suite.makepyfile(test_a=BODY)
    r = run(suite, "--doctest-modules")
    r.assert_outcomes(passed=2)
    assert agent.log() == ["claim .", "doctest_namespace setup", "begin mod.py::mod.add", "end mod.py::mod.add passed",
                           "begin test_a.py::test_a", "body", "end test_a.py::test_a passed"]
    # A test that is no doctest does not set it up.
    agent.calls.clear()
    r = run(suite, "test_a.py")
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "body", "end test_a.py::test_a passed"]


def asyncio_version():
    from importlib.metadata import version
    return tuple(int(x) for x in version("pytest-asyncio").split(".")[:2])


def test_pytest_asyncio_tests_and_fixtures_begin_after_their_event_loop(suite, agent):
    # pytest-asyncio adds its event loop runner (1.x) or loop (0.23) to an
    # async test's names as the test is set up, and requests a runner by name
    # as it sets an async fixture up. Its loops, of every loop scope, are set
    # up before the test begins; the async fixture and the body run inside.
    # Each file runs in a process of its own, so each is the first to need
    # a loop.
    loop = "loop_scope" if asyncio_version() >= (1, 0) else "scope"
    two = """
async def test_one(%s):
    mark("one body")

async def test_two(%s):
    mark("two body")
"""
    head = "import pytest\nfrom markhelp import mark\n"
    fixture = """
@pytest.fixture
async def client():
    mark("client up")
    yield 1
    mark("client down")
"""
    shapes = {
        # strict mode, the asyncio marker, the default (function) loop scope
        "test_strict.py": ("strict", head + "pytestmark = pytest.mark.asyncio\n" + two % ("", "")),
        # auto mode, an async fixture (the shape of FastAPI's and httpx's docs)
        "test_auto.py": ("auto", head + fixture + two % ("client", "client")),
        "test_module_loop.py": ("strict", head + "pytestmark = pytest.mark.asyncio(%s='module')\n" % loop + two % ("", "")),
        "test_session_loop.py": ("strict", head + "pytestmark = pytest.mark.asyncio(%s='session')\n" % loop + two % ("", "")),
    }
    for f, (mode, text) in shapes.items():
        suite.makepyfile(**{f[:-3]: text})
        agent.calls.clear()
        r = run(suite, "-o", "asyncio_mode=" + mode, f)
        r.assert_outcomes(passed=2)
        up, down = (["client up"], ["client down"]) if f == "test_auto.py" else ([], [])
        assert agent.log() == (["claim .", "begin %s::test_one" % f] + up + ["one body"] + down +
                               ["end %s::test_one passed" % f, "begin %s::test_two" % f] + up + ["two body"] + down +
                               ["end %s::test_two passed" % f]), (f, agent.log())
    # A test that is not async, with an async fixture (an async generator
    # or a coroutine): only the fixture asks pytest-asyncio for a loop.
    coro = fixture.replace('    yield 1\n    mark("client down")\n', "    return 1\n")
    for f, text in {"test_sync_gen.py": fixture, "test_sync_coro.py": coro}.items():
        suite.makepyfile(**{f[:-3]: head + text + '\ndef test_one(client):\n    mark("one body")\n'})
        agent.calls.clear()
        r = run(suite, "-o", "asyncio_mode=auto", f)
        r.assert_outcomes(passed=1)
        down = ["client down"] if f == "test_sync_gen.py" else []
        assert agent.log() == ["claim .", "begin %s::test_one" % f, "client up", "one body"] + down + [
            "end %s::test_one passed" % f], (f, agent.log())
    if asyncio_version() < (1, 0):
        return
    # pytest-asyncio 1.x runs an async fixture in the runner of the fixture's
    # own loop scope, which it requests by name as it sets the fixture up:
    # the one pytest_asyncio.fixture gives it, else the
    # asyncio_default_fixture_loop_scope setting. That runner is set up
    # before the test begins too.
    scoped = head + "import pytest_asyncio\n" + fixture.replace("@pytest.fixture", "@pytest_asyncio.fixture(loop_scope='module')")
    shapes = {
        "test_fixture_loop.py": (["-o", "asyncio_mode=strict"], scoped + '\ndef test_one(client):\n    mark("one body")\n'),
        "test_default_loop.py": (["-o", "asyncio_mode=auto", "-o", "asyncio_default_fixture_loop_scope=session"],
                                 head + fixture + '\nasync def test_one(client):\n    mark("one body")\n'),
    }
    for f, (opts, text) in shapes.items():
        suite.makepyfile(**{f[:-3]: text})
        agent.calls.clear()
        r = run(suite, *opts, f)
        r.assert_outcomes(passed=1)
        assert agent.log() == ["claim .", "begin %s::test_one" % f, "client up", "one body", "client down",
                               "end %s::test_one passed" % f], (f, agent.log())


def test_pytest_asyncio_sets_no_event_loop_up_for_a_test_without_async_fixtures(suite, agent):
    if asyncio_version() < (1, 0):
        pytest.skip("pytest-asyncio 0.23 has no event loop runners")
    # The loop factory every runner of pytest-asyncio's requests, overridden
    # to mark its setup: set up before the async test begins, and never for
    # the test that has no async fixture.
    suite.makeconftest("""
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def _asyncio_loop_factory(_asyncio_loop_factory):
    mark("loop factory")
    return _asyncio_loop_factory
""")
    suite.makepyfile(test_a="""
import pytest
from markhelp import mark

@pytest.fixture(scope="module")
def mod():
    mark("module setup")

@pytest.fixture
def fn(mod):
    mark("function setup")

def test_sync(fn):
    mark("sync body")

async def test_async():
    mark("async body")
""")
    r = run(suite, "-o", "asyncio_mode=auto")
    r.assert_outcomes(passed=2)
    assert agent.log() == ["claim .", "module setup", "begin test_a.py::test_sync", "function setup", "sync body",
                           "end test_a.py::test_sync passed", "loop factory", "begin test_a.py::test_async", "async body",
                           "end test_a.py::test_async passed"]


def test_an_async_fixture_without_pytest_asyncio_begins_as_any_other(suite, agent):
    # Without pytest-asyncio (and so without its loop scope setting), an
    # async fixture is run as pytest runs it, inside the test: keploy-pytest
    # looks for no event loop, and the test begins.
    suite.makepyfile(test_a="""
import pytest

@pytest.fixture
async def coro():
    return 1

def test_a(coro):
    pass
""")
    r = run(suite, "-p", "no:asyncio")
    assert agent.log()[:2] == ["claim .", "begin test_a.py::test_a"], (agent.log(), r.outlines)
    assert not [x for x in r.outlines if "unknown configuration value" in x], r.outlines


def test_a_recursive_fixture_fails_with_pytests_own_error(suite, agent):
    # A fixture that requests its own name with none to override: pytest's
    # own error, not one of keploy-pytest's.
    suite.makepyfile(test_a="""
import pytest

@pytest.fixture
def selfish(selfish):
    pass

def test_a(selfish):
    pass
""")
    r = run(suite)
    r.assert_outcomes(errors=1)
    r.stdout.fnmatch_lines(["*recursive dependency involving fixture 'selfish' detected*"])


DJANGO_SETTINGS = """
SECRET_KEY = "rows"
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
"""


def test_pytest_djangos_test_database_is_set_up_before_the_test_begins(suite, agent):
    # pytest-django sets its test database (the session fixture
    # django_db_setup) up from inside a test, for the django_db marker and
    # for live_server; for db, pytest sets it up before the test.
    suite.makepyfile(settings=DJANGO_SETTINGS)
    suite.makeconftest("""
import pytest
from markhelp import mark

@pytest.fixture(scope="session")
def django_db_setup(django_db_setup):
    mark("test database created")
    yield
    mark("test database torn down")
""")
    body = """
    from django.contrib.auth.models import User
    mark("users %d" % User.objects.count())
"""
    suite.makepyfile(test_marker="import pytest\nfrom markhelp import mark\n\n@pytest.mark.django_db\ndef test_one():" + body,
                     test_transaction="import pytest\nfrom markhelp import mark\n\n@pytest.mark.django_db(transaction=True)\n"
                                      "def test_one():" + body,
                     test_db="from markhelp import mark\n\ndef test_one(db):" + body,
                     test_both="import pytest\nfrom markhelp import mark\n\n@pytest.mark.django_db\n"
                               "def test_one(db, request):" + body +
                               "    assert request.fixturenames.count('django_db_setup') == 1\n",
                     test_live="from markhelp import mark\n\ndef test_one(live_server):" + body)
    for f in ("test_marker.py", "test_transaction.py", "test_db.py", "test_both.py", "test_live.py"):
        agent.calls.clear()
        r = run(suite, "--ds=settings", f)
        r.assert_outcomes(passed=1)
        assert agent.log() == ["claim .", "test database created", "begin %s::test_one" % f, "users 0",
                               "end %s::test_one passed" % f, "test database torn down"], f
    # A fixture that overrides pytest-django's db by its name: the database
    # is set up before the test begins, through the db it overrides.
    suite.makepyfile(test_override="import pytest\nfrom markhelp import mark\n\n@pytest.fixture\ndef db(db):\n    pass\n\n"
                                   "def test_one(db):" + body)
    agent.calls.clear()
    r = run(suite, "--ds=settings", "test_override.py")
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "test database created", "begin test_override.py::test_one", "users 0",
                           "end test_override.py::test_one passed", "test database torn down"]


def test_without_pytest_django_its_marker_and_live_server_are_plain(suite, agent):
    suite.makeconftest("""
import pytest
from markhelp import mark

def pytest_configure(config):
    config.addinivalue_line("markers", "django_db: a marker of the suite's own")

@pytest.fixture
def live_server():
    mark("own live_server")

@pytest.fixture(scope="session")
def django_db_setup():
    mark("own django_db_setup")
""")
    suite.makepyfile(test_a="""
import pytest
from markhelp import mark

@pytest.mark.django_db
def test_m():
    mark("m body")

def test_l(live_server):
    mark("l body")
""")
    r = run(suite, "-p", "no:django")
    r.assert_outcomes(passed=2)
    assert agent.log() == ["claim .", "begin test_a.py::test_m", "m body", "end test_a.py::test_m passed",
                           "begin test_a.py::test_l", "own live_server", "l body", "end test_a.py::test_l passed"]


def test_the_lists_at_the_runs_end_come_through_pytest_xdist(suite, agent):
    # pytest-xdist's controller prints the lists from its workers' reports.
    agent.script["/agent/scope/begin"] = lambda body, n: ok(action="skip", reason="quarantined/flaky: set aside by a row") \
        if body["name"] == "test_b" else ok()
    suite.makeconftest(CUSTOM + WIDER)
    (suite.path / "a.spec").write_text("x\n")
    suite.makepyfile(test_a="""
def test_a(request):
    request.getfixturevalue("db")

def test_b():
    pass
""")
    for args in ((), ("-n", "1")):
        r = run(suite, *args)
        r.assert_outcomes(passed=2, skipped=1, errors=1)
        r.stdout.fnmatch_lines([
            "*= keploy: 1 test(s) failed: a fixture of a wider scope was set up inside them =*",
            "each fixture's calls would count as the test's; set it up before the test begins (each test's error says how):",
            "test_a.py::test_a: db (session scope)",
            "*= keploy: 1 test(s) set aside, skipped =*",
            "test_a.py::test_b: keploy: quarantined/flaky: set aside by a row",
            "*= keploy: 1 item(s) not judged by keploy =*",
            "they use no pytest fixtures, so keploy-pytest cannot begin or end them; their calls are the test process's:",
            "a.spec::spec1",
        ])


def test_items_that_use_no_fixtures_are_not_judged_and_named_at_the_end(suite, agent):
    # A custom pytest.Item (as a linter plugin's) sets up no fixture: it runs
    # as pytest runs it, and the run's end names it.
    suite.makeconftest(CUSTOM)
    (suite.path / "a.spec").write_text("x\n")
    suite.makepyfile(test_a=BODY)
    r = run(suite)
    r.assert_outcomes(passed=2)
    assert agent.log() == ["claim .", "custom item ran: spec1", "begin test_a.py::test_a", "body", "end test_a.py::test_a passed"]
    r.stdout.fnmatch_lines(["*= keploy: 1 item(s) not judged by keploy =*",
                            "they use no pytest fixtures, so keploy-pytest cannot begin or end them; their calls are the test process's:",
                            "a.spec::spec1"])
    # After a failed claim, such an item still runs as pytest runs it; every
    # test that uses fixtures errors.
    agent.calls.clear()
    agent.script["/agent/instance"] = lambda body, n: (500, "boom")
    r = run(suite)
    r.assert_outcomes(passed=1, errors=1)
    assert agent.log() == ["claim .", "custom item ran: spec1"]
    r.stdout.fnmatch_lines(["*= keploy: 1 item(s) not judged by keploy =*"])


CUSTOM = """
import pytest
from markhelp import mark

class SpecItem(pytest.Item):
    def runtest(self):
        mark("custom item ran: " + self.name)

class SpecFile(pytest.File):
    def collect(self):
        yield SpecItem.from_parent(self, name="spec1")

def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".spec":
        return SpecFile.from_parent(parent, path=file_path)
"""


def test_file_named_from_keploys_project_folder(suite, agent, monkeypatch):
    # The project folder is a symlink, the tests sit in a folder under it,
    # and pytest runs from that folder: the file is named from the project.
    real = suite.path / "real"
    real.mkdir()
    (real / "sub").mkdir()
    (real / "sub" / "test_x.py").write_text("def test_x():\n    pass\n")
    link = suite.path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("KEPLOY_MOCK_PROJECT", str(link))
    monkeypatch.chdir(link / "sub")
    r = suite.runpytest_subprocess("-p", "no:cacheprovider", "test_x.py")
    r.assert_outcomes(passed=1)
    assert agent.calls[0][1] == {"key": ".", "dir": str(real)}
    assert agent.log()[1:] == ["begin sub/test_x.py::test_x", "end sub/test_x.py::test_x passed"]


def test_file_named_through_a_symlink(suite, agent, monkeypatch):
    # pytest is given the test file by a path through the project's
    # symlink (the working directory is no symlink): the file is named from
    # where it is, under the project's real folder.
    real = suite.path / "real"
    real.mkdir()
    (real / "sub").mkdir()
    (real / "sub" / "test_x.py").write_text("def test_x():\n    pass\n")
    link = suite.path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("KEPLOY_MOCK_PROJECT", str(link))
    r = suite.runpytest_subprocess("-p", "no:cacheprovider", "link/sub/test_x.py")
    r.assert_outcomes(passed=1)
    assert agent.log()[1:] == ["begin sub/test_x.py::test_x", "end sub/test_x.py::test_x passed"]


def test_dir_is_the_working_directory_without_a_project(suite, agent, monkeypatch):
    monkeypatch.delenv("KEPLOY_MOCK_PROJECT")
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    run(suite).assert_outcomes(passed=1)
    assert agent.calls[0][1]["dir"] == os.path.realpath(str(suite.path))


def test_token_and_test_pid(suite, agent, monkeypatch):
    monkeypatch.setenv("KEPLOY_MOCK_AGENT_TOKEN", "s3cret")
    monkeypatch.setenv("KEPLOY_MOCK_AGENT", agent.url + "/")
    suite.makepyfile(test_a='''
import os
from markhelp import mark

def test_a():
    mark("pid " + ("self" if os.environ["KEPLOY_TEST_PID"] == str(os.getpid()) else os.environ["KEPLOY_TEST_PID"]))
''')
    run(suite).assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "pid self", "end test_a.py::test_a passed"]
    for path, _, headers in agent.calls:
        if path != "/mark":
            assert headers.get("Authorization") == "Bearer s3cret", path
            assert headers.get("Content-Type") == "application/json", path


def test_claim_comes_before_the_suites_sessionstart(suite, agent):
    # A suite's own pytest_sessionstart (one that starts the service under
    # test, say) runs after the claim: what it starts inherits the marker,
    # and joins as a member.
    suite.makeconftest('''
import os
from markhelp import mark

def pytest_sessionstart(session):
    mark("suite sessionstart " + ("claimer" if os.environ.get("KEPLOY_TEST_PID") == str(os.getpid()) else "unclaimed"))
''')
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    run(suite).assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "suite sessionstart claimer", "begin test_a.py::test_a", "end test_a.py::test_a passed"]


def test_member_of_a_claimed_process_is_inert(suite, agent, monkeypatch):
    # This process (the rows') runs the suite's: a marker naming it makes
    # the suite's process a member.
    monkeypatch.setenv("KEPLOY_TEST_PID", str(os.getpid()))
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    run(suite).assert_outcomes(passed=1)
    assert agent.calls == []


def test_marker_of_another_process_claims(suite, agent, monkeypatch):
    # pid 1 is no ancestor of the suite's process inside a pid namespace of
    # its own only; a pid no process has is never one.
    monkeypatch.setenv("KEPLOY_TEST_PID", str(2 ** 22 + 7))
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    run(suite).assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "end test_a.py::test_a passed"]


def test_skip_set_aside(suite, agent):
    agent.script["/agent/scope/begin"] = lambda body, n: ok(action="skip", reason="quarantined/flaky: set aside by a row") \
        if body["name"] == "test_b" else ok()
    suite.makepyfile(test_a='''
import pytest
from markhelp import mark

@pytest.fixture
def own():
    mark("own fixture")

def test_a():
    pass

def test_b(own):
    mark("b body")
''')
    r = run(suite)
    r.assert_outcomes(passed=1, skipped=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "end test_a.py::test_a passed", "begin test_a.py::test_b"]
    r.stdout.fnmatch_lines([
        "*= keploy: 1 test(s) set aside, skipped =*",
        "test_a.py::test_b: keploy: quarantined/flaky: set aside by a row",
        "*short test summary info*",
        "SKIPPED [[]1[]] test_a.py:11: keploy: quarantined/flaky: set aside by a row",
    ])


def test_skip_without_reason(suite, agent):
    agent.script["/agent/scope/begin"] = lambda body, n: ok(action="skip")
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(skipped=1)
    r.stdout.fnmatch_lines(["SKIPPED * keploy: set aside (the agent gave no reason)"])


def test_end_fails_the_test(suite, agent):
    agent.script["/agent/scope/end"] = lambda body, n: ok(action="fail", reason="keploy: GET /price/pen had no recorded answer") \
        if body["name"] == "test_a" else ok()
    suite.makepyfile(test_a="def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(passed=2, errors=1)
    assert r.ret == 1
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_a*", "keploy: GET /price/pen had no recorded answer"])


def test_end_fail_without_reason_and_beside_a_failed_teardown(suite, agent):
    agent.script["/agent/scope/end"] = lambda body, n: ok(action="fail")
    suite.makepyfile(test_a='''
import pytest

@pytest.fixture
def broken():
    yield
    raise RuntimeError("teardown broke")

def test_a(broken):
    pass
''')
    r = run(suite)
    r.assert_outcomes(passed=1, errors=1)
    r.stdout.fnmatch_lines(["*RuntimeError: teardown broke*", "*keploy*", "keploy: the keploy agent failed this test and gave no reason"])


def test_outcomes(suite, agent):
    suite.makepyfile(test_a='''
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup broke")

def test_pass():
    pass

def test_fail():
    assert False

def test_skip_inside():
    pytest.skip("not today")

@pytest.mark.xfail
def test_xfail():
    assert False

@pytest.mark.xfail(strict=True)
def test_xpass_strict():
    pass

@pytest.mark.xfail
def test_xpass():
    pass

def test_setup_error(broken):
    pass

@pytest.mark.skip
def test_marked_skip():
    pass
''')
    run(suite)
    ends = [line for line in agent.log() if line.startswith("end ")]
    assert ends == [
        "end test_a.py::test_pass passed",
        "end test_a.py::test_fail failed",
        "end test_a.py::test_skip_inside skipped",
        "end test_a.py::test_xfail skipped",
        "end test_a.py::test_xpass_strict failed",
        "end test_a.py::test_xpass passed",
        "end test_a.py::test_setup_error failed",
    ]
    assert "begin test_a.py::test_marked_skip" not in agent.log()


RERUN = '''
from _pytest.runner import runtestprotocol

def pytest_runtest_protocol(item, nextitem):
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    runtestprotocol(item, nextitem=nextitem)
    runtestprotocol(item, nextitem=nextitem)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
'''


def test_rerun_fails_closed(suite, agent):
    # What a rerun plugin does: run an item's protocol again.
    suite.makeconftest(RERUN)
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(passed=1, errors=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "end test_a.py::test_a passed"]
    r.stdout.fnmatch_lines(["keploy: test_a in test_a.py would begin a second time, and keploy names a test by its file and name: "
                            "run each test once (no reruns, such as pytest-rerunfailures' --reruns)"])


def test_rerun_after_keploy_failed_the_first_run(suite, agent):
    # keploy's text fails the run whose end got it, once: the rerun errors
    # with its own reason only.
    agent.script["/agent/scope/end"] = lambda body, n: ok(action="fail", reason="keploy: GET /price/pen had no recorded answer")
    suite.makeconftest(RERUN)
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(passed=1, errors=2)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "end test_a.py::test_a passed"]
    assert r.stdout.str().count("keploy: GET /price/pen had no recorded answer") == 1
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_a*", "keploy: GET /price/pen had no recorded answer",
                            "*ERROR at setup of test_a*", "keploy: test_a in test_a.py would begin a second time*"])


def test_claim_404_names_the_switch(suite, agent):
    agent.script["/agent/instance"] = lambda body, n: (404, "404 page not found\n")
    suite.makepyfile(test_a="def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(errors=2)
    assert agent.log() == ["claim ."]
    r.stdout.fnmatch_lines(["keploy: this keploy agent answered 404 to POST /agent/instance, so this pytest process cannot claim keploy's run: "
                            "keploy speaks keploy-pytest's wire, scope protocol v1, only when it runs with KEPLOY_PROVEN=1 "
                            "(KEPLOY_PROVEN=1 keploy mock record|replay -c \"python -m pytest\"); "
                            "a keploy run without KEPLOY_PROVEN=1, or one from before scope protocol v1, answers 404 there"])


def test_claim_refused_errors_every_test(suite, agent):
    agent.script["/agent/instance"] = lambda body, n: (422, json.dumps(
        {"error": "second_instance", "message": "another test process holds this run", "remedy": "run one test process"}))
    suite.makepyfile(test_a="def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(errors=2)
    assert r.stdout.str().count("keploy: another test process holds this run (run one test process)") >= 2


def test_claim_without_instance(suite, agent):
    agent.script["/agent/instance"] = lambda body, n: (200, '{"v": 1}')
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(errors=1)
    r.stdout.fnmatch_lines(["keploy: the keploy agent answered the claim of this test process without an instance"])


def test_refused_agent_url(suite, agent, monkeypatch):
    monkeypatch.setenv("KEPLOY_MOCK_AGENT", "ftp://127.0.0.1:1")
    suite.makepyfile(test_a="def test_a():\n    pass\n")
    r = run(suite)
    r.assert_outcomes(errors=1)
    assert agent.calls == []
    r.stdout.fnmatch_lines(['keploy: KEPLOY_MOCK_AGENT="ftp://127.0.0.1:1" is neither off nor the http URL of a keploy agent; set it to off to run without keploy'])


def test_begin_errors_fail_the_test(suite, agent):
    answers = {
        "test_500": (500, json.dumps({"error": "not_open", "message": "no test is open"})),
        "test_plain": (502, "bad gateway"),
        "test_nov": (200, '{"status": "ok"}'),
        "test_v2": (200, '{"v": 2, "status": "ok"}'),
        "test_status": (200, '{"v": 1, "status": "busy"}'),
        "test_action": (200, '{"v": 1, "status": "ok", "action": "fail"}'),
        "test_redirect": (307, ""),
    }
    agent.script["/agent/scope/begin"] = lambda body, n: answers[body["name"]]
    suite.makepyfile(test_a="\n".join("def %s():\n    pass\n" % name for name in answers))
    r = run(suite)
    r.assert_outcomes(errors=len(answers))
    assert [line for line in agent.log() if line.startswith("end ")] == []
    r.stdout.fnmatch_lines([
        "keploy: no test is open",
        'keploy: the keploy agent answered 502 Bad Gateway to /agent/scope/begin: "bad gateway"',
        'keploy: the keploy agent answered /agent/scope/begin with "{\\"status\\": \\"ok\\"}", which names no scope protocol version; '
        "this keploy-pytest needs a keploy agent that speaks scope protocol v1: upgrade keploy",
        "keploy: the keploy agent answered /agent/scope/begin in scope protocol v2 *; "
        "this keploy-pytest speaks scope protocol v1: use a keploy and a keploy-pytest of the same release",
        'keploy: the keploy agent answered the begin of test_status with status "busy"',
        'keploy: the keploy agent answered the begin of test_action with the unknown action "fail"',
        'keploy: the keploy agent answered 307 Temporary Redirect to /agent/scope/begin: ""',
    ])


def test_end_errors_fail_the_test(suite, agent):
    answers = {
        "test_status": (200, '{"v": 1}'),
        "test_action": (200, '{"v": 1, "status": "ok", "action": "skip"}'),
    }
    agent.script["/agent/scope/end"] = lambda body, n: answers[body["name"]]
    suite.makepyfile(test_a="\n".join("def %s():\n    pass\n" % name for name in answers))
    r = run(suite)
    r.assert_outcomes(passed=2, errors=2)
    r.stdout.fnmatch_lines([
        'keploy: the keploy agent answered the end of test_status with status ""',
        'keploy: the keploy agent answered the end of test_action with the unknown action "skip"',
    ])


def test_xdist_controller_claims_nothing(suite, agent):
    suite.makepyfile(test_a='''
import os
from markhelp import mark

def test_a():
    mark("worker " + ("claimer" if os.environ["KEPLOY_TEST_PID"] == str(os.getpid()) else "member"))
''')
    r = run(suite, "-n", "1")
    r.assert_outcomes(passed=1)
    assert agent.log() == ["claim .", "begin test_a.py::test_a", "worker claimer", "end test_a.py::test_a passed"]
