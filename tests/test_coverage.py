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


def run_case(tmp_path, app, steps, env=None):
    d = tempfile.mkdtemp(prefix="kv")
    try:
        case = {"sock": os.path.join(d, "c.sock"), "data": os.path.join(d, "d.sock"),
                "app": str(app), "steps": steps}
        e = dict(os.environ, PYTHONPATH=str(ROOT / "src"), KEPLOY_APP_SOURCE_DIR=str(app),
                 KEPLOY_COVERAGE_EXCLUDE="scripts/*")
        e.update(env or {})
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


def test_a_forked_child_stops_measuring_and_the_parent_goes_on(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"}, {"fork": True},
        {"cmd": "START s/t1"}, {"call": "shop.orders:b"}, {"cmd": "END s/t1"},
        {"wait": "records", "n": 2},
    ])
    assert got["forked"] == {"collector": False,
                             "why": "a forked child of the process that serves the socket"}, log
    assert {r["id"]: r for r in records(got)}["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [7, 8]}


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


def test_an_empty_window_while_prefork_workers_live_is_not_taken(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"fork_worker": "start"},
        # The worker served the request: the parent ran no app line.
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        # A line the parent ran itself is its own: taken.
        {"cmd": "START s/t2"}, {"call": "shop.orders:a"}, {"cmd": "END s/t2"},
        {"fork_worker": "stop"},
        # No worker left: an empty window is a test that ran no app line.
        {"cmd": "START s/t3"}, {"cmd": "END s/t3"},
        {"wait": "records", "n": 3},
    ])
    assert got["answers"][0] == TAKEN, log
    assert got["answers"][1] == ("IGNORED proto=2 the app serves requests in child processes "
                                 "(prefork workers) that keploy cannot measure; run one process\n"), log
    assert got["answers"][2:] == [TAKEN] * 4, log
    recs = {r["id"]: r for r in records(got)}
    assert "s/t1" not in recs
    assert recs["s/t2"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    assert recs["s/t3"]["executedLinesByFile"] == {}


def test_an_owner_yields_its_socket_once_and_serves_on_if_not_taken(tmp_path):
    """Two children ask at once (uvicorn --reload's server and a helper):
    one is given the socket, the other is refused. The owner serves on until
    the socket file is the asker's; one that never binds it leaves the owner
    serving."""
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"handover_wait": 0.5},
        {"fork_yielders": 2},
        {"cmd": "START s/t0"},
        {"wait_s": 1.0},
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"call": "shop.orders:a"}, {"cmd": "END s/t1"},
        {"fork_worker": "stop"},
    ])
    assert got["yields"] == [TAKEN, "IGNORED proto=2 this process gave its socket to another "
                                    "process already\n"], log
    assert got["answers"][0] == ("IGNORED proto=2 this process gave keploy's socket to the "
                                 "process that serves the app\n"), log
    assert got["answers"][1:] == [TAKEN, TAKEN], log
    assert "did not take it within" in log


def test_peer_and_yield_name_their_sender_only(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "PEER 1"}, {"cmd": "PEER 99999999999999999999"}, {"cmd": "YIELD 12345"},
        {"cmd": "PEER \u00b2"},
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"},
        {"ancestors": True},
    ])
    me = got["driver_pid"]
    assert got["answers"][0] == f"IGNORED proto=2 it names process 1, but process {me} sent it\n", log
    assert got["answers"][1] == "IGNORED proto=2 not a process id: '99999999999999999999'\n", log
    assert got["answers"][2] == f"IGNORED proto=2 it names process 12345, but process {me} sent it\n", log
    assert got["answers"][3] == "IGNORED proto=2 not a process id: '\u00b2'\n", log
    # No bogus peer was kept: the empty window is taken.
    assert got["answers"][4:] == [TAKEN, TAKEN], log
    # A container's command is pid 1: it is an ancestor like any other.
    assert got["ancestors"] == [[1, 5], [1]]


