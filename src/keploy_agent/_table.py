"""The build table: the app's coverable lines, as keploy's coverage contract
(schema 1) holds a Python build, and the frames that carry it to keploy.

A unit is one app file: ``{"kind":"file","id":"f:<path>","pkg":<dir>,
"name":<base>,"path":<path>,"hash":<sha256 of its bytes>,"state":"ok",
"methods":[...]}``. A method is one code object (the module, a function, a
class body), ``{"id":"m:<path>#<qualname>@<first line>","name":<qualname>,
"line":<first line>,"leaves":[[L,0,L,0,1,0],...]}``, one code leaf per line
it owns. Each line is owned by exactly one method: the innermost code object
whose ``co_lines()`` has it, except a code object's first line (its ``def``
or first decorator), which stays with the code object that defines it when
that one has the line too. Code objects whose ids collide (two lambdas on one
line) are one method.

The build id is ``py1-`` and the first 32 hex of the sha256 over
``keploy-python-table 1\\npython <major>.<minor>\\nkeploy-agent <version>\\n
optimize <level>\\n`` and the sorted ``<path>\\0<hash>`` lines joined by ``\\n``: another file set,
another byte of a file, another Python minor version (which compiles lines
otherwise), another agent release (which may read lines otherwise) or another
optimization level (-O drops assert lines) is another build, so one build id
never names two tables.

Records are encoded as Go's encoding/json writes the contract's structs (its
field order, no spaces, no HTML escaping), units in the contract's order, so
the stream is the bytes keploy encodes the same table to.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import sys
import types

TABLE_FORMAT = 1
FRAME_MAGIC = b"KTB1"
# 8 + body <= 1 MiB (the agent's maxTableFrame); a header line is far under
# the 1 KiB kept for it.
MAX_FRAME = 1 << 20
MAX_PAYLOAD = MAX_FRAME - 8 - 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# The real socket class; the agent sets it to its saved original (gevent).
SOCKET = socket.socket


def runtime() -> str:
    return f"python {sys.version_info[0]}.{sys.version_info[1]}"


def build_id(hashes: dict[str, str], sdk_version: str, optimize: int | None = None) -> str:
    """The build id of the files {path: sha256 hex} as this agent version
    reads them at this optimization level (the process's own: -O drops
    assert lines); "" for none."""
    if not hashes:
        return ""
    if optimize is None:
        optimize = sys.flags.optimize
    head = (f"keploy-python-table {TABLE_FORMAT}\n{runtime()}\nkeploy-agent {sdk_version}\n"
            f"optimize {optimize}\n")
    body = "\n".join(sorted(f"{p}\0{h}" for p, h in hashes.items()))
    return "py1-" + hashlib.sha256((head + body).encode("utf-8")).hexdigest()[:32]


def file_hashes(root: str, paths: list[str]) -> dict[str, str]:
    """{path: sha256 hex of its bytes}. A file that cannot be read is left
    out: it is no unit of the build."""
    out = {}
    for p in paths:
        try:
            with open(os.path.join(root, p), "rb") as f:
                out[p] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
    return out


def _lines(code: types.CodeType) -> set[int]:
    if hasattr(code, "co_lines"):
        return {ln for _, _, ln in code.co_lines() if ln is not None and ln > 0}
    # 3.9: co_lnotab, through dis.
    import dis
    return {ln for _, ln in dis.findlinestarts(code) if ln is not None and ln > 0}


def _children(code: types.CodeType):
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            yield c


def methods_of(path: str, code: types.CodeType) -> list[dict]:
    """The methods of a module's code object, each with the lines it owns."""
    owner: dict[int, tuple] = {}   # line -> (depth, method key)
    names: dict[str, tuple[str, int]] = {}

    def visit(c: types.CodeType, parent_lines: set[int], qual: str, depth: int):
        lines = _lines(c)
        first = 1 if depth == 0 else c.co_firstlineno
        key = f"{qual}@{first}"
        names.setdefault(key, (qual, first))
        for ln in lines:
            if depth > 0 and ln == c.co_firstlineno and ln in parent_lines:
                continue  # its def line stays with the code that defines it
            got = owner.get(ln)
            if got is None or depth > got[0]:
                owner[ln] = (depth, key)
        for ch in _children(c):
            if hasattr(ch, "co_qualname"):
                q = ch.co_qualname
            elif depth == 0:
                q = ch.co_name
            elif c.co_flags & 0x02:  # CO_NEWLOCALS: a function's locals
                q = f"{qual}.<locals>.{ch.co_name}"
            else:
                q = f"{qual}.{ch.co_name}"
            visit(ch, lines, q, depth + 1)

    visit(code, set(), "<module>", 0)
    by_key: dict[str, list[int]] = {}
    for ln, (_, key) in owner.items():
        by_key.setdefault(key, []).append(ln)
    out = []
    for key, lines in by_key.items():
        qual, first = names[key]
        lines.sort()
        out.append({"id": f"m:{path}#{key}", "name": qual, "line": first,
                    "leaves": [[ln, 0, ln, 0, 1, 0] for ln in lines]})
    out.sort(key=lambda m: (m["line"], m["id"].encode("utf-8")))
    return out


