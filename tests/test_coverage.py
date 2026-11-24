"""Rows for keploy_agent's coverage: the build table of the app's files, the
lines one test's window ran, and the records and frames keploy reads."""

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import signal
import tempfile
import textwrap
import threading
import time
import types

from fake_agent import FakeAgent, command  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "coverage_driver.py"
SRC = ROOT / "src" / "keploy_agent"

TAKEN = "ACK proto=2\n"


def _modules():
    """keploy_agent's _files and _table, loaded without the package's
    __init__ (which would start the agent in pytest's process)."""
    pkg = types.ModuleType("kaprobe")
    pkg.__path__ = [str(SRC)]
    sys.modules.setdefault("kaprobe", pkg)
    return importlib.import_module("kaprobe._files"), importlib.import_module("kaprobe._table")


files_mod, table_mod = _modules()

FIXTURE = textwrap.dedent('''\
    import os


    def outer(x):
        y = x + 1
        def inner(z):
            return z * 2
        return inner(y)


    class Shop:
        rate = 3

        def price(self, n):
            return n * self.rate


    def deco(f):
        return f


    @deco
    def decorated():
        return [i for i in range(3)]


    f1, f2 = (lambda: 1), (lambda: 2)
    ''')


def methods_by_name(u):
    return {m["name"]: [leaf[0] for leaf in m["leaves"]] for m in u["methods"]}


def owner_of(u):
    owner = {}
    for m in u["methods"]:
        for leaf in m["leaves"]:
            assert leaf == [leaf[0], 0, leaf[0], 0, 1, 0]
            assert leaf[0] not in owner, f"line {leaf[0]} in two methods"
            owner[leaf[0]] = m["name"]
    return owner


def test_a_file_is_one_unit_and_each_line_one_method(tmp_path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "orders.py").write_text(FIXTURE)
    h = hashlib.sha256(FIXTURE.encode()).hexdigest()
    u = table_mod.unit(str(tmp_path), "shop/orders.py", h)
    assert list(u)[:7] == ["kind", "id", "pkg", "name", "path", "hash", "state"]
    assert (u["kind"], u["id"], u["pkg"], u["name"], u["path"], u["hash"], u["state"]) == (
        "file", "f:shop/orders.py", "shop", "orders.py", "shop/orders.py", h, "ok")
    owner = owner_of(u)
    # A def line is the defining code's; a body line its function's.
    assert owner[1] == "<module>"
    assert owner[4] == "<module>" and owner[5] == "outer" and owner[8] == "outer"
    assert owner[6] == "outer", "a nested def line stays with the function that defines it"
    assert owner[7] == "outer.<locals>.inner"
    assert owner[11] == "<module>" and owner[12] == "Shop" and owner[14] == "Shop"
    assert owner[15] == "Shop.price"
    assert owner[19] == "deco"
    assert owner[24] == "decorated" or owner[24].startswith("decorated.")
    ids = [m["id"] for m in u["methods"]]
    assert len(ids) == len(set(ids))
    for m in u["methods"]:
        assert m["id"] == f"m:shop/orders.py#{m['name']}@{m['line']}", m
        assert m["line"] >= 1 and m["leaves"], m
    # A one-line lambda's line is its def line: the code that defines it
    # keeps it, and the lambda (two here, with one id) has no line of its own.
    assert owner[27] == "<module>"
    assert not [m for m in u["methods"] if m["name"] == "<lambda>"]
    assert [m["line"] for m in u["methods"]] == sorted(m["line"] for m in u["methods"])


def test_a_file_that_does_not_compile_is_not_measured(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n")
    u = table_mod.unit(str(tmp_path), "bad.py", "0" * 64)
    assert (u["pkg"], u["name"], u["id"]) == ("", "bad.py", "f:bad.py")
    assert u["state"] == "not_measured"
    assert u["reason"] == {"code": "analyzer_failed"}
    assert u["methods"] == []
    assert list(u) == ["kind", "id", "pkg", "name", "path", "hash", "state", "reason", "methods"]


def test_a_file_changed_since_the_build_was_named_is_not_measured(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    old = hashlib.sha256(b"x = 1\n").hexdigest()
    assert table_mod.unit(str(tmp_path), "a.py", old)["state"] == "ok"
    (tmp_path / "a.py").write_text("x = 2\n")
    u = table_mod.unit(str(tmp_path), "a.py", old)
    assert (u["state"], u["reason"], u["methods"], u["hash"]) == (
        "not_measured", {"code": "analyzer_failed"}, [], old)


def test_the_encoding_is_go_json(tmp_path):
    u = {"kind": "file", "id": "f:a\u2028.py", "pkg": "", "name": "a\u2028.py",
         "path": "a\u2028.py", "hash": "0" * 64, "state": "ok",
         "methods": [{"id": "m:a#<module>@1", "name": "<b>&", "line": 1,
                      "leaves": [[1, 0, 1, 0, 1, 0]]}]}
    line = table_mod.encode(u)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert b" " not in line
    assert b"\\u2028" in line and "\u2028".encode() not in line
    assert b'"<b>&"' in line   # no HTML escaping
    assert line.startswith(b'{"kind":"file","id":')


def test_units_are_in_the_contract_order(tmp_path):
    for p in ["a-x/z.py", "a/b/y.py", "a/x.py", "top.py"]:
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("x = 1\n")
    hashes = table_mod.file_hashes(str(tmp_path), ["a-x/z.py", "a/b/y.py", "a/x.py", "top.py"])
    us = table_mod.units(str(tmp_path), hashes)
    # "" < a < a/b < a-x: a package and those under it are one run.
    assert [u["path"] for u in us] == ["top.py", "a/x.py", "a/b/y.py", "a-x/z.py"]


def test_the_build_id_names_the_bytes_and_the_python(tmp_path, monkeypatch):
    b1 = table_mod.build_id({"a.py": "1" * 64, "b/c.py": "2" * 64}, "0.2.0")
    assert b1.startswith("py1-") and len(b1) == 4 + 32
    assert int(b1[4:], 16) >= 0
    assert table_mod.build_id({"b/c.py": "2" * 64, "a.py": "1" * 64}, "0.2.0") == b1
    assert table_mod.build_id({"a.py": "1" * 64, "b/c.py": "3" * 64}, "0.2.0") != b1
    assert table_mod.build_id({"a.py": "1" * 64}, "0.2.0") != b1
    assert table_mod.build_id({"a.py": "1" * 64, "b/c.py": "2" * 64}, "0.2.1") != b1
    assert table_mod.build_id({}, "0.2.0") == ""
    assert table_mod.build_id({"a.py": "1" * 64, "b/c.py": "2" * 64}, "0.2.0", sys.flags.optimize) == b1
    assert table_mod.build_id({"a.py": "1" * 64, "b/c.py": "2" * 64}, "0.2.0",
                              1 - min(sys.flags.optimize, 1)) != b1
    monkeypatch.setattr(table_mod, "runtime", lambda: "python 2.7")
    assert table_mod.build_id({"a.py": "1" * 64, "b/c.py": "2" * 64}, "0.2.0") != b1


def test_the_table_is_compiled_at_the_processs_optimize_level(tmp_path):
    src = "def f(x):\n    assert x\n    return x\n"
    (tmp_path / "a.py").write_text(src)
    h = hashlib.sha256(src.encode()).hexdigest()
    lines = lambda o: owner_of(table_mod.unit(str(tmp_path), "a.py", h, o))  # noqa: E731
    assert 2 in lines(0)
    assert 2 not in lines(1), "-O drops the assert line"


def test_frames_carry_a_part_whole(monkeypatch):
    monkeypatch.setattr(table_mod, "MAX_PAYLOAD", 10)
    payload = b'{"a":1}\n{"b":22}\n{"c":333}\n'
    bodies = list(table_mod.frames("table", "py1-" + "0" * 32, payload))
    assert len(bodies) == 3
    got = b""
    for i, body in enumerate(bodies):
        line, _, chunk = body.partition(b"\n")
        hd = json.loads(line)
        assert (hd["v"], hd["part"], hd["seq"], hd["last"]) == (1, "table", i, i == 2)
        assert ("records" in hd) == (i == 2)
        got += chunk
    assert got == payload
    assert hd["records"] == 3 and hd["sha256"] == hashlib.sha256(payload).hexdigest()
    (empty,) = list(table_mod.frames("probes", "b", b""))
    hd = json.loads(empty.partition(b"\n")[0])
    assert hd == {"v": 1, "part": "probes", "build": "b", "seq": 0, "last": True,
                  "records": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    assert empty.endswith(b"\n")


def make_app(tmp_path):
    app = tmp_path / "app"
    (app / "shop").mkdir(parents=True)
    (app / "shop" / "__init__.py").write_text("")
    (app / "shop" / "orders.py").write_text(textwrap.dedent('''\
        def a():
            x = 1
            return x + 1


        def b():
            y = 2
            return y * 3


        def both():
            return a() + b()
        '''))
    # Not app files: a test, a venv, a hidden dir, and an excluded glob.
    (app / "tests").mkdir()
    (app / "tests" / "test_orders.py").write_text("def test_x():\n    pass\n")
    (app / "test_top.py").write_text("x = 1\n")
    (app / ".venv" / "lib").mkdir(parents=True)
    (app / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (app / ".venv" / "lib" / "dep.py").write_text("x = 1\n")
    (app / "env2").mkdir()
    (app / "env2" / "pyvenv.cfg").write_text("home = /usr\n")
    (app / "env2" / "dep.py").write_text("x = 1\n")
    (app / "scripts").mkdir()
    (app / "scripts" / "seed.py").write_text("x = 1\n")
    return app


def run_dir(d):
    """The run's control dir, as keploy makes it: 1777."""
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o1777)
    return d


def run_case(tmp_path, app, steps, env=None, **extra):
    d = tempfile.mkdtemp(prefix="kv")
    try:
        case = {"data": os.path.join(d, "d.sock"), "app": str(app), "steps": steps, **extra}
        run_dir(os.path.join(d, "ctl"))
        e = dict(os.environ, PYTHONPATH=str(ROOT / "src"), KEPLOY_APP_SOURCE_DIR=str(app),
                 KEPLOY_COVERAGE_EXCLUDE="scripts/*", KEPLOY_COVERAGE_CONTROL_DIR=os.path.join(d, "ctl"))
        e.update(env or {})
        e.pop("KEPLOY_AGENT_FIRST_PID", None)
        p = subprocess.run([sys.executable, str(DRIVER), json.dumps(case)], cwd=tmp_path,
                           env=e, capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["errors"] == [], p.stderr
    return got, p.stderr


def records(got):
    return [r["record"] for r in got["records"] if "id" in r["record"]]


def test_a_window_holds_only_its_own_lines(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},          # before any window: in none
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"call": "shop.orders:b"}, {"cmd": "END s/t2"},
        {"cmd": "START s/t3"}, {"thread": "shop.orders:both"}, {"cmd": "END s/t3"},
        {"cmd": "START s/t4"}, {"cmd": "END s/t4"},
        {"cmd": "END s/t9"},
        {"wait": "records", "n": 5},
    ])
    want_producer = "sys.monitoring" if sys.version_info >= (3, 12) else "settrace"
    assert got["producer"] == want_producer, log
    assert got["answers"][:8] == [TAKEN] * 8, log
    assert got["answers"][8].startswith("IGNORED proto=2"), log
    recs = {r["id"]: r for r in records(got)}
    assert set(recs) == {"s/t1", "s/t2", "s/t3", "s/t4"}, log
    assert recs["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert recs["s/t2"]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}
    assert recs["s/t3"]["executedLinesByFile"] == {"shop/orders.py": [2, 3, 7, 8, 12]}
    assert recs["s/t4"]["executedLinesByFile"] == {}
    for r in got["records"]:
        assert r["first_key"] in ("id", "kind")
        assert "windowCut" not in r["record"]
        assert "partial" not in r["record"], r
    (idx,) = [r["record"] for r in got["records"] if r["record"].get("kind") == "index"]
    orders = hashlib.sha256((app / "shop" / "orders.py").read_bytes()).hexdigest()
    for r in recs.values():
        # The build, or "" when it was still being read at END (the file
        # hashes are exact either way).
        assert r["pid"] == got["driver_pid"] and r["build"] in ("", idx["build"])
        assert r["fileHashes"] == {f: orders for f in r["executedLinesByFile"]}
        assert list(r)[:5] == ["id", "executedLinesByFile", "pid", "build", "fileHashes"]


def test_the_startup_record_holds_the_lines_before_the_first_window(tmp_path):
    """Import time and the app's start run lines no test ran: they come in
    the process's STARTUP record, once, before its first START is
    answered, and in no test's record."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:a"},                  # the import: 1, 6, 11; a: 2, 3
        {"cmd": "START s/t1"}, {"call": "shop.orders:b"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 4},
    ])
    kinds = [r["record"].get("kind", "test") for r in got["records"]]
    assert kinds.count("startup") == 1, kinds
    startup = [r for r in got["records"] if r["record"].get("kind") == "startup"][0]
    assert startup["first_key"] == "kind"
    rec = startup["record"]
    assert list(rec) == ["kind", "v", "pid", "build", "executedLinesByFile", "fileHashes"]
    assert rec["v"] == 1 and rec["pid"] == got["driver_pid"] and rec["build"].startswith("py1-")
    want = {"shop/orders.py": [1, 2, 3, 6, 11]}
    if sys.version_info < (3, 11):
        want["shop/__init__.py"] = [1]   # 3.9 and 3.10 compile an empty module to line 1
    assert rec["executedLinesByFile"] == want
    assert set(rec["fileHashes"]) == set(want)
    recs = {r["id"]: r for r in records(got)}
    assert recs["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}


def test_an_idle_pool_thread_the_test_made_is_the_tests_own(tmp_path):
    """A pool whose thread the test made, ran app code once and waits idle
    at its END (a stdlib target, no app frame): the test's own, done."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t2"}, {"idle_pool": "shop.orders:b"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 3},
    ])
    recs = {r["id"]: r for r in records(got)}
    assert "partial" not in recs["s/t2"], recs["s/t2"]
    assert recs["s/t2"]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}