def test_a_files_mtime_never_matters_only_its_bytes(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"cmd": "START s/t1"}, {"cmd": "END s/t1"}, {"wait": "records", "n": 2},
        {"touch_app": True},
        {"cmd": "TABLE"}, {"wait": "frames", "n": 2},
    ])
    units = [json.loads(x) for x in got["parts"][0]["stream"].splitlines()]
    assert [u["state"] for u in units] == ["ok", "ok"], log


def test_a_callback_error_is_swallowed_and_said_later(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"cmd": "START s/t1"}, {"stub": "rel_fails"}, {"cmd": "END s/t1"},
    ])
    assert got["late"] == 1, log
    assert got["answers"][0] == TAKEN, log
    assert "keploy's line callback failed in" in log and "(RuntimeError)" in log, log


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
    (idx,) = [r["record"] for r in got["records"] if r["first_key"] == "kind"]
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
    (idx,) = [r["record"] for r in got["records"] if r["first_key"] == "kind"]
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
    idx = [r for r in got["records"] if r["first_key"] == "kind"]
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
    (idx,) = [r["record"] for r in got["records"] if r["first_key"] == "kind"]
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
    """A copy of the package whose socket lines name a private dir, so a
    row that starts the agent through the hook never binds keploy's own
    paths in /tmp."""
    d = tempfile.mkdtemp(prefix="kh")
    src = tmp_path / "pkgsrc"
    shutil.copytree(SRC, src / "keploy_agent")
    init = src / "keploy_agent" / "__init__.py"
    text = init.read_text()
    for line, name, path in (('CONTROL_SOCK = "/tmp/coverage_control.sock"', "CONTROL_SOCK", d + "/c.sock"),
                             ('DATA_SOCK = "/tmp/coverage_data.sock"', "DATA_SOCK", d + "/d.sock")):
        assert text.count(line) == 1
        text = text.replace(line, f"{name} = {path!r}")
    init.write_text(text)
    return src, d


def test_the_sitecustomize_hook_starts_the_agent_and_chains(tmp_path):
    src, sockdir = private_copy(tmp_path)
    # The user's own sitecustomize, later on PYTHONPATH, still runs.
    user = tmp_path / "user"
    user.mkdir()
    (user / "sitecustomize.py").write_text("import os\nos.environ['USER_SITE_RAN'] = '1'\n")
    app = make_app(tmp_path)
    env = dict(os.environ, KEPLOY_APP_SOURCE_DIR=str(app))
    env["PYTHONPATH"] = str(src) + os.pathsep + str(user)
    try:
        pp = subprocess.run([sys.executable, "-m", "keploy_agent", "--pythonpath"], env=env,
                            capture_output=True, text=True, timeout=60, cwd=tmp_path)
        assert pp.returncode == 0, pp.stderr
        assert "Keploy" not in pp.stderr, "printing the path starts no agent"
        assert not os.path.exists(sockdir + "/c.sock")
        env["PYTHONPATH"] = pp.stdout.strip()
        assert env["PYTHONPATH"] == str(src / "keploy_agent" / "_boot") + os.pathsep + str(src) + os.pathsep + str(user)
        code = ("import os, keploy_agent as k;"
                "print(os.environ.get('USER_SITE_RAN'), k.BOOT, k._collector is not None,"
                " os.path.exists(k.CONTROL_SOCK))")
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, timeout=60, cwd=tmp_path)
        assert p.returncode == 0, p.stderr
        assert p.stdout.split() == ["1", "True", "True", "True"], p.stderr
        # Its thread is a daemon (BOOT): the process exited, and removed its
        # socket on the way out.
        assert not os.path.exists(sockdir + "/c.sock")
        # A second process while a live one serves the socket leaves it be.
        hold = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env,
                                stderr=subprocess.PIPE, text=True, cwd=tmp_path)
        try:
            for _ in range(200):
                if os.path.exists(sockdir + "/c.sock"):
                    break
                import time
                time.sleep(0.05)
            p2 = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                text=True, timeout=60, cwd=tmp_path)
            # Not its ancestor: the live owner keeps the socket.
            assert p2.stdout.split() == ["1", "True", "False", "True"], p2.stderr
        finally:
            hold.kill()
            hold.wait()
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


