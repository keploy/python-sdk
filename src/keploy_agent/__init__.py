"""
Keploy Python agent: per-test code coverage of the app, for keploy's dedup and
code coverage (`keploy test --dedup`).

Importing it (first thing in the app, or through the sitecustomize hook that
`python -m keploy_agent --pythonpath` puts on PYTHONPATH) binds the control
socket keploy sends its commands on, before the import returns, and starts
measuring the app's own files (see _files):

  START <set>/<case>  a test's window begins: the lines noted so far go.
  END <set>/<case>    the window ends: its lines go to keploy, by app-root-
                      relative path, on the data socket.
  TABLE               the build table (every app file's coverable lines,
                      _table) goes to keploy on the data socket, as frames.

Every command gets one answer line before the close: "ACK proto=2" when it
was taken, "IGNORED proto=2 <why>" when it was read and not taken. proto=2
says this agent answers every command it reads, so keploy reads a close with
no answer as a command not taken.

When the data socket is a new one (keploy starts a receiver per test run),
the next START also sends the INDEX record: the build id of the app files
and how they were found, which tells keploy to ask for the table.
"""

from __future__ import annotations

import atexit
import errno
import json
import logging
import os
import platform
import socket
import sys
import threading
import time

from . import _files, _measure, _table

# ------------------------------------------------ configuration
CONTROL_SOCK = "/tmp/coverage_control.sock"
DATA_SOCK = "/tmp/coverage_data.sock"

SDK_VERSION = "0.2.0"

APP_ROOT = os.path.realpath(os.getenv("KEPLOY_APP_SOURCE_DIR") or os.getcwd())
AGENT_DIR = os.path.dirname(os.path.realpath(__file__))

# Started by the sitecustomize hook (see _boot): every Python process the
# app's command starts imports the agent, so its thread is a daemon (a short
# script must still exit) and a socket a live process serves is left to it.
BOOT = bool(getattr(sys, "_keploy_agent_boot", False))

# END waits for the window's lines to settle: until no new line was noted for
# SETTLE_STEP, for at most SETTLE_MAX. Lines still coming then are a window
# cut short (windowCut): keploy keeps the test and does not use its lines.
SETTLE_STEP = 0.002
SETTLE_MAX = 0.1

log = logging.getLogger("keploy_agent")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[Keploy Agent] %(asctime)s pid %(process)d - %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    # The app's own logging setup is not this agent's to change.
    log.propagate = False

# The agent's answers on the control socket: one line for every command it
# reads, written before it closes the connection.
REPLY_TAKEN = b"ACK proto=2\n"
REPLY_IGNORED = b"IGNORED proto=2\n"

# ------------------------------------------------ agent state
lock = threading.Lock()
current_id = None                      # the test whose window is open
_EXPLICIT_ROOT = bool(os.getenv("KEPLOY_APP_SOURCE_DIR"))
_root_refused = _files.refused_root(APP_ROOT, _EXPLICIT_ROOT)
_app = _files.AppFiles(APP_ROOT, AGENT_DIR,
                       _files.excludes_from_env(os.getenv("KEPLOY_COVERAGE_EXCLUDE")),
                       explicit=_EXPLICIT_ROOT)
_collector = None                      # set once the control socket is bound
_unmeasured = ""                       # why there is no collector
_owner_pid = None                      # the process that bound the socket
_own_ident = None                      # the socket file it bound
_had_window = False                    # a START was taken here
_peers: set = set()                    # processes the hook loaded that found it served
_yielding = False                      # a YIELD was taken: the socket is another's
_from_pid = 0                          # the process this one took the socket from
HANDOVER_WAIT = 5.0                    # how long a YIELD waits for the asker's bind
_yielded = False

_index_lock = threading.Lock()
_index = None                          # (hashes, build, why), once computed
_index_ready = threading.Event()
_data_ident = None                     # the data socket the INDEX went to
_table_lock = threading.Lock()
_table_busy = False

# The INDEX waits this long for the build before the START is answered; a
# build not ready by then is sent on its own thread once it is.
INDEX_WAIT = 2.0


class _NotTaken(Exception):
    """A command read and not taken: the message is why."""