def test_a_thread_from_before_the_test_running_in_it_makes_it_partial(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t0"}, {"cmd": "END s/t0"},
        # A scheduler-like thread, started between tests, runs app code on
        # its own all the time.
        {"spawn_thread": "shop.orders:b", "seconds": 1.0, "name": "scheduler"},
        {"sleep": 0.1},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"sleep": 0.05}, {"cmd": "END s/t1"},
        {"sleep": 1.0}, {"threads_now": True},
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 4},
    ])
    recs = {r["id"]: r for r in records(got)}
    assert "scheduler" not in got["threads_now"][0], got["threads_now"]
    p = recs["s/t1"]["partial"]
    assert "app code that is not the test's ran during it (thread scheduler)" in p, recs["s/t1"]
    # Once it ended, a test is its own again.
    assert "partial" not in recs["s/t2"], (recs["s/t2"].get("partial"), got["threads_now"], log[-3000:])


def test_lines_that_keep_coming_cut_the_window(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"},
        {"stub": "lines_keep_coming"}, {"cmd": "END s/t1"}, {"stub": "lines_settle"},
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 3},
    ])
    assert got["answers"] == [TAKEN] * 4, log
    recs = {r["id"]: r for r in records(got)}
    assert recs["s/t1"]["windowCut"] is True
    assert recs["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert "windowCut" not in recs["s/t2"]


def test_a_forked_child_binds_its_own_socket_and_is_measured_too(tmp_path):
    """A prefork worker is a process of its own: it binds <pid>.sock, starts
    with no window, and keploy's commands reach it and the parent alike;
    each sends its own record, which keploy merges."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"fork": True},
        {"fork_worker": "start"},
        {"cmd_all": "START s/t1"}, {"call": "shop.orders:a"}, {"sleep": 0.05}, {"cmd_all": "END s/t1"},
        {"fork_worker": "stop"},
        {"wait": "records", "n": 4},
    ])
    child = got["forked"]
    assert child["collector"] is True and child["current"] is None, log
    assert os.path.dirname(child["sock"]) == os.path.dirname(got["sock"])
    assert os.path.basename(child["sock"]).endswith(f"-{child['pid']}.sock")
    (worker_sock,) = got["worker_socks"]
    worker = int(os.path.basename(worker_sock)[:-5].rsplit("-", 1)[1])
    assert [a for _, a in got["all_answers"]] == [TAKEN] * 4, got["all_answers"]
    recs = [r for r in records(got) if r["id"] == "s/t1"]
    by_pid = {r["pid"]: r for r in recs}
    assert by_pid[got["driver_pid"]]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert by_pid[worker]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}
    assert os.path.basename(got["sock"]).endswith(f"-{got['driver_pid']}.sock")


def test_a_worker_forked_from_c_binds_its_own_socket_at_its_first_app_line(tmp_path):
    """uWSGI's and mod_wsgi's workers are forked from C: no at-fork handler
    runs. The agent finds it at the worker's first app line (its pid is not
    the one that bound the socket), runs the fork handling then, binds the
    worker's own socket, and the parent's socket answers only for the
    parent."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:a"}, {"cfork_worker": True},
        {"cmd_all": "START s/t1"}, {"call": "shop.orders:a"}, {"sleep": 0.05}, {"cmd_all": "END s/t1"},
        {"fork_worker": "stop"},
        {"wait": "records", "n": 4},
    ])
    (worker_sock,) = got["worker_socks"]
    assert worker_sock, log
    worker = int(os.path.basename(worker_sock)[:-5].rsplit("-", 1)[1])
    assert [a for _, a in got["all_answers"]] == [TAKEN] * 4, got["all_answers"]
    by_pid = {r["pid"]: r for r in records(got) if r["id"] == "s/t1"}
    assert set(by_pid) == {got["driver_pid"], worker}, by_pid
    assert by_pid[got["driver_pid"]]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert by_pid[worker]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}
    assert by_pid[got["driver_pid"]]["requests"] == 1 and by_pid[worker]["requests"] == 0


def test_a_fork_that_execs_at_once_binds_no_socket(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [{"fork_exec": True}])
    assert got["exec_child_sock"] is False, log