RELOADER_CHILD = """
import os, subprocess, sys, time
import keploy_agent as k
import shop.orders as o
d = os.environ["ROW_DIR"]
open(os.path.join(d, "child-ready.tmp"), "w").write(str(os.getpid()))
os.rename(os.path.join(d, "child-ready.tmp"), os.path.join(d, "child-ready"))
spawned = False
while True:
    o.a()
    if not spawned and os.path.exists(os.path.join(d, "spawn")):
        spawned = True
        # A helper process the app starts once tests run: it never takes
        # the socket from the app.
        code = ("import os, keploy_agent as k;"
                "open(os.path.join(os.environ['ROW_DIR'], 'helper.tmp'), 'w')"
                ".write(str(k._collector is not None));"
                "os.rename(os.path.join(os.environ['ROW_DIR'], 'helper.tmp'),"
                " os.path.join(os.environ['ROW_DIR'], 'helper'))")
        subprocess.run([sys.executable, "-c", code])
    time.sleep(0.005)
"""


def wait_for(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path):
        assert time.monotonic() < deadline, f"{path} never came"
        time.sleep(0.02)


def test_a_reloaders_child_takes_the_socket_and_is_measured(tmp_path):
    """Django runserver, uvicorn --reload, Flask debug: a parent that serves
    no request starts the child that does. The child takes the socket over
    from its parent (which has taken no window), and is measured; a helper
    it starts once tests run does not take it."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    env = dict(os.environ, KEPLOY_APP_SOURCE_DIR=str(app), ROW_DIR=str(row))
    env["PYTHONPATH"] = str(src)
    env["PYTHONPATH"] = subprocess.check_output(
        [sys.executable, "-m", "keploy_agent", "--pythonpath"], env=env, text=True,
        cwd=tmp_path).strip() + os.pathsep + str(app)
    # Django's autoreloader starts its server child with RUN_MAIN=true.
    parent_code = ("import os, subprocess, sys, keploy_agent as k;"
                   f"subprocess.run([sys.executable, '-c', {RELOADER_CHILD!r}],"
                   " env=dict(os.environ, RUN_MAIN='true'))")
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env=env, cwd=tmp_path,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "child-ready")
        child = int((row / "child-ready").read_text())
        sock = sockdir + "/c.sock"
        assert command(sock, "START s/t1") == TAKEN
        time.sleep(0.05)
        assert command(sock, "END s/t1") == TAKEN
        (row / "spawn").write_text("")
        wait_for(row / "helper")
        assert (row / "helper").read_text() == "False", "a helper took the socket"
        assert command(sock, "START s/t2") == TAKEN
        time.sleep(0.05)
        assert command(sock, "END s/t2") == TAKEN
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with lk:
                ids = {r["record"].get("id") for r in out["records"]}
            if {"s/t1", "s/t2"} <= ids:
                break
            time.sleep(0.02)
        with lk:
            recs = {r["record"]["id"]: r["record"] for r in out["records"] if "id" in r["record"]}
        assert recs["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
        assert recs["s/t2"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
        import socket
        import struct
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(sock)
        owner = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        c.close()
        assert owner == child
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        _, err = parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    assert f"gave the Keploy control socket to process {child}" in err, err


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
        o.a()
        time.sleep(0.005)


if __name__ == "__main__":
    # uvicorn --reload's shape: a spawn context starts multiprocessing's
    # resource tracker, then the server (spawn_main).
    p = mp.get_context("spawn").Process(target=serve, args=(os.environ["ROW_DIR"],))
    p.start()
    p.join()
"""


def hook_env(src, app, row):
    env = dict(os.environ, KEPLOY_APP_SOURCE_DIR=str(app), ROW_DIR=str(row))
    env["PYTHONPATH"] = str(src)
    env["PYTHONPATH"] = subprocess.check_output(
        [sys.executable, "-m", "keploy_agent", "--pythonpath"], env=env, text=True).strip() \
        + os.pathsep + str(app)
    return env


