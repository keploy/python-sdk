"""The provider the shop calls: up while keploy records, stopped while it
replays. Prints "ready" once it listens."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PRICES = {"book": 4200, "pen": 150}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/price/"):
            return self.answer({"cents": PRICES.get(self.path[len("/price/"):], 0)})
        self.send_error(404, "no route")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if self.path == "/reserve":
            return self.answer({"ok": body.get("item") in PRICES})
        if self.path == "/charge":
            return self.answer({"status": "captured" if body.get("cents", 0) > 0 else "declined"})
        self.send_error(404, "no route")

    def answer(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
print("ready", flush=True)
server.serve_forever()