def test_an_import_in_the_window_is_the_windows_and_line_0_is_no_line(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"call": "shop.orders:both"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 3},
        {"cmd": "TABLE"}, {"wait": "frames", "n": 2},
    ])
    # Every line a record names is a line of its file's unit in the table:
    # keploy maps a line to its leaf, and a line with none is a mismatch.
    table = {}
    for u in (json.loads(x) for x in got["parts"][0]["stream"].splitlines()):
        table[u["path"]] = {leaf[0] for m in u["methods"] for leaf in m["leaves"]}
    for r in records(got):
        for f, lines in r["executedLinesByFile"].items():
            assert set(lines) <= table[f], (r["id"], f, lines, table[f])
    want = {"shop/orders.py": [1, 2, 3, 6, 11]}
    # The empty shop/__init__.py runs no line from 3.11 on (its entry is line
    # 0, no line); 3.9 and 3.10 compile its one return to line 1, which their
    # table holds too.
    if sys.version_info < (3, 11):
        want["shop/__init__.py"] = [1]
    assert {r["id"]: r for r in records(got)}["s/t1"]["executedLinesByFile"] == want, log


def test_a_forked_child_that_exits_leaves_the_parents_socket(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"fork_exit": True},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    assert got["sock_after_child_exit"] is True, log
    assert got["answers"] == [TAKEN, TAKEN], log


def test_a_files_mtime_never_matters_only_its_bytes(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
        {"touch_app": True},
        {"cmd": "TABLE"}, {"wait": "frames", "n": 2},
    ])
    units = [json.loads(x) for x in got["parts"][0]["stream"].splitlines()]
    assert [u["state"] for u in units] == ["ok", "ok"], log


def test_a_callback_error_is_swallowed_and_put_on_the_tests_record(tmp_path):
    """Lines a failing callback lost: the window's record is partial (the
    log line does not take that away), each time it happens."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"stub": "rel_fails"}, {"cmd": "END s/t1"},
        {"stub": "rel_ok"},
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"cmd": "START s/t3"}, {"stub": "rel_fails", "module": "late2"}, {"cmd": "END s/t3"},
        {"wait": "records", "n": 5},
    ])
    assert got["late"] == 1, log
    assert "keploy's line callback failed in" in log and "(RuntimeError)" in log, log
    assert "keploy's line callback failed in" in partial_of(got, "s/t1"), partial_of(got, "s/t1")
    assert partial_of(got, "s/t2") == "", partial_of(got, "s/t2")
    assert "keploy's line callback failed in" in partial_of(got, "s/t3"), partial_of(got, "s/t3")


def test_a_tracer_that_replaces_keploys_is_said(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"settrace_other": True},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
    ])
    if sys.version_info >= (3, 12):
        # sys.monitoring is not settrace's: another tracer leaves it be.
        assert got["answers"] == [TAKEN, TAKEN], log
    else:
        assert got["answers"][0].startswith("IGNORED proto=2 another tracer replaced keploy's"), log
        assert got["answers"][1].startswith("IGNORED proto=2"), log


def test_a_table_frame_the_agent_refuses_frees_the_next_ask(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
        {"frame_answer": "NO\n"}, {"cmd": "TABLE"},
        {"wait": "frames", "n": 1}, {"wait": "table_idle"},
        {"frame_answer": "OK\n"}, {"max_payload": 64}, {"cmd": "TABLE"},
        {"wait": "frames", "n": 3},
    ])
    assert got["answers"] == [TAKEN] * 4, log
    assert "could not send the build table" in log
    # The second stream comes whole, in many frames through send_frame.
    whole = [p for p in got["parts"] if p["part"] == "table" and p["frames"] > 1]
    assert whole and whole[-1]["records"] == 2, got["parts"]
    assert got["parts"][-1]["part"] == "probes" and got["parts"][-1]["records"] == 0


def test_a_root_with_no_python_file_has_no_table(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "readme.txt").write_text("x\n")
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"}, {"cmd": "TABLE"},
        {"wait": "records", "n": 2},
    ])
    assert got["answers"][:2] == [TAKEN, TAKEN], log
    assert got["answers"][2].startswith("IGNORED proto=2 no build table: no Python file"), log
    (idx,) = [r["record"] for r in got["records"] if r["record"].get("kind") == "index"]
    assert idx["build"] == "" and idx["source"] == "none"


def test_a_system_dir_as_root_measures_nothing_and_says_why(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"}, {"cmd": "TABLE"},
        {"wait": "records", "n": 1},
    ], env={"KEPLOY_APP_SOURCE_DIR": "/"})
    assert got["producer"] is None, log
    for a in got["answers"]:
        assert a.startswith("IGNORED proto=2 keploy cannot measure this process: the app root is /, "
                            "a system directory"), a
    (idx,) = [r["record"] for r in got["records"] if r["record"].get("kind") == "index"]
    assert idx["build"] == "" and idx["source"] == "none" and idx["classes"] == 0
    assert "a system directory" in idx["launch"]


def test_the_index_goes_to_each_new_data_socket(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"cmd": "END s/t2"},
        {"rebind": True},
        {"cmd": "START s/t3"}, {"cmd": "END s/t3"},
        {"wait": "records", "n": 5},
    ])
    idx = [r for r in got["records"] if r["record"].get("kind") == "index"]
    assert len(idx) == 2, log
    rec = idx[0]["record"]
    assert list(rec)[0] == "kind"
    assert rec["kind"] == "index" and rec["v"] == 1 and rec["language"] == "python"
    assert rec["build"].startswith("py1-") and len(rec["build"]) == 36
    assert rec["source"] == "root" and rec["roots"] == [os.path.realpath(app)]
    assert rec["rootCount"] == 1 and rec["sdkVersion"] == "0.2.0"
    assert rec["classes"] == 2, "shop/__init__.py and shop/orders.py, nothing else"
    assert idx[1]["record"]["build"] == rec["build"]


def test_the_table_comes_as_frames_the_agent_takes(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
        {"cmd": "TABLE"},
        {"wait": "frames", "n": 2},
    ])
    assert got["answers"] == [TAKEN, TAKEN, TAKEN], log
    table, probes = got["parts"]
    (idx,) = [r["record"] for r in got["records"] if r["record"].get("kind") == "index"]
    assert table["part"] == "table" and table["build"] == idx["build"]
    assert probes == {"part": "probes", "build": idx["build"], "frames": 1, "records": 0, "stream": ""}
    units = [json.loads(x) for x in table["stream"].splitlines()]
    assert table["records"] == 2
    assert [u["id"] for u in units] == ["f:shop/__init__.py", "f:shop/orders.py"]
    orders = units[1]
    assert orders["hash"] == hashlib.sha256((app / "shop" / "orders.py").read_bytes()).hexdigest()
    assert methods_by_name(orders) == {"<module>": [1, 6, 11], "a": [2, 3], "b": [7, 8], "both": [12]}
    # The stream is the units' own encoding, byte for byte.
    assert table["stream"].encode() == b"".join(table_mod.encode(u) for u in units)
    # The build id is the files' (path, sha256) under this Python.
    assert idx["build"] == table_mod.build_id({u["path"]: u["hash"] for u in units}, "0.2.0")


def test_app_files_leave_out_tests_venvs_and_excludes(tmp_path):
    app = make_app(tmp_path)
    # A conda env (conda-meta, no pyvenv.cfg), a pip --target dir and an
    # interpreter's own dir under the root are libraries, not the app.
    (app / "env3" / "conda-meta").mkdir(parents=True)
    (app / "env3" / "lib" / "python3.11").mkdir(parents=True)
    (app / "env3" / "lib" / "python3.11" / "os.py").write_text("x = 1\n")
    (app / "vendor" / "dep-1.0.dist-info").mkdir(parents=True)
    (app / "vendor" / "dep").mkdir()
    (app / "vendor" / "dep" / "__init__.py").write_text("x = 1\n")
    (app / "pyroot" / "lib").mkdir(parents=True)
    (app / "pyroot" / "lib" / "abc.py").write_text("x = 1\n")
    # An editable install's egg-info sits beside the app's own source.
    (app / "shop.egg-info").mkdir()
    af = files_mod.AppFiles(str(app), str(SRC), ("scripts/*",), libs=[str(app / "pyroot")])
    assert af.walk() == ["shop/__init__.py", "shop/orders.py"]
    assert af.rel(str(app / "env3" / "lib" / "python3.11" / "os.py")) is None
    assert af.rel(str(app / "vendor" / "dep" / "__init__.py")) is None
    assert af.rel(str(app / "pyroot" / "lib" / "abc.py")) is None
    assert af.rel(str(app / "shop" / "orders.py")) == "shop/orders.py"
    assert af.rel(str(app / "env2" / "dep.py")) is None
    assert af.rel(str(app / "tests" / "test_orders.py")) is None
    assert af.rel(str(app / "scripts" / "seed.py")) is None
    assert af.rel("<frozen os>") is None
    assert af.rel(str(tmp_path / "elsewhere.py")) is None


def test_system_dirs_are_no_app_root_and_a_home_is(monkeypatch, tmp_path):
    for root in ["/", "/usr", "/home", "/etc", "/opt", "/var"]:
        assert "a system directory" in files_mod.refused_root(root)
    # A home dir runs apps (s2i, Heroku): it is a root like any other.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert files_mod.refused_root(str(tmp_path)) == ""
    assert files_mod.refused_root("/opt/app") == ""
    # A root the app names (KEPLOY_APP_SOURCE_DIR) is refused only when /.
    assert files_mod.refused_root("/usr", explicit=True) == ""
    assert "a system directory" in files_mod.refused_root("/", explicit=True)
    af = files_mod.AppFiles("/", str(SRC))
    try:
        af.walk()
    except ValueError as e:
        assert "a system directory" in str(e)
    else:
        raise AssertionError("/ was walked")


def test_the_interpreters_own_files_are_no_app_files():
    import sysconfig
    af = files_mod.AppFiles(os.path.dirname(sysconfig.get_paths()["stdlib"]), str(SRC))
    assert af.rel(os.__file__) is None


def test_pythonpath_keeps_the_users_value():
    main = importlib.import_module("kaprobe.__main__")
    boot = str(SRC / "_boot")
    assert main.pythonpath("") == boot
    assert main.pythonpath("/u/a:/u/b") == boot + os.pathsep + "/u/a:/u/b"
    assert main.pythonpath("/u/a::/u/b") == boot + os.pathsep + "/u/a::/u/b"
    assert main.pythonpath(boot + ":/u/a") == boot + ":/u/a"


def private_copy(tmp_path):
    """A copy of the package whose data socket line names a private dir, and
    that dir for the control sockets (hook_env), so a row that starts the
    agent through the hook never touches keploy's own paths in /tmp."""
    d = tempfile.mkdtemp(prefix="kh")
    src = tmp_path / "pkgsrc"
    shutil.copytree(SRC, src / "keploy_agent")
    init = src / "keploy_agent" / "__init__.py"
    text = init.read_text()
    line = 'DATA_SOCK = "/tmp/coverage_data.sock"'
    assert text.count(line) == 1
    init.write_text(text.replace(line, f"DATA_SOCK = {d + '/d.sock'!r}"))
    return src, d