def socket_owner(sock):
    import socket
    import struct
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(sock)
    owner = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    c.close()
    return owner


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


def test_a_reload_tree_with_a_resource_tracker_measures_the_server(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    (tmp_path / "parent.py").write_text(SPAWN_PARENT)
    parent = subprocess.Popen([sys.executable, str(tmp_path / "parent.py")], env=hook_env(src, app, row),
                              cwd=tmp_path, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "child-ready")
        child = int((row / "child-ready").read_text())
        kids = children_of(parent.pid)
        trackers = [pid for pid, cmd in kids if "multiprocessing.resource_tracker" in cmd]
        assert trackers, kids
        sock = sockdir + "/c.sock"
        assert socket_owner(sock) == child
        assert command(sock, "START s/t1") == TAKEN
        time.sleep(0.05)
        assert command(sock, "END s/t1") == TAKEN
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with lk:
                recs = {r["record"]["id"]: r["record"] for r in out["records"] if "id" in r["record"]}
            if "s/t1" in recs:
                break
            time.sleep(0.02)
        assert recs["s/t1"]["executedLinesByFile"] == {"shop/orders.py": [2, 3]}
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        _, err = parent.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    for t in trackers:
        assert f" pid {t} " not in err, err
    assert err.count("took the Keploy control socket over") == 1, err


LONER = """
import os, socket, time
import keploy_agent
d = os.environ["ROW_DIR"]
open(os.path.join(d, "ready.tmp"), "w").write(str(os.getpid()))
os.rename(os.path.join(d, "ready.tmp"), os.path.join(d, "ready"))
s = None
while True:
    if s is None and os.path.exists(os.path.join(d, "listen")):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        open(os.path.join(d, "listening"), "w").write("")
    time.sleep(0.01)
"""


def test_a_socket_holder_that_serves_nothing_fails_closed(tmp_path):
    """Whatever process ends up holding keploy's socket: one that listens on
    no socket of its own serves no request, so a window that ran no app line
    there is not taken as a test that ran none."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    p = subprocess.Popen([sys.executable, "-c", LONER], env=hook_env(src, app, row), cwd=tmp_path,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "ready")
        pid = int((row / "ready").read_text())
        sock = sockdir + "/c.sock"
        assert command(sock, "START s/t1") == TAKEN
        a = command(sock, "END s/t1")
        assert a.startswith("IGNORED proto=2 the process holding keploy's control socket serves no "
                            "requests ("), a
        assert a.endswith(f" pid {pid}): keploy measures the wrong process\n"), a
        (row / "listen").write_text("")
        wait_for(row / "listening")
        assert command(sock, "START s/t2") == TAKEN
        assert command(sock, "END s/t2") == TAKEN
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)


TWINS_PARENT = """
import os, subprocess, sys, time
import keploy_agent
d = os.environ["ROW_DIR"]
code = ("import os, time, keploy_agent as k;"
        "d = os.environ['ROW_DIR'];"
        "open(os.path.join(d, '%d.tmp' % os.getpid()), 'w').write(str(k._collector is not None));"
        "os.rename(os.path.join(d, '%d.tmp' % os.getpid()), os.path.join(d, '%d.kid' % os.getpid()));"
        "time.sleep(60)")
env = dict(os.environ, KEPLOY_COVERAGE_TAKEOVER="1")
kids = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(2)]
open(os.path.join(d, "kids.tmp"), "w").write(" ".join(str(k.pid) for k in kids))
os.rename(os.path.join(d, "kids.tmp"), os.path.join(d, "kids"))
time.sleep(60)
"""


def test_two_children_racing_for_the_socket_never_both_win(tmp_path):
    """A parent that serves no request starts two children at once that both
    find it an ancestor that took no window: one takes the socket, the other
    is left unmeasured, and the owner is the one that says it measures."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    for round_ in range(5):
        row = tmp_path / f"row{round_}"
        row.mkdir()
        p = subprocess.Popen([sys.executable, "-c", TWINS_PARENT], env=hook_env(src, app, row),
                             cwd=tmp_path, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            wait_for(row / "kids")
            kids = [int(x) for x in (row / "kids").read_text().split()]
            for k in kids:
                wait_for(row / f"{k}.kid")
            measured = [k for k in kids if (row / f"{k}.kid").read_text() == "True"]
            assert len(measured) == 1, (round_, measured)
            assert socket_owner(sockdir + "/c.sock") == measured[0], round_
        finally:
            os.killpg(p.pid, signal.SIGKILL)
            _, err = p.communicate(timeout=30)
        assert err.count("took the Keploy control socket over") == 1, err
    shutil.rmtree(sockdir, ignore_errors=True)



def test_a_replace_never_removes_a_file_it_did_not_probe(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [{"bind_race": True}, {"bind_lock": True}])
    assert got["bind_race"] == "served", log
    assert got["winner_kept"] is True, log
    assert got["bind_waited"] >= 0.4, got


def test_stdlib_helpers_never_start_the_agent_and_spawn_main_does(tmp_path):
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    row = tmp_path / "row"
    row.mkdir()
    env = hook_env(src, app, row)
    probe = "import os, keploy_agent as k; print('_srv' in vars(k), os.path.exists(k.CONTROL_SOCK))"
    try:
        for head, want in (("from multiprocessing.resource_tracker import main", "False False"),
                           ("from multiprocessing.forkserver import main", "False False"),
                           ("from multiprocessing.spawn import spawn_main", "True True")):
            p = subprocess.run([sys.executable, "-c", head + "\n" + probe], env=env, cwd=tmp_path,
                               capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, p.stderr
            assert p.stdout.split() == want.split(), (head, p.stdout, p.stderr)
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


FROM_PARENT = """
import os, subprocess, sys, time
import keploy_agent
code = (
    "import os, socket, time, keploy_agent as k;"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen();"
    "d = os.environ['ROW_DIR'];"
    "open(os.path.join(d, 'kid.tmp'), 'w').write(str(os.getpid()));"
    "os.rename(os.path.join(d, 'kid.tmp'), os.path.join(d, 'kid'));"
    "time.sleep(60)")
# Flask's reloader starts its server child with WERKZEUG_RUN_MAIN=true.
subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ, WERKZEUG_RUN_MAIN="true"))
time.sleep(60)
"""


def test_an_empty_window_while_the_reloader_lives_is_not_taken(tmp_path):
    """A child took the socket from its parent; the parent lives on and may
    be the one serving (a relay of its windows is phase 2): an empty window
    is not taken as a test that ran no code while it lives."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    p = subprocess.Popen([sys.executable, "-c", FROM_PARENT], env=hook_env(src, app, row), cwd=tmp_path,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "kid")
        kid = int((row / "kid").read_text())
        sock = sockdir + "/c.sock"
        deadline = time.monotonic() + 30
        while socket_owner(sock) != kid:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert command(sock, "START s/t1") == TAKEN
        assert command(sock, "END s/t1") == (
            f"IGNORED proto=2 the process this one took keploy's socket from (pid {p.pid}) is alive "
            "and may serve requests keploy cannot measure; run one process\n")
        p.kill()
        p.wait()
        assert command(sock, "START s/t2") == TAKEN
        assert command(sock, "END s/t2") == TAKEN
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)


PLAIN_PARENT = """
import os, subprocess, sys, time
import keploy_agent
code = (
    "import os, time, keploy_agent as k;"
    "d = os.environ['ROW_DIR'];"
    "open(os.path.join(d, 'kid.tmp'), 'w').write('%d %s' % (os.getpid(), k._collector is not None));"
    "os.rename(os.path.join(d, 'kid.tmp'), os.path.join(d, 'kid'));"
    "time.sleep(60)")
# A sidecar or a pool the app starts before it is ready: no reloader's mark.
subprocess.Popen([sys.executable, "-c", code])
open(os.path.join(os.environ["ROW_DIR"], "parent"), "w").write(str(os.getpid()))
time.sleep(60)
"""


def test_a_child_that_is_no_reloaders_server_never_takes_the_socket(tmp_path):
    """Only a reloader's server child (RUN_MAIN, WERKZEUG_RUN_MAIN,
    spawn_main, KEPLOY_COVERAGE_TAKEOVER=1) takes the socket from its
    ancestor: any other registers as a peer, so the owner's empty windows
    fail closed instead of another process's lines being taken as the
    test's."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    p = subprocess.Popen([sys.executable, "-c", PLAIN_PARENT], env=hook_env(src, app, row), cwd=tmp_path,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "kid")
        kid, measured = (row / "kid").read_text().split()
        assert measured == "False"
        sock = sockdir + "/c.sock"
        assert socket_owner(sock) == p.pid
        assert command(sock, "START s/t1") == TAKEN
        assert command(sock, "END s/t1") == (
            "IGNORED proto=2 the app serves requests in child processes (prefork workers) that "
            "keploy cannot measure; run one process\n")
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        _, err = p.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    assert "took the Keploy control socket over" not in err, err


def test_a_fork_with_a_command_in_flight_drops_the_tracer_and_the_connection(tmp_path):
    app = make_app(tmp_path)
    got, log = run_case(tmp_path, app, [
        {"call": "shop.orders:both"},
        {"fork_in_flight": True},
    ])
    child = got["forked_in_flight"]
    assert child == {"collector": False, "tracer": False, "conns": 0}, log
    # The child closed its copy: the parent's answer, then its close, came.
    assert got["in_flight_answer"] == [TAKEN, True], log


GRAND_PARENT = """
import os, subprocess, sys, time
import keploy_agent
child = (
    "import os, socket, subprocess, sys, time, keploy_agent as k;"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen();"
    "d = os.environ['ROW_DIR'];"
    # The grandchild inherits RUN_MAIN=true, as a server child's pool or
    # helper does.
    "g = subprocess.Popen([sys.executable, '-c', 'import os, time, keploy_agent as k;"
    "d = os.environ[\\\\'ROW_DIR\\\\'];"
    "open(os.path.join(d, \\\\'g.tmp\\\\'), \\\\'w\\\\').write(\\\\'%d %s\\\\' % (os.getpid(), k._collector is not None));"
    "os.rename(os.path.join(d, \\\\'g.tmp\\\\'), os.path.join(d, \\\\'grandchild\\\\'));"
    "time.sleep(60)']);"
    "open(os.path.join(d, 'c.tmp'), 'w').write(str(os.getpid()));"
    "os.rename(os.path.join(d, 'c.tmp'), os.path.join(d, 'child'));"
    "time.sleep(60)")
subprocess.Popen([sys.executable, "-c", child], env=dict(os.environ, RUN_MAIN="true"))
time.sleep(60)
"""


def test_a_server_childs_own_child_never_takes_the_socket(tmp_path):
    """The marks a server child carries (RUN_MAIN, spawn_main) are its
    children's too: one handover per tree. The server child keeps the
    socket, the grandchild is its peer, and an empty window fails closed
    while the grandchild lives."""
    src, sockdir = private_copy(tmp_path)
    app = make_app(tmp_path)
    out, lk = {"records": [], "parts": [], "errors": []}, threading.Lock()
    fake = FakeAgent(sockdir + "/d.sock", out, lk)
    row = tmp_path / "row"
    row.mkdir()
    p = subprocess.Popen([sys.executable, "-c", GRAND_PARENT], env=hook_env(src, app, row), cwd=tmp_path,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        wait_for(row / "child")
        wait_for(row / "grandchild")
        child = int((row / "child").read_text())
        grandchild, measured = (row / "grandchild").read_text().split()
        assert measured == "False"
        sock = sockdir + "/c.sock"
        assert socket_owner(sock) == child
        assert command(sock, "START s/t1") == TAKEN
        assert command(sock, "END s/t1") == (
            "IGNORED proto=2 the app serves requests in child processes (prefork workers) that "
            "keploy cannot measure; run one process\n")
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        _, err = p.communicate(timeout=30)
        fake.srv.close()
        shutil.rmtree(sockdir, ignore_errors=True)
    assert err.count("took the Keploy control socket over") == 1, err
