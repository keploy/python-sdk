"""The rows: real pytest runs (pytester, each in a process of its own) of
small suites under a fake keploy agent, each read by what a user reads (the
outcomes, the report's text) and by the calls the agent got, in order."""
import json
import os

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