def hook_env(src, app, row, sockdir, make=True):
    if make:
        run_dir(sockdir + "/ctl")
    env = dict(os.environ, KEPLOY_APP_SOURCE_DIR=str(app), ROW_DIR=str(row),
               KEPLOY_COVERAGE_CONTROL_DIR=sockdir + "/ctl")
    env.pop("KEPLOY_AGENT_FIRST_PID", None)
    env["PYTHONPATH"] = str(src)
    env["PYTHONPATH"] = subprocess.check_output(
        [sys.executable, "-m", "keploy_agent", "--pythonpath"], env=env, text=True).strip() \
        + os.pathsep + str(app)
    return env


def sock_pid(name):
    """The pid in a socket's name (the tests read it; keploy never does):
    <ns>-<pid>.sock, or <pid>-<random>.sock."""
    a, _, b = name[:-5].partition("-")
    return int(b) if b.isdigit() and len(a) > 6 else int(a)


def live_socks(sockdir):
    """The live processes' sockets, {pid: path}, as keploy's agent finds them:
    every *.sock that takes a connection."""
    import socket as socketmod
    d = sockdir + "/ctl"
    out = {}
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".sock"):
            continue
        c = socketmod.socket(socketmod.AF_UNIX)
        try:
            c.connect(os.path.join(d, name))
        except OSError:
            continue
        finally:
            c.close()
        out[sock_pid(name)] = os.path.join(d, name)
    return out


def wait_sock(sockdir, pid, seconds=30):
    deadline = time.monotonic() + seconds
    while pid not in live_socks(sockdir):
        assert time.monotonic() < deadline, f"no socket of {pid}"
        time.sleep(0.02)
    return live_socks(sockdir)[pid]


def command_all(sockdir, cmd):
    return {pid: command(path, cmd) for pid, path in live_socks(sockdir).items()}


def wait_for(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path):
        assert time.monotonic() < deadline, f"{path} never came"
        time.sleep(0.02)


def wait_records(out, lk, want, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with lk:
            recs = [r["record"] for r in out["records"] if "id" in r["record"]]
        if want(recs):
            return recs
        time.sleep(0.02)
    return recs


def test_the_sitecustomize_hook_starts_the_agent_and_chains(tmp_path):
    src, sockdir = private_copy(tmp_path)
    # The user's own sitecustomize, later on PYTHONPATH, still runs.
    user = tmp_path / "user"
    user.mkdir()
    (user / "sitecustomize.py").write_text("import os\nos.environ['USER_SITE_RAN'] = '1'\n")
    app = make_app(tmp_path)
    env = dict(os.environ, KEPLOY_APP_SOURCE_DIR=str(app), KEPLOY_COVERAGE_CONTROL_DIR=sockdir + "/ctl")
    env.pop("KEPLOY_AGENT_FIRST_PID", None)
    env["PYTHONPATH"] = str(src) + os.pathsep + str(user)
    run_dir(sockdir + "/ctl")
    try:
        pp = subprocess.run([sys.executable, "-m", "keploy_agent", "--pythonpath"], env=env,
                            capture_output=True, text=True, timeout=60, cwd=tmp_path)
        assert pp.returncode == 0, pp.stderr
        assert "Keploy" not in pp.stderr, "printing the path starts no agent"
        assert os.listdir(sockdir + "/ctl") == [], "printing the path binds no socket"
        env["PYTHONPATH"] = pp.stdout.strip()
        assert env["PYTHONPATH"] == str(src / "keploy_agent" / "_boot") + os.pathsep + str(src) + os.pathsep + str(user)
        code = ("import os, keploy_agent as k;"
                "print(os.environ.get('USER_SITE_RAN'), k.BOOT, k._collector is not None,"
                " os.path.dirname(k.CONTROL_SOCK) == os.environ['KEPLOY_COVERAGE_CONTROL_DIR'] and k.CONTROL_SOCK.endswith('-%d.sock' % os.getpid()),"
                " os.path.exists(k.CONTROL_SOCK), oct(os.stat(k.CONTROL_SOCK).st_mode & 0o777),"
                " oct(os.stat(os.path.dirname(k.CONTROL_SOCK)).st_mode & 0o7777))")
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, timeout=60, cwd=tmp_path)
        assert p.returncode == 0, p.stderr
        # The dir is made 1777 (as /tmp), the socket 0666: an app's
        # processes may run as several users.
        assert p.stdout.split() == ["1", "True", "True", "True", "True", "0o666", "0o1777"], p.stderr
        # Its thread is a daemon: the process exited, and removed its own
        # socket on the way out.
        assert os.listdir(sockdir + "/ctl") == []
        # Two processes at once: each binds its own.
        hold = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env,
                                stderr=subprocess.PIPE, text=True, cwd=tmp_path)
        try:
            wait_sock(sockdir, hold.pid)
            p2 = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                text=True, timeout=60, cwd=tmp_path)
            assert p2.stdout.split()[:5] == ["1", "True", "True", "True", "True"], p2.stderr
        finally:
            hold.kill()
            hold.wait()
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


def test_a_name_another_pid_namespace_holds_is_never_taken_and_both_are_measured(tmp_path):
    """Two containers that share /tmp can each have a process of one pid:
    the names are <pid namespace inode>-<pid>.sock, and when one is taken
    anyway (a live socket there) the process binds <pid>-<random>.sock: both
    are measured, and neither socket is removed."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row, sockdir)
    code = ("import os, signal; os.kill(os.getpid(), signal.SIGSTOP); import keploy_agent as k;"
            "print(k._srv is not None, k._collector is not None, os.path.basename(k.CONTROL_SOCK))")
    env["PYTHONPATH"] = str(src) + os.pathsep + str(app)   # import mode: the stop comes first
    p = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=tmp_path,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import socket as socketmod
    other = socketmod.socket(socketmod.AF_UNIX)
    ns = os.readlink("/proc/self/ns/pid")
    ns = ns[ns.find("[") + 1:ns.rfind("]")]
    taken = sockdir + f"/ctl/{ns}-{p.pid}.sock"
    try:
        deadline = time.monotonic() + 30
        while True:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "T":
                    break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.makedirs(sockdir + "/ctl", exist_ok=True)
        os.chmod(sockdir + "/ctl", 0o1777)
        other.bind(taken)
        other.listen()
        os.kill(p.pid, signal.SIGCONT)
        out, err = p.communicate(timeout=60)
        srv, coll, name = out.split()
        assert (srv, coll) == ("True", "True"), err
        assert name.startswith(f"{p.pid}-") and name != os.path.basename(taken), name
        assert os.path.exists(taken), "the other's socket was removed"
    finally:
        other.close()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_an_unsafe_control_dir_is_refused(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row, sockdir, make=False)
    os.makedirs(sockdir + "/ctl")
    os.chmod(sockdir + "/ctl", 0o777)          # world-writable, not sticky
    try:
        p = subprocess.run([sys.executable, "-c", "import keploy_agent as k; print(k._srv is not None)"],
                           env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert p.stdout.split() == ["False"], p.stderr
        assert "does not trust its control dir" in p.stderr and "1777" in p.stderr, p.stderr
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


RELOADER_CHILD = """
import os, subprocess, sys, time
import keploy_agent as k
import shop.orders as o
d = os.environ["ROW_DIR"]
open(os.path.join(d, "child-ready.tmp"), "w").write(str(os.getpid()))
os.rename(os.path.join(d, "child-ready.tmp"), os.path.join(d, "child-ready"))
while True:
    if os.path.exists(os.path.join(d, "go")):
        o.a()
        os.remove(os.path.join(d, "go"))
        open(os.path.join(d, "done"), "w").write("")
    time.sleep(0.002)