def unit(root: str, path: str, file_hash: str, optimize: int | None = None) -> dict:
    """The table record of one app file, compiled at the process's own
    optimization level. One that cannot be read, does not compile, or whose
    bytes are no longer file_hash's (the bytes the build was named by, read
    when the agent started) is held not_measured, analyzer_failed. No clock
    is read: keploy's time freeze moves the app's clock, never a file's
    bytes."""
    pkg, _, name = path.rpartition("/")
    u = {"kind": "file", "id": "f:" + path, "pkg": pkg, "name": name,
         "path": path, "hash": file_hash, "state": "ok"}
    if optimize is None:
        optimize = sys.flags.optimize
    try:
        full = os.path.join(root, path)
        with open(full, "rb") as f:
            src = f.read()
        if hashlib.sha256(src).hexdigest() != file_hash:
            # Changed since the build was named: its lines are not the
            # build's.
            raise ValueError("changed since the build was named")
        # The warnings filters are the app's, and process-wide: they are not
        # touched here. A SyntaxWarning the file raises (an invalid escape)
        # is shown again, as the app's import showed it.
        code = compile(src, full, "exec", dont_inherit=True, optimize=optimize)
        methods = methods_of(path, code)
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        u["state"] = "not_measured"
        u["reason"] = {"code": "analyzer_failed"}
        methods = []
    u["methods"] = methods
    return u


def _pkg_key(pkg: str) -> bytes:
    # The contract orders package paths segment by segment ('/' first).
    return pkg.encode("utf-8").replace(b"/", b"\0")


def units(root: str, hashes: dict[str, str], optimize: int | None = None) -> list[dict]:
    us = [unit(root, p, h, optimize) for p, h in hashes.items()]
    us.sort(key=lambda u: (_pkg_key(u["pkg"]), u["id"].encode("utf-8")))
    return us


def encode(record: dict) -> bytes:
    """One NDJSON line, as Go's json.Encoder with SetEscapeHTML(false)
    writes the same struct (key order kept, no spaces)."""
    s = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return s.encode("utf-8") + b"\n"


def stream(us: list[dict]) -> bytes:
    return b"".join(encode(u) for u in us)


def frames(part: str, build: str, payload: bytes):
    """The bodies of one part's frames, in order: a JSON header line and up
    to MAX_PAYLOAD bytes of the part's stream. The last header names the
    stream's record count and sha256."""
    chunks = [payload[i:i + MAX_PAYLOAD] for i in range(0, len(payload), MAX_PAYLOAD)] or [b""]
    sha = hashlib.sha256(payload).hexdigest()
    records = payload.count(b"\n")
    for seq, chunk in enumerate(chunks):
        hd = {"v": 1, "part": part, "build": build, "seq": seq,
              "last": seq == len(chunks) - 1}
        if hd["last"]:
            hd["records"] = records
            hd["sha256"] = sha
        yield json.dumps(hd, separators=(",", ":")).encode() + b"\n" + chunk


def send_frame(data_sock: str, body: bytes, timeout: float = 60.0) -> None:
    """Send one frame on a connection of its own and wait for the agent's
    OK: the next frame goes only once this one is held."""
    with SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(data_sock)
        s.sendall(FRAME_MAGIC + struct.pack(">I", len(body)) + body)
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 64:
            chunk = s.recv(64)
            if not chunk:
                break
            answer += chunk
    if answer.strip() != b"OK":
        raise ConnectionError(f"the agent answered {answer!r} to a table frame, not OK")


def send_table(data_sock: str, build: str, table_stream: bytes) -> None:
    """Stream the table part, then the empty probes part (keploy maps a
    Python line to its leaf itself)."""
    for body in frames("table", build, table_stream):
        send_frame(data_sock, body)
    for body in frames("probes", build, b""):
        send_frame(data_sock, body)
