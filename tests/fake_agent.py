"""A fake keploy agent on the coverage data socket, for the rows: it takes
what pkg/service/agent's receiver takes. A connection that opens with "K"
is a KTB1 frame (each part's stream checked on its last frame as
finishLocked checks it, then answered "OK"); any other is one JSON record.
What it takes goes into out["records"] and out["parts"], under lock."""

import hashlib
import json
import os
import socket
import struct
import threading


class FakeAgent:
    def __init__(self, path, out, lock):
        self.path = path
        self.out = out
        self.lock = lock
        self.srv = None
        self.cur = None   # [part, build, next seq, bytes]
        # The answer to a frame: the agent's "OK", or another to refuse it.
        self.frame_answer = b"OK\n"
        self.bind()

    def bind(self):
        if self.srv is not None:
            self.srv.close()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(64)
        self.srv = srv
        threading.Thread(target=self.serve, args=(srv,), daemon=True).start()

    def serve(self, srv):
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        try:
            first = c.recv(1)
            if first == b"K":
                self.frame(c)
            else:
                data = first
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                rec = json.loads(data)
                with self.lock:
                    self.out["records"].append({"first_key": next(iter(rec)), "record": rec})
        except Exception as e:
            with self.lock:
                self.out["errors"].append(repr(e))
        finally:
            c.close()

    def read(self, c, n):
        b = b""
        while len(b) < n:
            chunk = c.recv(n - len(b))
            if not chunk:
                raise EOFError("short frame")
            b += chunk
        return b

    def frame(self, c):
        prefix = self.read(c, 7)
        if prefix[:3] != b"TB1":
            raise ValueError(f"magic K{prefix[:3]!r}")
        n = struct.unpack(">I", prefix[3:])[0]
        if n + 8 > 1 << 20:
            raise ValueError(f"frame of {n + 8} bytes")
        body = self.read(c, n)
        line, _, payload = body.partition(b"\n")
        hd = json.loads(line)
        with self.lock:
            if hd["v"] != 1:
                raise ValueError("version")
            if hd["seq"] == 0:
                self.cur = [hd["part"], hd["build"], 0, b""]
            cur = self.cur
            if cur is None or cur[0] != hd["part"] or cur[1] != hd["build"] or cur[2] != hd["seq"]:
                raise ValueError(f"frame out of order: {hd}")
            cur[3] += payload
            cur[2] += 1
            if hd["last"]:
                stream = cur[3]
                if hd.get("records") is None:
                    raise ValueError("last frame names no record count")
                if hd["sha256"] != hashlib.sha256(stream).hexdigest():
                    raise ValueError("sha256")
                if stream and not stream.endswith(b"\n"):
                    raise ValueError("last record not ended")
                if hd["records"] != stream.count(b"\n"):
                    raise ValueError("records")
                self.out["parts"].append({"part": cur[0], "build": cur[1], "frames": cur[2],
                                     "records": hd["records"],
                                     "stream": stream.decode("utf-8")})
                self.cur = None
        c.sendall(self.frame_answer)


def command(sock, cmd):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(30)
    c.connect(sock)
    c.sendall(cmd.encode() + b"\n")
    # One answer line, as keploy reads it: never the close, which a process
    # forked meanwhile can hold back (it holds a copy of the connection).
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = c.recv(256)
        if not chunk:
            break
        reply += chunk
    c.close()
    return reply.decode()
