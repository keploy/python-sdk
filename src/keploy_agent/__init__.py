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

def _ack(c: socket.socket):
    try: c.sendall(b"ACK\n")
    except OSError: pass

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
def _handle(conn: socket.socket):
    global current_id, baseline_counts, baseline_tids
    fp = conn.makefile("r")
    try:
        cmd = fp.readline().strip()
        if not cmd: return
        action, test_id = cmd.split(" ", 1)

        with lock:
            if action == "START":
                logging.info(f"START {test_id}")

                _tracer.results().counts.clear()        # start from 0 hits
                baseline_counts = {}                    # diff against empty dict
                baseline_tids = _current_tids()         # remember threads

                current_id = test_id
                _ack(conn)
                return

            if action == "END":
                logging.info(f"END   {test_id}")
                if test_id == current_id:
                    _wait_for_worker_exit(baseline_tids)   # <- NEW
                    time.sleep(0.02)  
                    _emit(test_id)
                    current_id = None
                _ack(conn)
                return

            logging.warning(f"Unknown command: {action}")
            _ack(conn)

    except Exception as e:
        logging.error(f"Handler error: {e}", exc_info=True); _ack(conn)
    finally:
        try: fp.close(); conn.close()
        except Exception: pass

# ------------------------------------------------ control server
def _server():
    if os.path.exists(CONTROL_SOCK): os.remove(CONTROL_SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(CONTROL_SOCK); srv.listen()
    logging.info(f"Keploy control server at {CONTROL_SOCK}")
    try:
        while True:
            c, _ = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
    finally:
        srv.close()

threading.Thread(target=_server, daemon=False,
                 name="KeployControlServer").start()
logging.info("Keploy agent ready (always-on tracer, clean start per test)")


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
