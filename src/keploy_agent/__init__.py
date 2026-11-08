"""
Keploy Python agent – always-on trace, per-test diff, std-lib filtered.
"""

from __future__ import annotations
import os, sys, json, time, socket, threading, logging, trace
import sysconfig, site, pathlib
from typing import Dict, Tuple

# ------------------------------------------------ configuration
CONTROL_SOCK = "/tmp/coverage_control.sock"
DATA_SOCK    = "/tmp/coverage_data.sock"

APP_ROOT  = pathlib.Path(os.getenv("KEPLOY_APP_SOURCE_DIR", os.getcwd())).resolve()
AGENT_DIR = pathlib.Path(__file__).parent.resolve()
STDLIB    = pathlib.Path(sysconfig.get_paths()["stdlib"]).resolve()
SITE_PKGS = [pathlib.Path(p).resolve() for p in site.getsitepackages()]


logging.basicConfig(level=logging.INFO,
                    format='[Keploy Agent] %(asctime)s - %(message)s')

# ------------------------------------------------ global tracer
_tracer = trace.Trace(count=1, trace=0)
sys.settrace(_tracer.globaltrace)           # main thread
threading.settrace(_tracer.globaltrace)     # all future threads
logging.info("Global tracer started for every thread")

# ------------------------------------------------ agent state
lock            = threading.Lock()
current_id      = None                      # active test-case id
baseline_counts: Dict[Tuple[str, int], int] = {}  # empty after clear
baseline_tids: set[int] = set()


# ------------------------------------------------ helpers
def _copy_counts_once() -> dict[tuple[str, int], int]:
    while True:
        try:  return dict(_tracer.results().counts)
        except RuntimeError:  continue            # mutated – retry

def _stable_snapshot(max_wait: float = .5) -> dict[tuple[str, int], int]:
    start = time.time(); snap = _copy_counts_once()
    while True:
        time.sleep(0.07)
        snap2 = _copy_counts_once()
        if snap2 == snap or (time.time() - start) >= max_wait:
            return snap2
        snap = snap2

# The agent's answers on the control socket: one line for every command it
# reads, written before it closes the connection. ACK says it took the
# command: it reset the counts for a START, or sent the test's coverage for an
# END. IGNORED says it read the command and did not take it. proto=2 says this
# agent answers every command it reads, so Keploy can read a close with no
# answer as a command not taken: on macOS a connection closed with the command
# unread gives Keploy no error, only the close.
REPLY_TAKEN   = b"ACK proto=2\n"
REPLY_IGNORED = b"IGNORED proto=2\n"

def _is_app_file(raw: str, p: pathlib.Path) -> bool:
    if "<frozen " in raw:               return False
    if p.is_relative_to(AGENT_DIR):     return False
    if p.is_relative_to(STDLIB):        return False
    if any(p.is_relative_to(sp) for sp in SITE_PKGS): return False
    return p.is_relative_to(APP_ROOT)

def _diff(after: Dict, before: Dict):
    for key, hits in after.items():
        if hits > before.get(key, 0):
            yield key

def _emit(test_id: str):
    after = _stable_snapshot()
    data  = {}
    for (raw, line) in _diff(after, baseline_counts):
        p = pathlib.Path(raw).resolve()
        if _is_app_file(raw, p):
            data.setdefault(str(p), []).append(line)

    payload = json.dumps({"id": test_id,
                          "executedLinesByFile": data},
                         separators=(",", ":")).encode()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(DATA_SOCK); s.sendall(payload)

    logging.info(f"[{test_id}] sent {len(data)} file(s) / "
                 f"{sum(len(v) for v in data.values())} line(s)")

# ------------------------------------------------ per-connection handler
def _run(cmd: str) -> bool:
    """Run one command ("START id", "END id") and say whether it was taken."""
    global current_id, baseline_counts, baseline_tids
    action, sep, test_id = cmd.partition(" ")
    if not sep:
        logging.warning(f"Invalid command: {cmd!r}")
        return False

    with lock:
        if action == "START":
            logging.info(f"START {test_id}")
            try:
                _tracer.results().counts.clear()    # start from 0 hits
            except Exception as e:
                # Counts not reset would hold the lines of the tests before
                # this one: the START is not taken, and its END is ignored.
                current_id = None
                logging.error(f"[{test_id}] could not reset the counts: {e}",
                              exc_info=True)
                return False
            baseline_counts = {}                    # diff against empty dict
            baseline_tids = _current_tids()         # remember threads
            current_id = test_id
            return True

        if action == "END":
            logging.info(f"END   {test_id}")
            if test_id != current_id:
                logging.warning(f"END {test_id} does not match the test "
                                f"started ({current_id}): no coverage sent")
                return False
            try:
                _wait_for_worker_exit(baseline_tids)
                time.sleep(0.02)
                _emit(test_id)
            except Exception as e:
                logging.error(f"[{test_id}] could not send the coverage: {e}",
                              exc_info=True)
                return False
            finally:
                current_id = None
            return True

        logging.warning(f"Unknown command: {action}")
        return False

def _handle(conn: socket.socket):
    """Read one command, run it, and answer it before the close."""
    fp = conn.makefile("r", errors="replace")
    try:
        try:
            raw = fp.readline()
        except OSError as e:
            logging.warning(f"Could not read a command: {e}")
            return
        if not raw: return                          # closed with no command
        cmd = raw.strip()
        try:
            taken = _run(cmd)
        except Exception as e:
            logging.error(f"Handler error: {e}", exc_info=True); taken = False
        try: conn.sendall(REPLY_TAKEN if taken else REPLY_IGNORED)
        except OSError as e:
            logging.warning(f"Could not answer {cmd!r}: {e}")
    finally:
        try: fp.close(); conn.close()
        except Exception: pass

# ------------------------------------------------ control server
def _bind(path: str) -> socket.socket:
    """Bind and listen on the control socket at path, replacing a stale one."""
    if os.path.exists(path): os.remove(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path); srv.listen()
    except BaseException:
        srv.close(); raise
    return srv

def _server(srv: socket.socket):
    try:
        while True:
            c, _ = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
    finally:
        srv.close()


# ---------------------------------------------------------------- thread helper
def _current_tids() -> set[int]:
    return {t.ident for t in threading.enumerate() if t.ident is not None}

def _wait_for_worker_exit(baseline: set[int], timeout: float = 1.0):
    """Block until no extra threads (vs. baseline) remain, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _current_tids() <= baseline:       # no extra threads left
            return
        time.sleep(0.02)                      # wait 20 ms and retry


# ------------------------------------------------ start
# The control socket is bound here, before this import returns, so it is
# there before the app can say it is ready: bound on a thread, the bind could
# come after the app's port, and the first test's START then found no one
# listening and its coverage was lost. Commands are served on a thread. A
# socket that cannot be bound is said, and the app runs on without coverage.
try:
    _srv = _bind(CONTROL_SOCK)
except OSError as e:
    logging.error(f"Could not start the Keploy control server at "
                  f"{CONTROL_SOCK}: {e}")
else:
    logging.info(f"Keploy control server at {CONTROL_SOCK}")
    threading.Thread(target=_server, args=(_srv,), daemon=False,
                     name="KeployControlServer").start()
    logging.info("Keploy agent ready (always-on tracer, clean start per test)")