"""


def test_a_reloader_and_its_server_child_are_both_measured(tmp_path):
    """Django runserver, uvicorn --reload, Flask debug: a parent that serves
    no request starts the child that does. Each binds its own socket, keploy
    sends each command to both, and each sends its own record: the child's
    holds the test's lines, the parent's none."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row, sockdir)
    parent_code = ("import os, subprocess, sys, keploy_agent as k;"
                   f"subprocess.run([sys.executable, '-c', {RELOADER_CHILD!r}],"
                   " env=dict(os.environ, RUN_MAIN='true'))")
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env=env, cwd=tmp_path,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "child-ready")
        child = int((row / "child-ready").read_text())
        wait_sock(sockdir, child)
        assert set(live_socks(sockdir)) == {parent.pid, child}
        assert command_all(sockdir, "START s/t1") == {parent.pid: TAKEN, child: TAKEN}
        (row / "go").write_text("")
        wait_for(row / "done")
        assert command_all(sockdir, "END s/t1") == {parent.pid: TAKEN, child: TAKEN}
        recs = wait_records(out, lk, lambda rs: len([r for r in rs if r["id"] == "s/t1"]) == 2)
        by_pid = {r["pid"]: r for r in recs if r["id"] == "s/t1"}
        assert by_pid[child]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
        assert by_pid[parent.pid]["executedLinesByFile"] == {}
        assert by_pid[child]["build"] == by_pid[parent.pid]["build"]
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        _, err = parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    assert "took the Keploy control socket" not in err and "gave the Keploy" not in err, err


SPAWN_PARENT = """
import multiprocessing as mp, os, socket, sys, time
import keploy_agent


def serve(d):
    import shop.orders as o
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    open(os.path.join(d, "child-ready.tmp"), "w").write(str(os.getpid()))
    os.rename(os.path.join(d, "child-ready.tmp"), os.path.join(d, "child-ready"))
    while True:
        if os.path.exists(os.path.join(d, "go")):
            o.a()
            os.remove(os.path.join(d, "go"))
            open(os.path.join(d, "done"), "w").write("")
        time.sleep(0.002)


if __name__ == "__main__":
    # uvicorn --reload's shape: a spawn context starts multiprocessing's
    # resource tracker, then the server (spawn_main).
    p = mp.get_context("spawn").Process(target=serve, args=(os.environ["ROW_DIR"],))
    p.start()
    p.join()
"""


def children_of(pid):
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, ValueError, IndexError):
            continue
        if ppid == pid:
            out.append((int(d), cmd))
    return out


def test_a_reload_tree_with_a_resource_tracker_merges_reloader_and_server(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    (tmp_path / "parent.py").write_text(SPAWN_PARENT)
    parent = subprocess.Popen([sys.executable, str(tmp_path / "parent.py")], env=hook_env(src, app, row, sockdir),
                              cwd=tmp_path, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "child-ready")
        child = int((row / "child-ready").read_text())
        wait_sock(sockdir, child)
        kids = children_of(parent.pid)
        trackers = [pid for pid, cmd in kids if "multiprocessing.resource_tracker" in cmd]
        assert trackers, kids
        # The tracker never starts the agent: the sockets are the reloader's
        # and the server's.
        assert set(live_socks(sockdir)) == {parent.pid, child}
        assert command_all(sockdir, "START s/t1") == {parent.pid: TAKEN, child: TAKEN}
        (row / "go").write_text("")
        wait_for(row / "done")
        assert command_all(sockdir, "END s/t1") == {parent.pid: TAKEN, child: TAKEN}
        recs = wait_records(out, lk, lambda rs: len([r for r in rs if r["id"] == "s/t1"]) == 2)
        by_pid = {r["pid"]: r for r in recs if r["id"] == "s/t1"}
        assert by_pid[child]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
        assert by_pid[parent.pid]["executedLinesByFile"] == {}
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        _, err = parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    for t in trackers:
        assert f" pid {t} " not in err, err


def test_stdlib_helpers_never_start_the_agent_and_spawn_main_does(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row, sockdir)
    probe = ("import os, keploy_agent as k; print(k._srv is not None,"
             " bool(k.CONTROL_SOCK) and os.path.exists(k.CONTROL_SOCK), k._collector is not None)")
    try:
        # The forkserver measures (its children are the app's workers) and
        # binds no socket.
        for head, want in (("from multiprocessing.resource_tracker import main", "False False False"),
                           ("from multiprocessing.forkserver import main", "False False True"),
                           ("from multiprocessing.spawn import spawn_main", "True True True")):
            p = subprocess.run([sys.executable, "-c", head + "\n" + probe], env=env, cwd=tmp_path,
                               capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, p.stderr
            assert p.stdout.split() == want.split(), (head, p.stdout, p.stderr)
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


def test_an_exclude_glob_prunes_the_walk(tmp_path):
    app = make_app(tmp_path)
    (app / "media" / "deep").mkdir(parents=True)
    (app / "media" / "deep" / "x.py").write_text("x = 1\n")
    seen = []
    import os as osmod
    real_walk = osmod.walk

    def walk(top, *a, **k):
        for d, dirs, files in real_walk(top, *a, **k):
            seen.append(d)
            yield d, dirs, files

    (app / "shop" / "migrations").mkdir()
    (app / "shop" / "migrations" / "0001.py").write_text("x = 1\n")
    files_mod.os.walk = walk
    try:
        af = files_mod.AppFiles(str(app), str(SRC), ("scripts/*", "media", "*/migrations"))
        walked = af.walk()
    finally:
        files_mod.os.walk = real_walk
    assert walked == ["shop/__init__.py", "shop/orders.py"]
    assert not [d for d in seen if os.sep + "media" in d or "migrations" in d], seen
    # One rule: the files rel() takes are exactly those the walk lists.
    every = [str(p.relative_to(app)) for p in app.rglob("*.py")]
    assert sorted(f for f in every if af.rel(str(app / f)) is not None) == walked


def test_a_fork_with_a_command_in_flight_starts_fresh_and_drops_the_connection(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"fork_in_flight": True},
    ])
    # The child measures on, with no window of its parent's and none of its
    # parent's connections.
    child = got["forked_in_flight"]
    assert child == {"collector": True, "current": None, "conns": 0}, log
    # The child closed its copy: the parent's answer, then its close, came.
    assert got["in_flight_answer"] == [TAKEN, True], log




JOBS = '''\
import asyncio
import time

STOP = []


def tick():
    x = 1
    return x


def wait_io(sock):
    data = sock.recv(1)
    return data


def poll_loop():
    while not STOP:
        tick()
        time.sleep(0.2)


async def coro_run():
    return tick()


async def coro_tail():
    await asyncio.sleep(1)
    return 1


async def coro_bg():
    while not STOP:
        tick()
        await asyncio.sleep(0.05)


def cb():
    return 1


def slow():
    time.sleep(0.3)
    return 1
'''


def jobs_app(tmp_path):
    app = make_app(tmp_path)
    (app / "shop" / "jobs.py").write_text(JOBS)
    return app


def partial_of(got, test):
    return {r["id"]: r for r in records(got)}[test].get("partial", "")


def test_a_fork_while_locks_are_held_still_answers(tmp_path):
    """gunicorn forks a worker while the master answers a command (the
    window lock held) or reads its build (the build's lock held): the
    worker re-makes its locks and reads the build itself."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [{"call": "shop.orders:both"}, {"fork_locked": True},
                                        {"fork_worker": "stop"}])
    answer_, took = got["locked_child"]
    assert answer_ == TAKEN and took < 5, (got["locked_child"], log)


def test_a_fork_during_the_test_makes_it_partial_and_one_just_before_does_not(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"fork_quick": True}, {"cmd": "END s/t1"},
        {"fork_quick": True}, {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"sleep": 0.2},
        {"cmd": "START s/t3"}, {"call": "shop.orders:a"}, {"cmd": "END s/t3"},
        {"wait": "records", "n": 5},
    ])
    assert "a process forked during the test (pid " in partial_of(got, "s/t1"), log
    # A child binds in the fork itself: one forked just before the test has
    # its socket, and its own window.
    assert partial_of(got, "s/t2") == "" and partial_of(got, "s/t3") == ""


def test_a_python_process_that_runs_app_code_and_exits_in_the_test_keeps_its_socket(tmp_path):
    """Its lines are in no record: its socket file stays, so keploy sees a
    socket come at the test's END (the test is not verified), and removes
    it as stale before the next test."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"exec_python": "shop.orders:a"}, {"cmd": "END s/t1"},
    ])
    assert len(got["exec_left"]) == 1 and got["exec_left"][0].endswith(".sock"), got