# ------------------------------------------------ the build and its INDEX
def _build():
    """The app files' hashes and build id, computed once: the table keploy
    asks for later is of these bytes. why says why there is no build."""
    global _index
    with _index_lock:
        if _index is None:
            try:
                paths = _app.walk()
                hashes = _table.file_hashes(APP_ROOT, paths)
                build = _table.build_id(hashes, SDK_VERSION)
                why = "" if build else f"no Python file of the app under {APP_ROOT}"
            except Exception as e:  # any failure is a build with a reason, kept
                hashes, build, why = {}, "", str(e)
            _index = (hashes, build, why)
            _index_ready.set()
        return _index


def _index_record() -> dict:
    hashes, build, why = _build()
    return {
        "kind": "index", "v": 1, "language": "python", "build": build,
        "source": "root" if build else "none",
        "launch": (why or " ".join(sys.argv))[:512],
        "roots": [APP_ROOT], "rootCount": 1, "packages": [],
        "classes": len(hashes), "includes": "", "jacoco": "",
        "sdkVersion": SDK_VERSION, "jacocoVersion": "",
        "producer": _collector.name if _collector else "",
        "producerVersion": platform.python_version(),
    }


def _ident(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_ctime_ns)


def _send(payload: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(DATA_SOCK)
        s.sendall(payload)


def _send_index(ident) -> None:
    global _data_ident
    try:
        _send(json.dumps(_index_record(), separators=(",", ":")).encode())
    except Exception as e:
        # Sent again at the next START.
        if _data_ident == ident:
            _data_ident = None
        log.warning(f"could not send the INDEX record to {DATA_SOCK}: {e}")


def _index_if_new() -> None:
    """Send the INDEX record when the data socket is not the one it last
    went to (a new receiver knows no build yet), before the START that
    sees it is answered, so keploy asks for the table at the next START.
    A build not ready within INDEX_WAIT goes on a thread of its own."""
    global _data_ident
    ident = _ident(DATA_SOCK)
    if ident is None or ident == _data_ident:
        return
    _data_ident = ident
    if _index_ready.wait(INDEX_WAIT):
        _send_index(ident)
    else:
        threading.Thread(target=_send_index, args=(ident,), daemon=True,
                         name="KeployIndex").start()


def _stream_table(build: str, hashes: dict) -> None:
    global _table_busy
    try:
        t0 = time.monotonic()
        units = _table.units(APP_ROOT, hashes)
        payload = _table.stream(units)
        _table.send_table(DATA_SOCK, build, payload)
        log.info(f"sent the build table of {build}: {len(units)} file(s), "
                 f"{len(payload)} bytes, in {time.monotonic() - t0:.2f}s")
    except Exception as e:
        log.warning(f"could not send the build table: {e}")
    finally:
        with _table_lock:
            _table_busy = False


# ------------------------------------------------ other processes of the app
def _live_children() -> list:
    """The pids of this process's live child processes."""
    me = os.getpid()
    kids = []
    try:
        for tid in os.listdir(f"/proc/{me}/task"):
            with open(f"/proc/{me}/task/{tid}/children") as f:
                kids += [int(x) for x in f.read().split()]
        return kids
    except (OSError, ValueError):
        pass
    if os.path.isdir("/proc"):
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    if int(f.read().rsplit(")", 1)[1].split()[1]) == me:
                        kids.append(int(d))
            except (OSError, ValueError, IndexError):
                continue
        return kids
    try:  # macOS and the like
        import subprocess
        out = subprocess.run(["pgrep", "-P", str(me)], capture_output=True, text=True, timeout=5)
        return [int(x) for x in out.stdout.split()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _alive(pid: int) -> bool:
    if not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    except (OverflowError, ValueError):
        return False
    return True


def _others_serving() -> str:
    """Why a window that ran no app line may not be the test's: the app
    serves requests in processes keploy does not measure. "" otherwise."""
    if _live_children():
        return ("the app serves requests in child processes (prefork workers) that "
                "keploy cannot measure; run one process")
    if any(_alive(p) for p in list(_peers)):
        return ("the app serves requests in other processes (workers) that keploy "
                "cannot measure; run one process")
    if _from_pid and _alive(_from_pid):
        return (f"the process this one took keploy's socket from (pid {_from_pid}) is alive "
                "and may serve requests keploy cannot measure; run one process")
    return ""


def _cmdline() -> list:
    """This process's command line: sys.orig_argv, else /proc (3.9, whose
    sys.argv lacks a -c command's text)."""
    argv = list(getattr(sys, "orig_argv", None) or [])
    if argv:
        return argv
    try:
        with open("/proc/self/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return list(getattr(sys, "argv", None) or [])


def _stdlib_helper() -> str:
    """The stdlib helper this process is (multiprocessing's resource tracker,
    forkserver or semaphore tracker), "" for any other. The hook loads the
    agent in every Python process, and a helper never serves the app: one
    that took the socket left the server unmeasured. multiprocessing's
    spawn_main is the server itself (uvicorn --reload's child), never left
    out."""
    argv = _cmdline()
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            code = argv[i + 1]
            for helper in ("multiprocessing.resource_tracker", "multiprocessing.forkserver",
                           "multiprocessing.semaphore_tracker"):
                if f"from {helper} import main" in code:
                    return helper
            return ""
    return ""


def _serves_nothing() -> str:
    """Why this process, which holds keploy's control socket, is not the one
    that serves the app's requests: it holds no listening TCP or Unix socket
    besides keploy's (Linux). "" when it holds one, or it cannot be told."""
    try:
        fds = os.listdir("/proc/self/fd")
    except OSError:
        return ""
    held = set()
    for fd in fds:
        try:
            t = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if t.startswith("socket:["):
            try:
                held.add(int(t[8:-1]))
            except ValueError:
                pass
    try:
        held.discard(os.fstat(_srv.fileno()).st_ino)
    except (OSError, NameError, ValueError):
        pass
    listening, read = set(), False
    for name in ("tcp", "tcp6"):
        try:
            with open(f"/proc/self/net/{name}") as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        read = True
        for r in rows:
            c = r.split()
            if len(c) > 9 and c[3] == "0A":          # TCP_LISTEN
                listening.add(int(c[9]))
    try:
        with open("/proc/self/net/unix") as f:
            rows = f.read().splitlines()[1:]
        read = True
        for r in rows:
            c = r.split()
            if len(c) > 6 and int(c[3], 16) & 0x10000:   # __SO_ACCEPTCON
                listening.add(int(c[6]))
    except (OSError, ValueError):
        pass
    if not read or held & listening:
        return ""
    cmd = " ".join(_cmdline())[:80] or "?"
    return (f"the process holding keploy's control socket serves no requests "
            f"({cmd} pid {os.getpid()}): keploy measures the wrong process")


# ------------------------------------------------ the window
def _settle() -> bool:
    """Wait until no new line comes for SETTLE_STEP, at most SETTLE_MAX;
    True when the lines settled."""
    deadline = time.monotonic() + SETTLE_MAX
    n = _collector.count()
    while True:
        time.sleep(SETTLE_STEP)
        m = _collector.count()
        if m == n:
            return True
        if time.monotonic() >= deadline:
            return False
        n = m


def _emit(test_id: str) -> None:
    """Send the window's lines as test_id's record. A window that ran no app
    line while other processes of the app serve requests is not taken: its
    test was likely served where keploy does not measure."""
    settled = _settle()
    data = _collector.take()
    if not data:
        why = _others_serving() or _serves_nothing()
        if why:
            raise _NotTaken(why)
    record = {"id": test_id, "executedLinesByFile": data}
    if not settled:
        record["windowCut"] = True
    _send(json.dumps(record, separators=(",", ":")).encode())
    log.debug(f"[{test_id}] sent {len(data)} file(s) / "
              f"{sum(len(v) for v in data.values())} line(s)"
              + ("; its lines were still coming (windowCut)" if not settled else ""))


# ------------------------------------------------ per-connection handler
def _sender(arg: str, peer) -> int:
    """The pid a PEER or YIELD names, when it is the pid of the process that
    sent it (peer, read from the connection by SO_PEERCRED in this pid
    namespace); never the text alone."""
    if not (arg.isascii() and arg.isdigit() and len(arg) <= 10) or not 0 < int(arg) < 2 ** 31:
        raise _NotTaken(f"not a process id: {arg[:20]!r}")
    if not peer or peer < 1:
        raise _NotTaken("keploy cannot tell which process sent this")
    if int(arg) != peer:
        raise _NotTaken(f"it names process {int(arg)}, but process {peer} sent it")
    return peer


def _run(cmd: str, peer=None) -> bool:
    """Run one command; True when taken. Raises _NotTaken (or anything)
    when it was not, with why. peer is the sender's pid, read for PEER and
    YIELD only."""
    global current_id, _table_busy, _had_window
    action, sep, arg = cmd.partition(" ")
    if action == "TABLE" and not sep:
        if _yielding:
            raise _NotTaken("this process gave keploy's socket to the process that serves the app")
        if _collector is None:
            raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
        _, build, why = _build()
        if not build:
            raise _NotTaken(f"no build table: {why}")
        with _table_lock:
            if _table_busy:
                raise _NotTaken("a build table is on its way already")
            _table_busy = True
        try:
            threading.Thread(target=_stream_table, args=(build, _build()[0]),
                             daemon=True, name="KeployTable").start()
        except BaseException:
            with _table_lock:
                _table_busy = False
            raise
        return True
    if action in ("PEER", "YIELD"):
        pid = _sender(arg, peer)
        if action == "PEER":
            # A process the hook loaded found this socket served: if this
            # process's windows run no line while it lives, it may be serving.
            _peers.add(pid)
            return True
        return _yield_to(pid)
    if action not in ("START", "END"):
        raise _NotTaken(f"unknown command {action[:20]!r}")
    test_id = arg
    if not test_id:
        raise _NotTaken(f"{action} names no test")

    if _collector is not None:
        failure = _collector.take_failure()
        if failure:
            log.warning(f"keploy's line callback failed in {failure[0]} ({failure[1]}): "
                        "those locations are no longer measured")
    with lock:
        if action == "START":
            if _yielding:
                raise _NotTaken("this process gave keploy's socket to the process that serves the app")
            # The INDEX goes even from a process that measures nothing, so
            # keploy can say why.
            _index_if_new()
            if _collector is None:
                raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
            replaced = _collector.replaced()
            if replaced:
                current_id = None
                raise _NotTaken(replaced)
            log.debug(f"START {test_id}")
            if current_id is not None and current_id != test_id:
                log.warning(f"START {test_id} while {current_id} had no END: "
                            f"{current_id}'s lines are dropped")
            try:
                _collector.reset()
            except Exception as e:
                # Lines not reset would hold the tests before this one: the
                # START is not taken, and its END is ignored.
                current_id = None
                log.error(f"[{test_id}] could not reset the lines: {e}", exc_info=True)
                raise _NotTaken("could not reset the lines") from e
            current_id = test_id
            _had_window = True
            return True

        if _collector is None:
            raise _NotTaken(f"keploy cannot measure this process: {_unmeasured}")
        log.debug(f"END   {test_id}")
        if test_id != current_id:
            log.warning(f"END {test_id} does not match the test started "
                        f"({current_id}): no coverage sent")
            raise _NotTaken(f"no window of {test_id[:60]} is open")
        try:
            replaced = _collector.replaced()
            if replaced:
                raise _NotTaken(replaced)
            _emit(test_id)
        except _NotTaken:
            raise
        except Exception as e:
            log.error(f"[{test_id}] could not send the coverage: {e}", exc_info=True)
            raise _NotTaken("could not send the coverage") from e
        finally:
            current_id = None
        return True


_open_conns: set = set()               # connections being served (closed in a forked child)


def _handle(conn: socket.socket):
    """Read one command, run it, and answer it before the close."""
    _open_conns.add(conn)
    if conn.gettimeout() is None:
        conn.settimeout(60)
    fp = conn.makefile("r", errors="replace")
    try:
        try:
            raw = fp.readline(4096)
        except OSError as e:
            log.warning(f"Could not read a command: {e}")
            return
        if not raw:
            return                          # closed with no command
        cmd = raw.strip()
        reply = REPLY_IGNORED
        try:
            if cmd.split(" ", 1)[0] in ("PEER", "YIELD"):
                taken = _run(cmd, _peer_pid(conn))
            else:
                taken = _run(cmd)
            if taken:
                reply = REPLY_TAKEN
        except _NotTaken as e:
            reply = REPLY_IGNORED[:-1] + b" " + str(e).replace("\n", " ").encode("utf-8", "replace") + b"\n"
        except Exception as e:
            log.error(f"Handler error: {e}", exc_info=True)
        try:
            conn.sendall(reply)
        except OSError as e:
            log.warning(f"Could not answer {cmd!r}: {e}")
    finally:
        try:
            fp.close()
            conn.close()
        except Exception:
            pass
        _open_conns.discard(conn)
        _check_still_owner()


# ------------------------------------------------ control server
class _Served(Exception):
    """A live process serves the control socket already."""


def _peer_pid(s: socket.socket):
    """The pid of the process at the other end of a Unix socket, or None."""
    try:
        if hasattr(socket, "SO_PEERCRED"):           # Linux
            import struct
            return struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                    struct.calcsize("3i")))[0]
        if sys.platform == "darwin":                 # SOL_LOCAL, LOCAL_PEERPID
            import struct
            return struct.unpack("i", s.getsockopt(0, 0x002, 4))[0]
    except OSError:
        pass
    return None


def _read_ppid(pid: int):
    """pid's parent by /proc, or None where /proc cannot say."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _ancestors(start=None, read_ppid=_read_ppid) -> set:
    """The pids above this process (or above start's child: start is the
    parent), the parent first, pid 1 included: a container's command is pid
    1, and it may be the reloader."""
    out = set()
    pid = os.getppid() if start is None else start
    for _ in range(64):
        if pid < 1 or pid in out:
            break
        out.add(pid)
        nxt = read_ppid(pid)
        if nxt is None:
            break
        pid = nxt
    return out


def _ask(s: socket.socket, line: str) -> str:
    s.sendall(line.encode() + b"\n")
    reply = b""
    while not reply.endswith(b"\n") and len(reply) < 512:
        chunk = s.recv(512)
        if not chunk:
            break
        reply += chunk
    return reply.decode("utf-8", "replace").strip()


def _server_child() -> str:
    """Why this process is a reloader's server child, which takes keploy's
    socket from the ancestor that started it: Django's runserver child
    (RUN_MAIN), Flask's (WERKZEUG_RUN_MAIN), uvicorn's and hypercorn's
    (multiprocessing's spawn_main), or KEPLOY_COVERAGE_TAKEOVER=1. "" for any
    other process (a pool, a sidecar, a script), which never takes it: its
    lines are not the server's."""
    if os.environ.get("KEPLOY_COVERAGE_TAKEOVER") == "1":
        return "KEPLOY_COVERAGE_TAKEOVER"
    if os.environ.get("RUN_MAIN") == "true":
        return "RUN_MAIN"
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        return "WERKZEUG_RUN_MAIN"
    argv = _cmdline()
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            return "spawn_main" if "from multiprocessing.spawn import spawn_main" in argv[i + 1] else ""
    return ""


def _send_peer(path: str) -> None:
    """Tell the socket's owner this process lives beside it, unmeasured."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(2)
    try:
        c.connect(path)
        _ask(c, f"PEER {os.getpid()}")
    except OSError:
        pass
    finally:
        c.close()


def _lock(path: str):
    """An exclusive lock beside the socket (path.lock) that serializes the
    processes that probe, take over or bind it, so two never both replace
    it; None when the lock file cannot be had (the bind goes on without)."""
    try:
        import fcntl
        fd = os.open(path + ".lock", os.O_RDONLY | os.O_CREAT, 0o666)
    except (ImportError, OSError):
        return None
    deadline = time.monotonic() + 5
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)


def _bind(path: str) -> socket.socket:
    """Bind and listen on the control socket at path. A file no process
    serves is replaced. One a live process serves is left to it (_Served),
    unless that process is an ancestor of this one that has taken no test's
    window yet (a reloader or supervisor whose child serves the app): asked,
    it yields, once, and this process takes the socket. The probe, the ask
    and the replace hold path.lock, and the replace removes only the file
    this process probed."""
    lk = _lock(path)
    try:
        return _bind_locked(path)
    finally:
        if lk is not None:
            os.close(lk)


def _bind_locked(path: str) -> socket.socket:
    global _from_pid
    took_from = 0
    probed = _ident(path)
    if probed is not None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2)
        try:
            try:
                probe.connect(path)
            except (ConnectionRefusedError, FileNotFoundError):
                pass                        # stale: no one listens there
            except OSError as e:
                if isinstance(e, socket.timeout) or e.errno in (None, errno.EAGAIN):
                    raise _Served(path)     # a live owner too busy to accept
                raise
            else:
                owner = _peer_pid(probe)
                if owner is None or owner not in _ancestors() or not _server_child():
                    try:
                        _ask(probe, f"PEER {os.getpid()}")
                    except OSError:
                        pass
                    raise _Served(path)
                probe.settimeout(10)
                try:
                    answer = _ask(probe, f"YIELD {os.getpid()}")
                except OSError as e:
                    log.warning(f"could not ask process {owner} for keploy's control socket "
                                f"({e!r}); this process (pid {os.getpid()}) is not measured")
                    raise _Served(path)
                if not answer.startswith("ACK"):
                    # Refused: a peer of the owner, as any other process the
                    # app starts, so its empty windows fail closed.
                    _send_peer(path)
                    raise _Served(path)
                took_from = owner
        finally:
            probe.close()
        if _ident(path) != probed:
            # Another process replaced the file since this one probed it:
            # that one's socket is not this one's to remove.
            raise _Served(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen()
    except BaseException as e:
        srv.close()
        if took_from:
            # The owner waits for this bind (HANDOVER_WAIT), then serves on.
            log.warning(f"process {took_from} gave keploy's control socket, and this one "
                        f"(pid {os.getpid()}) could not bind it: {e!r}")
        raise
    if took_from:
        # Its windows may still be served there: an empty one is not taken
        # while it lives (_others_serving).
        _from_pid = took_from
        log.info(f"took the Keploy control socket over from process {took_from}, "
                 "which serves no request itself (a reloader or supervisor)")
    return srv


def _stop_serving(why: str) -> None:
    """Stop serving the control socket and measuring, for good."""
    global _collector, _unmeasured, _yielded
    if _yielded:
        return
    _yielded = True
    c, _collector, _unmeasured = _collector, None, why
    try:
        # shutdown wakes the accept blocked on it (close alone does not, on
        # Linux), so no connection queued before is answered.
        _srv.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        _srv.close()
    except Exception:
        pass
    if c is not None:
        try:
            c.stop()
        except Exception:
            pass
    log.info(why)


def _yield_to(pid: int) -> bool:
    """YIELD from pid, a process that found this socket served: this one
    gives it up when it has taken no test's window (a reloader's parent);
    the asker then binds the path again."""
    global _yielding
    with lock:
        if _from_pid:
            # One handover per tree: the markers a server child carries
            # (RUN_MAIN, spawn_main) are its children's too.
            raise _NotTaken("this process took keploy's socket over itself, and hands it to no other")
        if _yielding:
            raise _NotTaken("this process gave its socket to another process already")
        if _had_window:
            raise _NotTaken("this process has taken a test's window")
        if pid not in _descendants_hint(pid):
            raise _NotTaken("only a child of this process takes its socket")
        # From here no START is taken and no other asker wins.
        _yielding = True
    threading.Thread(target=_hand_over, args=(pid,), daemon=True,
                     name="KeployHandover").start()
    return True


def _hand_over(pid: int) -> None:
    """Serve on until the socket file is pid's (its listener, by
    SO_PEERCRED), then stop. One that never is (the asker died, or could
    not bind) within HANDOVER_WAIT leaves this process the owner."""
    global _yielding
    deadline = time.monotonic() + HANDOVER_WAIT
    while time.monotonic() < deadline:
        ident = _ident(CONTROL_SOCK)
        if ident is not None and ident != _own_ident:
            got = None
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(1)
            try:
                c.connect(CONTROL_SOCK)
                got = _peer_pid(c)
            except OSError:
                pass                        # bound, not listening yet
            finally:
                c.close()
            if got == pid:
                _stop_serving(f"gave the Keploy control socket to process {pid}, which "
                              "serves the app; this process is no longer measured")
                return
            if got is not None:
                _stop_serving(f"the Keploy control socket at {CONTROL_SOCK} is another "
                              "process's now; this process is no longer measured")
                return
        time.sleep(0.02)
    with lock:
        _yielding = False
    log.warning(f"process {pid} asked for keploy's control socket and did not take it "
                f"within {HANDOVER_WAIT:.0f}s; this process (pid {os.getpid()}) serves it again")


def _descendants_hint(pid: int) -> set:
    """{pid} when pid descends from this process (by /proc), else empty;
    {pid} where /proc cannot tell (the asker checked its own ancestors)."""
    me, cur = os.getpid(), pid
    for _ in range(64):
        nxt = _read_ppid(cur)
        if nxt is None:
            return {pid} if not os.path.isdir("/proc") else set()
        cur = nxt
        if cur == me:
            return {pid}
        if cur < 1:
            return set()
    return set()


def _check_still_owner() -> None:
    """After a command: a process whose socket file is no longer the one it
    bound (another took the path) stops serving and measuring."""
    if _own_ident is not None and not _yielded and not _yielding and os.getpid() == _owner_pid \
            and _ident(CONTROL_SOCK) != _own_ident:
        _stop_serving(f"the Keploy control socket at {CONTROL_SOCK} is another "
                      "process's now; this process is no longer measured")


def _server(srv: socket.socket):
    try:
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return                      # closed: yielded, or forked away
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
    finally:
        srv.close()


def _at_exit(path: str) -> None:
    """At exit: stop measuring, and remove the control socket file if this
    process bound it and it is still the one it bound (a forked child that
    exits leaves its parent's)."""
    c = _collector
    if c is not None:
        try:
            c.stop()
        except Exception:
            pass
    if os.getpid() == _owner_pid and _own_ident is not None and _ident(path) == _own_ident:
        try:
            os.remove(path)
        except OSError:
            pass


def _forked():
    """A forked child (a prefork server's worker) has no thread serving the
    control socket, which its parent holds: it closes its copies of the
    listening socket and of the connections the parent was serving, and
    stops measuring. The parent answers keploy for it (_others_serving)."""
    global _collector, _unmeasured
    collector = _collector
    _collector, _unmeasured = None, "a forked child of the process that serves the socket"
    # The fds themselves: a served connection's makefile holds a reference,
    # so socket.close() alone would leave the fd open, and the parent's
    # answer's close held back while this child lives.
    srv = globals().get("_srv")
    for sock in ([srv] if srv is not None else []) + list(_open_conns):
        try:
            os.close(sock.detach())
        except Exception:
            pass
    _open_conns.clear()
    if collector is not None:
        try:
            collector.stop()
        except Exception:
            pass
    log.debug(f"process {os.getpid()} was forked from {os.getppid()}, which serves "
              "keploy's control socket: the requests this process serves are not "
              "measured (run the app as one process to measure it)")


def _invoked_for_pythonpath() -> bool:
    """`python -m keploy_agent --pythonpath` only prints a path."""
    argv = list(getattr(sys, "orig_argv", None) or [])
    for i, a in enumerate(argv):
        if a == "-m" and argv[i + 1:i + 3] == ["keploy_agent", "--pythonpath"]:
            return True
        if a == "-mkeploy_agent" and argv[i + 1:i + 2] == ["--pythonpath"]:
            return True
    if argv:
        return False
    # 3.9 has no orig_argv: while -m imports the package, argv[0] is "-m".
    argv = getattr(sys, "argv", None) or []
    return argv[:2] == ["-m", "--pythonpath"]


# ------------------------------------------------ start
# The control socket is bound here, before this import returns, so it is
# there before the app can say it is ready: bound on a thread, the bind could
# come after the app's port, and the first test's START then found no one
# listening and its coverage was lost. Commands are served on a thread. A
# socket that cannot be bound is said, and the app runs on without coverage.
def _start():
    global _srv, _collector, _unmeasured, _owner_pid, _own_ident
    helper = _stdlib_helper()
    if helper:
        log.debug(f"{helper} (pid {os.getpid()}) serves no request: not measured")
        return
    try:
        _srv = _bind(CONTROL_SOCK)
    except _Served:
        log.debug(f"another process serves the Keploy control socket at "
                  f"{CONTROL_SOCK}; this one (pid {os.getpid()}) is not measured")
        return
    except OSError as e:
        log.error(f"Could not start the Keploy control server at "
                  f"{CONTROL_SOCK}: {e}")
        return
    _owner_pid, _own_ident = os.getpid(), _ident(CONTROL_SOCK)
    atexit.register(_at_exit, CONTROL_SOCK)
    if _root_refused:
        _unmeasured = _root_refused
        log.error(f"Keploy measures nothing in this process: {_root_refused}")
    else:
        try:
            _collector = _measure.start(_app)
        except _measure.Unavailable as e:
            _unmeasured = str(e)
            log.error(f"Keploy cannot measure this process: {e}")
    # The build is read now, off the app's path, so the first START's INDEX
    # is ready.
    threading.Thread(target=_build, daemon=True, name="KeployBuild").start()
    log.info(f"Keploy control server at {CONTROL_SOCK}")
    threading.Thread(target=_server, args=(_srv,), daemon=True,
                     name="KeployControlServer").start()
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_forked)
    if _collector is not None:
        untraced = getattr(_collector, "untraced_threads", 0)
        if untraced:
            log.warning(f"{untraced} thread(s) started before keploy_agent was "
                        "imported are not measured (Python < 3.12): import it first")
        log.info(f"Keploy agent ready ({_collector.name}, app root {APP_ROOT})")


if not _invoked_for_pythonpath():
    _start()
