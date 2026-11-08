"""Rows for keploy_agent's control socket: it is bound before the import
returns, and every command read gets one answer line before the close."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "control_driver.py"

TAKEN = "ACK proto=2\n"
IGNORED = "IGNORED proto=2\n"


def run_case(case, tmp_path):
    """Run one case in a child process; return what it saw and its log."""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    env.pop("KEPLOY_APP_SOURCE_DIR", None)
    # subprocess.run kills the child if it outlives the timeout.
    p = subprocess.run(
        [sys.executable, str(DRIVER), json.dumps(case)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert p.returncode == 0, p.stderr
    lines = p.stdout.strip().splitlines()
    assert lines, p.stderr
    return json.loads(lines[-1]), p.stderr


def send(cmd):
    return {"send": cmd}


ANSWER_ROWS = {
    "a START then its END are taken": (
        [send("START t1\n"), send("END t1\n")],
        [TAKEN, TAKEN], ["t1"], None),
    "an END with no START is ignored": (
        [send("END t1\n")],
        [IGNORED], [], None),
    "an END for another test is ignored and keeps the started test": (
        [send("START t1\n"), send("END t2\n"), send("END t1\n")],
        [TAKEN, IGNORED, TAKEN], ["t1"], None),
    "a START with no id is ignored": (
        [send("START\n")],
        [IGNORED], [], None),
    "a blank line is ignored": (
        [send("\n")],
        [IGNORED], [], None),
    "an unknown command is ignored": (
        [send("PING t1\n")],
        [IGNORED], [], None),
    # Sent as the bytes 0xff 0xfe, which are not UTF-8.
    "bytes that are not text are a command that is ignored": (
        [send("\udcff\udcfe\n")],
        [IGNORED], [], None),
    "a START whose counts were not reset is ignored, and clears the test before it": (
        [send("START t1\n"), {"stub": "reset_fails"}, send("START t2\n"),
         send("END t1\n"), send("END t2\n")],
        [TAKEN, IGNORED, IGNORED, IGNORED], [], None),
    "an END whose coverage was not sent is ignored, and still ends the test": (
        [send("START t1\n"), {"stub": "emit_fails"}, send("END t1\n"),
         {"stub": "emit_ok"}, send("END t1\n")],
        [TAKEN, IGNORED, IGNORED], ["t1"], None),
    "a command that raises is ignored": (
        [{"stub": "run_raises"}, send("START t1\n")],
        [IGNORED], [], None),
    "a close with no command gets no answer": (
        [{"send": None}],
        [""], [], None),
    "a START is taken and leaves the test started": (
        [send("START t1\n")],
        [TAKEN], [], "t1"),
}


@pytest.mark.parametrize("name", sorted(ANSWER_ROWS))
def test_answers(name, tmp_path):
    steps, answers, emitted, current = ANSWER_ROWS[name]
    got, log = run_case({"mode": "answers", "steps": steps}, tmp_path)
    assert got["errors"] == [], log
    assert got["answers"] == answers, log
    assert got["emitted"] == emitted, log
    assert got["current_id"] == current, log


def test_a_read_that_fails_gets_no_answer_and_is_said(tmp_path):
    got, log = run_case({"mode": "answers", "steps": [{"silent": True}]},
                        tmp_path)
    assert got["errors"] == [], log
    assert got["answers"] == [""], log
    assert "Could not read a command" in log


def test_socket_is_bound_when_the_import_returns(tmp_path):
    # Every thread the import starts is held, so only what the import itself
    # did is there when it returns: the app binds its port right after. A
    # socket file an earlier app left at the path is replaced.
    got, log = run_case({"mode": "bind_before_ready"}, tmp_path)
    assert "connect_error" not in got, log
    assert got["held"] == ["KeployControlServer"], log
    assert got["answer"] == TAKEN, log


def test_a_socket_that_cannot_be_bound_is_said_and_the_import_goes_on(tmp_path):
    got, log = run_case({"mode": "bind_fails"}, tmp_path)
    assert "import_error" not in got, log
    assert got["closed"] == 1, log
    assert got["srv"] is False, log
    assert "KeployControlServer" not in got["threads"], log
    assert "Could not start the Keploy control server" in log
    assert "Keploy agent ready" not in log