def test_tail_work_found_by_state_makes_the_record_partial(tmp_path):
    app = jobs_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"call": "shop.jobs:tick"},
        {"loop_start": True}, {"loop_run": "shop.jobs:coro_run"},
        # A thread blocked in I/O inside app code at the end.
        {"cmd": "START s/io"}, {"io_thread": "shop.jobs:wait_io"}, {"sleep": 0.05}, {"cmd": "END s/io"},
        {"io_release": True}, {"sleep": 0.1},
        # A Timer that has not fired: and when it fires in the next test,
        # that test is partial too (no gap for it to fire in).
        # (Delays past END's 50 ms wait for the test's own work.)
        {"cmd": "START s/timer"}, {"timer": "shop.jobs:tick", "delay": 0.3}, {"cmd": "END s/timer"},
        {"cmd": "START s/timer2"}, {"sleep": 0.5}, {"cmd": "END s/timer2"},
        # A call_later that has not run, then runs in the next test.
        {"cmd": "START s/later"}, {"loop_call_later": "shop.jobs:cb", "delay": 0.3}, {"cmd": "END s/later"},
        {"cmd": "START s/later2"}, {"sleep": 0.5}, {"cmd": "END s/later2"},
        # The test's own call_later that runs within END's wait: measured.
        {"cmd": "START s/soon"}, {"loop_call_later": "shop.jobs:cb", "delay": 0.06}, {"cmd": "END s/soon"},
        # An asyncio task that awaits past the end.
        {"cmd": "START s/task"}, {"loop_task": "shop.jobs:coro_tail"}, {"cmd": "END s/task"},
        {"sleep": 1.2},
        # Nothing left: the test's own.
        {"cmd": "START s/clean"}, {"call": "shop.orders:a"}, {"cmd": "END s/clean"},
        {"wait": "records", "n": 10},
    ])
    assert "work the test started was still running at its end (threads: io)" in partial_of(got, "s/io"), log
    assert partial_of(got, "s/soon") == "", partial_of(got, "s/soon")
    assert {r["id"]: r for r in records(got)}["s/soon"]["executedLinesByFile"].get("shop/jobs.py") == [39], \
        {r["id"]: r for r in records(got)}["s/soon"]
    assert "work the test started was still running at its end (threads: timer)" in partial_of(got, "s/timer")
    assert "an asyncio task the test created was still pending at its end (coro_tail)" in partial_of(got, "s/task")
    assert "a callback the test scheduled had not run at its end (cb)" in partial_of(got, "s/later")
    assert "app code that is not the test's ran during it (thread timer)" in partial_of(got, "s/timer2"), \
        partial_of(got, "s/timer2")
    assert "app code that is not the test's ran during it (thread loop)" in partial_of(got, "s/later2"), \
        partial_of(got, "s/later2")
    assert partial_of(got, "s/clean") == "", partial_of(got, "s/clean")


def test_background_work_inside_the_test_makes_the_record_partial(tmp_path):
    app = jobs_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"call": "shop.jobs:tick"},
        {"loop_start": True}, {"loop_run": "shop.jobs:coro_run"},
        {"cmd": "START s/t0"}, {"cmd": "END s/t0"},
        # A poller that ticks every 0.2 s, idle in the gaps: a tick inside
        # the test continues an app call that was running at its START.
        {"bg_thread": "shop.jobs:poll_loop"}, {"sleep": 0.1},
        {"cmd": "START s/poll"}, {"call": "shop.orders:a"}, {"sleep": 0.35}, {"cmd": "END s/poll"},
        # An asyncio task from before the test, running app code in it.
        {"loop_task": "shop.jobs:coro_bg"}, {"sleep": 0.1},
        {"cmd": "START s/task"}, {"call": "shop.orders:a"}, {"sleep": 0.2}, {"cmd": "END s/task"},
        {"wait": "records", "n": 4},
    ])
    p = partial_of(got, "s/poll")
    assert "app code that is not the test's ran during it (thread poller)" in p, (p, log)
    p = partial_of(got, "s/task")
    assert "app code that is not the test's ran during it (task " in p and "coro_bg" in p, (p, log)


def test_the_startup_record_goes_to_each_new_receiver(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:a"},
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 3},
        {"rebind": True},
        {"cmd": "START s/t2"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 6},
    ])
    startups = [r["record"] for r in got["records"] if r["record"].get("kind") == "startup"]
    assert len(startups) == 2, [r["record"].get("kind") or r["record"]["id"] for r in got["records"]]
    assert startups[0]["executedLinesByFile"] == startups[1]["executedLinesByFile"]


def test_a_file_changed_after_the_build_makes_the_record_partial(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t0"}, {"cmd": "END s/t0"}, {"wait": "records", "n": 3},
        {"edit_app": "shop/orders.py"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 4},
    ])
    rec = {r["id"]: r for r in records(got)}["s/t1"]
    assert "shop/orders.py changed after this process's build was read" in rec.get("partial", ""), rec
    want = hashlib.sha256((app / "shop" / "orders.py").read_bytes()).hexdigest()
    assert rec["fileHashes"]["shop/orders.py"] == want


FORKSERVER_PARENT = """
import multiprocessing as mp, os, time
import keploy_agent


def warm():
    return os.getpid()


if __name__ == "__main__":
    import shop.orders as o
    d = os.environ["ROW_DIR"]
    pool = mp.get_context("forkserver").Pool(1)
    worker = pool.apply(warm)
    time.sleep(0.3)                 # the worker binds its socket
    open(os.path.join(d, "ready.tmp"), "w").write(str(worker))
    os.rename(os.path.join(d, "ready.tmp"), os.path.join(d, "ready"))
    while not os.path.exists(os.path.join(d, "go")):
        time.sleep(0.01)
    pool.apply(o.a)
    open(os.path.join(d, "done"), "w").write("")
    time.sleep(60)
"""


def test_a_forkserver_pools_workers_are_measured(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    (tmp_path / "fsparent.py").write_text(FORKSERVER_PARENT)
    parent = subprocess.Popen([sys.executable, str(tmp_path / "fsparent.py")],
                              env=hook_env(src, app, row, sockdir), cwd=tmp_path,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "ready")
        worker = int((row / "ready").read_text())
        wait_sock(sockdir, worker)
        socks = live_socks(sockdir)
        # The parent and the pool's worker; the forkserver binds none.
        assert set(socks) == {parent.pid, worker}, socks
        assert set(command_all(sockdir, "START s/t1").values()) == {TAKEN}
        (row / "go").write_text("")
        wait_for(row / "done")
        assert set(command_all(sockdir, "END s/t1").values()) == {TAKEN}
        recs = wait_records(out, lk, lambda rs: len([r for r in rs if r["id"] == "s/t1"]) == 2)
        merged = set()
        for r in recs:
            if r["id"] == "s/t1":
                merged |= set(r["executedLinesByFile"].get("shop/orders.py", []))
        assert {2, 3} <= merged, recs
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_a_socket_name_appears_only_once_it_listens(tmp_path):
    """Bound at a temp name, listening, then renamed: a *.sock in the dir
    always listens, or is dead (keploy reads a refusal as a dead process)."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [{"bind_order": True}])
    assert got["bind_order"] == {"final_at_listen": [False], "temp_left": []}, log
    # The process's umask is the app's: never changed, the socket chmod-ed.
    assert got["umask_kept"] is True and got["sock_mode"] == "0o666"


def test_a_request_served_in_the_gap_marks_no_test(tmp_path):
    """A health probe served by a request thread between tests is its own
    request: the tests the thread serves next are their own."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t0"}, {"call": "shop.orders:a"}, {"cmd": "END s/t0"},
        {"call": "shop.orders:b"},                       # in the gap, on the main thread
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"cmd": "START s/t3"}, {"call": "shop.orders:a"}, {"cmd": "END s/t3"},
        {"wait": "records", "n": 6},
    ])
    assert partial_of(got, "s/t0") == partial_of(got, "s/t1") == partial_of(got, "s/t2") == \
        partial_of(got, "s/t3") == "", log
    # The probe's lines went to the lines between tests.
    between = [r["record"] for r in got["records"] if r["record"].get("between")]
    assert between and between[0]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}, between


def test_a_preexec_fn_subprocess_in_the_test_is_no_fork_of_it(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"fork_exec": True}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 3},
    ])
    assert got["exec_child_sock"] is False
    assert "forked" not in partial_of(got, "s/t1"), partial_of(got, "s/t1")


FSWIN_PARENT = """
import multiprocessing as mp, os, time
import keploy_agent

if __name__ == "__main__":
    import shop.orders as o
    d = os.environ["ROW_DIR"]
    open(os.path.join(d, "ready"), "w").write(str(os.getpid()))
    while not os.path.exists(os.path.join(d, "go")):
        time.sleep(0.01)
    # A pool made and gone inside the window (its workers live a moment).
    with mp.get_context("forkserver").Pool(1) as pool:
        pool.apply(o.a)
    open(os.path.join(d, "done"), "w").write("")
    time.sleep(60)
"""


def test_a_forkserver_pool_made_inside_the_test_leaves_its_workers_sockets(tmp_path):
    """Its workers bind at once and die with the pool: their socket files
    stay, and refuse, so keploy sees processes came and went (the test is
    not verified) rather than lose their lines."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    (tmp_path / "fswin.py").write_text(FSWIN_PARENT)
    parent = subprocess.Popen([sys.executable, str(tmp_path / "fswin.py")],
                              env=hook_env(src, app, row, sockdir), cwd=tmp_path,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "ready")
        wait_sock(sockdir, parent.pid)
        before = set(os.listdir(sockdir + "/ctl"))
        assert set(command_all(sockdir, "START s/t1").values()) == {TAKEN}
        (row / "go").write_text("")
        wait_for(row / "done")
        came = sorted(set(os.listdir(sockdir + "/ctl")) - before - {n for n in before})
        socks = [n for n in came if n.endswith(".sock")]
        assert socks, came
        assert not [n for n in socks if sock_pid(n) in live_socks(sockdir)], "a worker still listens"
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_a_name_that_cannot_be_bound_is_retried_under_another(tmp_path):
    """Another user's stale file of the name (here: a dir, which no rename
    replaces): the process binds <pid>-<random>.sock instead."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row, sockdir)
    env["PYTHONPATH"] = str(src) + os.pathsep + str(app)
    code = ("import os, signal; os.kill(os.getpid(), signal.SIGSTOP); import keploy_agent as k;"
            "print(k._srv is not None, os.path.basename(k.CONTROL_SOCK))")
    p = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=tmp_path,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ns = os.readlink("/proc/self/ns/pid")
    ns = ns[ns.find("[") + 1:ns.rfind("]")]
    try:
        deadline = time.monotonic() + 30
        while True:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "T":
                    break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.makedirs(sockdir + "/ctl", exist_ok=True)
        os.chmod(sockdir + "/ctl", 0o1777)
        os.makedirs(sockdir + f"/ctl/{ns}-{p.pid}.sock")
        os.kill(p.pid, signal.SIGCONT)
        out, err = p.communicate(timeout=60)
        srv, name = out.split()
        assert srv == "True" and name.startswith(f"{p.pid}-"), (out, err)
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


def test_the_run_dir_keploy_names_is_used_and_never_made(tmp_path):
    """keploy makes its run's dir (1777) and names it: the agent binds only
    there; a named dir that is missing is refused, and said to keploy with
    the dir it tried."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    code = "import os, time, keploy_agent as k; print(k._srv is not None, k.CONTROL_SOCK); time.sleep(1.5)"
    try:
        env = hook_env(src, app, row, sockdir)
        p = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                           capture_output=True, text=True, timeout=60)
        srv, path = p.stdout.split()
        assert srv == "True" and os.path.dirname(path) == sockdir + "/ctl", p.stderr
        env["KEPLOY_COVERAGE_CONTROL_DIR"] = sockdir + "/missing"
        p = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                           capture_output=True, text=True, timeout=60)
        assert p.stdout.split()[0] == "False", p.stderr
        assert not os.path.exists(sockdir + "/missing"), "a named dir is never made"
        assert "KEPLOY_COVERAGE_CONTROL_DIR) does not exist" in p.stderr, p.stderr
        with lk:
            un = [r["record"] for r in out["records"] if r["record"].get("kind") == "unmeasured"]
        assert un and un[0]["dir"] == sockdir + "/missing" and un[0]["pid"] > 0, out["records"]
    finally:
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_a_second_request_in_the_test_or_no_request_makes_it_partial(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        # A health probe during the test: two requests in one window.
        {"cmd": "START s/two"}, {"call": "shop.orders:a"}, {"call": "shop.orders:b"}, {"cmd": "END s/two"},
        # App code with no request entry keploy knows (a server it does not
        # hook).
        {"cmd": "START s/none"}, {"plain_call": "shop.orders:a"}, {"cmd": "END s/none"},
        {"cmd": "START s/one"}, {"call": "shop.orders:a"}, {"cmd": "END s/one"},
        {"wait": "records", "n": 5},
    ])
    assert "another request was served during the test (2 requests)" in partial_of(got, "s/two")
    p = partial_of(got, "s/none")
    assert "keploy cannot see this server's requests" in p and "(thread MainThread)" in p, p
    assert partial_of(got, "s/one") == ""


def test_a_job_of_another_context_on_a_pool_thread_is_foreign(tmp_path):
    """A pool thread idle at START that runs a job not of the test's
    request (an APScheduler job, a shared executor): its lines are not the
    test's, by the job's context."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"pool_start": True},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"pool_job": "shop.orders:b"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"call_pool": "shop.orders:b"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 4},
    ])
    assert "app code that is not the test's ran during it (thread shared_0)" in partial_of(got, "s/t1"), \
        partial_of(got, "s/t1")
    # A job the test's own request submits is the test's.
    assert partial_of(got, "s/t2") == "", partial_of(got, "s/t2")


def test_a_thread_the_tracer_cannot_reach_running_app_code_makes_it_partial(tmp_path):
    """Before 3.12 a thread started before the agent is never traced: one
    seen running app code at START or END makes the test partial (its lines
    are in no record). From 3.12 sys.monitoring sees it: its lines are
    another's."""
    app = jobs_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"sleep": 0.3}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 3},
    ], pre_thread="shop.jobs:poll_loop")
    p = partial_of(got, "s/t1")
    if sys.version_info < (3, 12):
        assert "a thread keploy cannot trace ran Python during the test (pre)" in p, p
    else:
        assert "app code that is not the test's ran during it (thread pre)" in p, p


def test_the_tests_job_still_running_on_a_shared_pool_thread_is_its_tail(tmp_path):
    """A pool thread that existed before the test runs the test's job (a
    Starlette background task on anyio's threads): the job's context makes
    it the test's work, still running at its end."""
    app = jobs_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"call": "shop.jobs:tick"},
        {"pool_start": True},
        {"cmd": "START s/t1"}, {"call_pool_nowait": "shop.jobs:slow"}, {"sleep": 0.02}, {"cmd": "END s/t1"},
        {"sleep": 0.4},
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"wait": "records", "n": 4},
    ])
    p = partial_of(got, "s/t1")
    assert "work the test started was still running at its end (threads: shared_0)" in p or \
        "executor jobs the test submitted had not finished at its end (1)" in p, p
    assert partial_of(got, "s/t2") == "", partial_of(got, "s/t2")


def test_a_server_keploy_hooks_gives_each_request_its_id(tmp_path):
    """wsgiref (hooked at BaseHandler.run): each HTTP request is one request
    of its window, its app lines the test's."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"wsgiref_request": "shop.orders:a"},           # warm the server up, outside windows
        {"cmd": "START s/t1"}, {"wsgiref_request": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"cmd": "START s/t2"}, {"wsgiref_request": "shop.orders:a"}, {"wsgiref_request": "shop.orders:a"},
        {"cmd": "END s/t2"},
        {"wait": "records", "n": 4},
    ])
    assert got["wsgi_status"] == [200, 200, 200, 200], log
    assert partial_of(got, "s/t1") == "", partial_of(got, "s/t1")
    assert {r["id"]: r for r in records(got)}["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert "another request was served during the test (2 requests)" in partial_of(got, "s/t2")


# ---------------------------------------------------------------- round 5

def extra_app(app, name, text):
    (app / "shop" / name).write_text(textwrap.dedent(text))
    return app


def test_a_thread_patch_made_before_the_agent_sentry_style_still_runs_threads(tmp_path):
    """sentry-sdk patches Thread.start and calls getattr(run, "__func__",
    run)(thread): with keploy's patch outside it, threads still run, and a
    thread the request starts is the test's."""
    app = make_app(tmp_path)
    pre = textwrap.dedent('''\
        import threading
        _orig = threading.Thread.start
        def start(self, *a, **kw):
            old = getattr(self.run, "__func__", self.run)
            def run(*a2, **kw2):
                return old(self, *a2, **kw2)
            self.run = run
            return _orig(self, *a, **kw)
        threading.Thread.start = start
        ''')
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"},
        {"py": "import threading\n"
               "def work():\n"
               "    t = threading.Thread(target=resolve('shop.orders:a'))\n"
               "    t.start(); t.join()\n"
               "as_request(work)()"},
        {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
    ], pre_py=pre)
    r = {x["id"]: x for x in records(got)}["s/t1"]
    assert "partial" not in r, r
    assert r["executedLinesByFile"] == {"shop/orders.py": [2, 3]}, (r, log)


def test_an_executor_initializers_contextvar_is_seen_by_the_jobs(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"py": "import contextvars\n"
               "from concurrent.futures import ThreadPoolExecutor\n"
               "V = contextvars.ContextVar('v', default=0)\n"
               "ex = ThreadPoolExecutor(max_workers=1, initializer=lambda: V.set(5))\n"
               "out['seen'] = as_request(lambda: ex.submit(V.get).result())()\n"
               "ex.shutdown()"},
    ])
    assert got["seen"] == 5, log


def test_an_interpreter_pool_job_runs_on_314(tmp_path):
    if sys.version_info < (3, 14):
        import pytest
        pytest.skip("InterpreterPoolExecutor is 3.14's")
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"py": "from concurrent.futures import InterpreterPoolExecutor\n"
               "ex = InterpreterPoolExecutor(max_workers=1)\n"
               "out['seen'] = as_request(lambda: ex.submit(pow, 2, 3).result())()\n"
               "ex.shutdown()"},
    ])
    assert got["seen"] == 8, log


def test_a_server_entry_inside_a_request_is_a_request_of_its_own(tmp_path):
    """A pipelined request is served from inside the one before it: a
    server's entry there is a second request (the test partial)."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"},
        {"py": "server = ka._entries._sync_entry(resolve('shop.orders:b'), server=True)\n"
               "as_request(lambda: (resolve('shop.orders:a')(), server()))()"},
        {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
    ])
    r = {x["id"]: x for x in records(got)}["s/t1"]
    assert r["requests"] == 2 and "another request was served during the test (2 requests)" in r["partial"], r


def test_another_finder_ahead_of_keploys_keeps_both_loaders(tmp_path):
    """A wrapt- or OpenTelemetry-style finder put ahead of keploy's asks
    the other finders and wraps their loader: keploy's finder is asked too
    (no recursion), the entry is hooked and the other's loader runs."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"py": textwrap.dedent('''\
            import sys, importlib.abc
            ran = []
            class Wrap(importlib.abc.Loader):
                def __init__(self, l): self.l = l
                def create_module(self, spec): return self.l.create_module(spec)
                def exec_module(self, m): ran.append(m.__name__); self.l.exec_module(m)
            class Other(importlib.abc.MetaPathFinder):
                busy = set()
                def find_spec(self, name, path, target=None):
                    if name != "wsgiref.handlers" or name in self.busy: return None
                    self.busy.add(name)
                    try:
                        for f in sys.meta_path:
                            if f is self: continue
                            spec = f.find_spec(name, path, target) if hasattr(f, "find_spec") else None
                            if spec is not None:
                                spec.loader = Wrap(spec.loader)
                                return spec
                    finally:
                        self.busy.discard(name)
            sys.modules.pop("wsgiref.handlers", None)
            sys.meta_path.insert(0, Other())
            import wsgiref.handlers
            out["hooked"] = getattr(wsgiref.handlers.BaseHandler.run, "__keploy_entry__", False)
            out["ran"] = ran
            ''')},
    ])
    assert got["hooked"] is True and got["ran"] == ["wsgiref.handlers"], log


def test_an_app_signal_handler_is_no_requests_work(tmp_path):
    app = extra_app(make_app(tmp_path), "sig.py", '''\
        import os
        import signal


        def handler(signum, frame):
            z = 3
            return z


        def install():
            signal.signal(signal.SIGUSR1, handler)
            return signal.getsignal(signal.SIGUSR1) is handler


        def poke():
            os.kill(os.getpid(), signal.SIGUSR1)
            x = 1
            return x
        ''')
    got, log = run_case(tmp_path, app, [
        {"py": "out['same'] = resolve('shop.sig:install')()"},
        {"cmd": "START s/t1"}, {"call": "shop.sig:poke"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    assert got["same"] is True, "getsignal gives the app's own handler back"
    assert "app code that is not the test's ran during it (a signal handler)" in partial_of(got, "s/t1"), log


def test_a_finalizer_the_collector_runs_in_a_test_is_not_its_work(tmp_path):
    app = extra_app(make_app(tmp_path), "fin.py", '''\
        import gc


        class Node:
            def __del__(self):
                w = 4
                return w


        def cycle():
            n = Node()
            n.me = n
            del n


        def collect():
            gc.collect()
            return 1
        ''')
    got, log = run_case(tmp_path, app, [
        {"py": "import gc; gc.disable(); resolve('shop.fin:cycle')()"},
        {"cmd": "START s/t1"}, {"call": "shop.fin:collect"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    assert "app code that is not the test's ran during it (the garbage collector's finalizers)" in \
        partial_of(got, "s/t1"), log


def test_a_tracer_set_on_a_running_thread_is_said(tmp_path):
    """Before 3.12 a Python sys.settrace takes its thread from keploy's
    tracer: the END and the STARTs after are not taken, with why. From 3.12
    keploy's measuring is sys.monitoring's, which a tracer does not touch."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"py": "import sys; sys.settrace(None)"}, {"call": "shop.orders:a"},
        {"cmd": "END s/t1"}, {"cmd": "START s/t2"},
    ])
    if sys.version_info >= (3, 12):
        assert got["answers"] == [TAKEN] * 3, got["answers"]
    else:
        assert got["answers"][0] == TAKEN
        assert all(a.startswith("IGNORED") and "another tracer was set on thread MainThread (sys.settrace)" in a
                   for a in got["answers"][1:]), got["answers"]


def test_a_file_edited_after_the_process_started_and_before_its_build_is_partial(tmp_path):
    """The process imported the file, then it was edited, then the build
    was read (it names the new bytes, the process runs the old ones)."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"py": "import shop.orders, time, os\n"
               "time.sleep(1.2)\n"
               "p = os.path.join(os.environ['KEPLOY_APP_SOURCE_DIR'], 'shop', 'orders.py')\n"
               "open(p, 'a').write('\\n# edited\\n')\n"
               "ka._index, ka._index_ready = None, ka._Flag()\n"
               "ka._stats.clear(); ka._rehashed.clear()\n"
               "ka._start_build(); ka._index_ready.wait(10)"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    assert "shop/orders.py was edited after this process started" in partial_of(got, "s/t1"), log


def test_a_wsgi_body_under_a_server_keploy_does_not_hook_is_the_requests(tmp_path):
    """A WSGI framework's entry with no server's around it (uWSGI, mod_wsgi):
    the body's iteration and close() run as the request, which is served
    until close(); END waits for it."""
    app = extra_app(make_app(tmp_path), "fw.py", '''\
        import time


        def closing():
            c = 5
            return c


        class Body:
            def __iter__(self):
                yield b"ok"

            def close(self):
                time.sleep(0.02)
                closing()


        class App:
            def __call__(self, environ, start_response):
                start_response("200 OK", [])
                return Body()
        ''')
    got, log = run_case(tmp_path, app, [
        {"py": "fw = ka._entries._sync_entry(resolve('shop.fw:App').__call__, body=True)\n"
               "import threading\n"
               "def serve():\n"
               "    body = fw(resolve('shop.fw:App')(), {}, lambda *a: None)\n"
               "    list(body)\n"
               "    out['served'] = True\n"
               "    threading.Timer(0.005, body.close).start()\n"},
        {"cmd": "START s/t1"}, {"py": "serve()"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    r = {x["id"]: x for x in records(got)}["s/t1"]
    assert "partial" not in r, r
    assert 5 in r["executedLinesByFile"]["shop/fw.py"], r


def test_queued_work_not_taken_at_the_end_is_partial_and_taken_work_is_the_tests(tmp_path):
    app = extra_app(make_app(tmp_path), "q.py", '''\
        import queue
        import threading

        Q = queue.Queue()


        def handle(item):
            h = item
            return h


        def consume():
            while True:
                item = Q.get()
                handle(item)
                Q.task_done()


        def start():
            threading.Thread(target=consume, daemon=True, name="consumer").start()


        def put():
            Q.put(1)
        ''')
    got, log = run_case(tmp_path, app, [
        {"py": "resolve('shop.q:start')()"},
        # Taken and done inside the test: its lines are the test's.
        {"cmd": "START s/taken"}, {"py": "as_request(resolve('shop.q:put'))(); resolve('shop.q:Q').join()"},
        {"cmd": "END s/taken"},
        # Put while the consumer is held: not taken at the end.
        {"py": "import queue; resolve('shop.q:Q').mutex.acquire()"},
        {"cmd": "START s/held"}, {"py": "import threading\n"
                                        "q = resolve('shop.q:Q')\n"
                                        "as_request(lambda: q._put(2))()"},
        {"cmd": "END s/held"},
        {"py": "resolve('shop.q:Q').mutex.release()"},
        {"wait": "records", "n": 3},
    ])
    taken = {x["id"]: x for x in records(got)}["s/taken"]
    assert "partial" not in taken and {8, 9, 15, 24} <= set(taken["executedLinesByFile"]["shop/q.py"]), taken
    assert "work the test queued had not been taken at its end (1 item(s))" in partial_of(got, "s/held"), log


def test_a_thread_the_test_started_alive_in_library_code_is_its_tail(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"},
        {"py": "import threading, time\n"
               "as_request(lambda: threading.Thread(target=time.sleep, args=(0.3,), name='libwait').start())()"},
        {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
    ])
    assert "work the test started was still running at its end (threads: libwait)" in partial_of(got, "s/t1"), log


def test_a_thread_keploy_cannot_trace_that_ran_between_the_snapshots_is_said(tmp_path):
    """Before 3.12: a thread from before the agent that ran Python only
    between START and END (no app frame at either) is seen by its stack
    having moved."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"py": "import time; E1.set(); time.sleep(0.1)"},
        {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
    ], pre_py=textwrap.dedent('''\
        import threading, time
        E1, E2 = threading.Event(), threading.Event()
        def loop():
            E1.wait()
            sum(range(10))
            E2.wait()
        threading.Thread(target=loop, daemon=True, name="early").start()
        time.sleep(0.05)
        '''))
    p = partial_of(got, "s/t1")
    if sys.version_info >= (3, 12):
        assert p == "", p
    else:
        assert "a thread keploy cannot trace ran Python during the test (early)" in p, (p, log)
